"""Importing the DI spine must not import proof-surface services.

get_* getters back routers that stay unmounted unless ENABLE_PROOF_SURFACES
is true. Importing app.core.dependencies used to import those service
modules (IoT, media, telemetry, oracle, ...) at module import time, so a
production-posture boot paid for dormant surfaces. Service modules now load
lazily inside each getter. A clean interpreter proves the import-time
boundary; the existing router tests prove the getters still resolve.
"""

import subprocess
import sys
from pathlib import Path

GATEWAY_ROOT = Path(__file__).resolve().parent.parent


def test_dependencies_import_loads_no_service_modules():
    probe = (
        "import sys; "
        "import app.core.dependencies; "
        "loaded = sorted(m for m in sys.modules if m.startswith('app.services.')); "
        "assert not loaded, loaded"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=GATEWAY_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr


def test_getters_still_resolve_after_lazy_import():
    from app.core import dependencies

    assert callable(dependencies.get_agent_comms)
    assert callable(dependencies.get_rtaas_engine)
    assert callable(dependencies.get_iot_bridge)
    assert dependencies.get_agent_comms() is dependencies.get_agent_comms()
