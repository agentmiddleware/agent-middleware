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

    parser = argparse.ArgumentParser(prog="amw")
    parser.add_argument("command", choices=["verify"], help="Verify a receipt")
    parser.parse_args(args)
    return 0
