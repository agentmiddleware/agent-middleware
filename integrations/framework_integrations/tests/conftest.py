"""Make the checkout's own packages importable without install or env vars.

CI runs these tests in a dedicated job because this directory has no
pyproject.toml. This conftest also lets the tests run from a checkout by putting
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
