"""``amw-replay`` / ``python -m app.replay`` ``--bundle <bundle.json> --calls <recorded receipts>``."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from pathlib import Path
import sys
import tempfile

from app.replay.policy_replay import (
    ReplayError,
    load_policy_bundle,
    load_recorded_calls,
    replay_calls,
)
from app.services.signing_keys import SigningKeyError


def _default_output_mode() -> int:
    """Mode for new outputs: ``0o666`` masked by the process umask."""
    current = os.umask(0)
    os.umask(current)
    return 0o666 & ~current


def _write_outputs_atomically(out: Path, files: dict[str, str]) -> None:
    """Write every output through a temp file in ``out``, then rename into place.

    Existing targets are preserved via same-dir hardlink backups before any
    rename, falling back to a copy when hardlinks are unavailable. Each temp
    takes its target's mode when the target exists, else ``0o666 & ~umask``.
    Targets are recorded before each rename so an interruption between the
    rename and the bookkeeping still rolls back. On any failure, new temps
    are removed, targets whose rename happened are restored from backups,
    targets whose rename never happened keep their untouched contents, and
    targets that did not previously exist are removed when present. Backups
    are deleted on success and after a clean rollback. A backup whose
    restore fails is kept and its path is included in the raised error.
    """
    temps: list[tuple[str, Path]] = []
    backups: dict[Path, Path] = {}
    placed: list[Path] = []
    pre_existing: set[Path] = set()
    try:
        for name, content in files.items():
            fd, tmp_name = tempfile.mkstemp(prefix=f"{name}.", suffix=".tmp", dir=out)
            temps.append((tmp_name, out / name))
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
        for _, target in temps:
            if not target.exists():
                continue
            pre_existing.add(target)
            fd_bak, bak_name = tempfile.mkstemp(
                prefix=f"{target.name}.", suffix=".bak.tmp", dir=out
            )
            try:
                os.close(fd_bak)
                os.unlink(bak_name)
                try:
                    os.link(target, bak_name)
                except OSError:
                    shutil.copy2(target, bak_name)
            except BaseException:
                try:
                    os.unlink(bak_name)
                except OSError:
                    pass
                raise
            backups[target] = Path(bak_name)
        default_mode = _default_output_mode()
        for tmp_name, target in temps:
            try:
                mode = stat.S_IMODE(os.stat(target).st_mode)
            except FileNotFoundError:
                mode = default_mode
            os.chmod(tmp_name, mode)
            placed.append(target)
            os.replace(tmp_name, target)
    except BaseException as original:
        # A recorded target whose temp still exists was never replaced: the
        # rename failed or an interrupt landed first, and the target keeps
        # its untouched contents. A missing temp means the rename happened
        # and the target must be restored from backup.
        unreplaced = {target for tmp_name, target in temps if os.path.exists(tmp_name)}
        for tmp_name, _ in temps:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        failed_restores: list[Path] = []
        for target in placed:
            backup = backups.get(target)
            if backup is None:
                if target not in pre_existing and target.exists():
                    try:
                        target.unlink()
                    except OSError:
                        pass
            elif target not in unreplaced:
                try:
                    os.replace(backup, target)
                except OSError:
                    failed_restores.append(backup)
            # else: the rename never happened, so the untouched target needs
            # no restore and its backup is removed with the rest below.
        for backup in backups.values():
            if backup in failed_restores:
                continue
            try:
                os.unlink(backup)
            except OSError:
                pass
        if failed_restores:
            kept = ", ".join(str(path) for path in failed_restores)
            message = f"rollback restore failed, backup(s) preserved at: {kept}"
            if isinstance(original, OSError):
                raise OSError(f"{original}; {message}") from original
            original.add_note(message)
            raise
        raise
    else:
        for backup in backups.values():
            try:
                os.unlink(backup)
            except OSError:
                pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="amw-replay",
        description=(
            "Re-decide recorded gateway calls under a new policy bundle. "
            "Never executes a tool or contacts any server."
        ),
    )
    parser.add_argument("--bundle", required=True, help="New policy bundle JSON")
    parser.add_argument(
        "--calls",
        required=True,
        help="Recorded gateway receipts: .json, .jsonl, or a directory",
    )
    parser.add_argument("--out", required=True, help="Output directory")
    parser.add_argument(
        "--recorded-keys",
        help="trust-keys.json to verify each recorded receipt before replay",
    )
    parser.add_argument(
        "--require-verified",
        action="store_true",
        help="fail unless every recorded receipt verified against --recorded-keys",
    )
    args = parser.parse_args(argv)

    if args.require_verified and not args.recorded_keys:
        print(
            "amw-replay: --require-verified needs --recorded-keys",
            file=sys.stderr,
        )
        return 2

    try:
        recorded_keys = None
        if args.recorded_keys:
            from b2a_sdk.receipt_verifier import (
                VerificationError,
                key_set_from_document,
            )

            try:
                recorded_keys = key_set_from_document(
                    json.loads(Path(args.recorded_keys).read_text(encoding="utf-8"))
                )
            except VerificationError as exc:
                raise ReplayError(str(exc)) from exc
        calls = load_recorded_calls(args.calls, recorded_keys=recorded_keys)
        bundle = load_policy_bundle(args.bundle)
        result = replay_calls(calls, bundle)
    except (ValueError, SigningKeyError, OSError) as exc:
        print(f"amw-replay: {exc}", file=sys.stderr)
        return 2

    if args.require_verified:
        unverified = sum(
            1
            for receipt in result.receipts
            if json.loads(receipt["signing_input"]).get("recorded_signature_verified")
            is not True
        )
        if unverified:
            print(
                f"amw-replay: {unverified} unverified recorded calls",
                file=sys.stderr,
            )
            return 2

    out = Path(args.out)
    try:
        out.mkdir(parents=True, exist_ok=True)
        _write_outputs_atomically(
            out,
            {
                "receipts.jsonl": "".join(
                    json.dumps(r, sort_keys=True) + "\n" for r in result.receipts
                ),
                "summary.json": json.dumps(result.summary, indent=2, sort_keys=True)
                + "\n",
                "keys.json": json.dumps({"keys": [result.public_key]}, indent=2) + "\n",
            },
        )
    except OSError as exc:
        print(f"amw-replay: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result.summary, sort_keys=True))
    return 0
