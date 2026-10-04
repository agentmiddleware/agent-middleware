"""``python -m failure_lab`` dispatches to :mod:`failure_lab.cli`.

Kept to one line of behaviour on purpose. Running a module as ``__main__``
gives it a second, distinct copy of everything it defines, so any logic that
lived here would be invisible to code that imported ``failure_lab.cli`` by
name -- and two copies of an enum or a dataclass fail ``isinstance`` against
each other in ways that are hard to read at a traceback. The command lives in
the importable module; this file only forwards to it.
"""

from __future__ import annotations

from failure_lab.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
