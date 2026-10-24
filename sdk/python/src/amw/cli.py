"""Agent Middleware command line entry point."""

from __future__ import annotations

import argparse
import sys

from . import _load_sdk


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args and args[0] == "verify":
        _load_sdk()
        from b2a_sdk.verify_cli import main as verify_main

        return verify_main(args[1:])
    if args and args[0] == "replay":
        # Policy replay re-runs the gateway's own decision core, so it needs
        # the gateway package (agent-native-middleware) importable.
        try:
            from app.replay.cli import main as replay_main
        except ImportError:
            print(
                "amw replay needs the gateway package installed (pip install -e gateway)",
                file=sys.stderr,
            )
            return 2
        return replay_main(args[1:])

    parser = argparse.ArgumentParser(prog="amw")
    parser.add_argument(
        "command",
        choices=["verify", "replay"],
        help="verify: check a receipt; replay: re-decide recorded calls",
    )
    parser.parse_args(args)
    return 0
