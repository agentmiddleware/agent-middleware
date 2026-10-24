"""Make the checkout's own packages importable without install or env vars.

CI only executes pytest inside directories containing pyproject.toml, so this
directory (framework_integrations, no pyproject) is not a CI project. This
conftest at least lets the tests run anywhere with a plain `pytest` by putting
the checkout's `integrations/` (for `framework_integrations`) and
`sdk/python/src` (for `b2a_sdk`) first on `sys.path`, ahead of any other
checkout's SDK that a shared interpreter may resolve.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _entry in (str(_ROOT / "integrations"), str(_ROOT / "sdk/python/src")):
    while _entry in sys.path:
        sys.path.remove(_entry)
    sys.path.insert(0, _entry)
