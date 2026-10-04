"""Agent Gateway Failure Lab.

An independent harness that drives the Agent Middleware API under realistic
failure conditions -- lost responses, severed connections, crashes at durable
boundaries, concurrent retries, budget races, revocation races -- and measures
what actually happened against a downstream system of record the gateway
cannot reach.

Three things make its numbers evidence rather than assertion:

* The **independent effect ledger** (:mod:`failure_lab.effect_ledger`) is a
  separate SQLite file owned by the simulated downstream tool. The gateway has
  no handle to it. Every downstream execution is a row; duplicates are rows.
* The **fault injection layer** (:mod:`failure_lab.faults`) sits between the
  gateway and the tool and counts every request that crosses it, so "gateway
  dispatches" is observed from outside the gateway, not read from its tables.
* Every scenario runs the **same workload and the same injected failure**
  against several configurations (:mod:`failure_lab.configurations`),
  including a correct native baseline that needs no gateway at all. A
  scenario the baseline already handles is reported as exactly that.

The lab never contacts production, never uses production credentials, and
writes only under a run directory it creates and deletes.
"""

from __future__ import annotations

__version__ = "0.1.0"

# Version of the scenario definitions. Bump when a scenario's workload,
# injected failure, or verdict rule changes so historical claims manifests
# cannot be compared against a different test by accident.
TEST_DEFINITION_VERSION = "2026.09.1"

__all__ = ["TEST_DEFINITION_VERSION", "__version__"]
