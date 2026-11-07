"""TEST-ONLY launcher: the shipped proxy plus one injected fault.

Not part of the package. Used to record the real Goose sessions in the PR:

* ``AMW_TEST_FAULT=cut_after_commit`` - the first write runs and commits, then
  its response stream is closed before the result reaches Goose.
* ``AMW_TEST_FAULT=lose_upstream_response`` - the first write is dispatched
  to the filesystem server, then its response is dropped (outcome unknown).
"""

import os

from goose_amw.proxy import (
    Config,
    Gateway,
    UpstreamUnknown,
    build_server,
    filesystem_upstream,
)

FAULT = os.environ["AMW_TEST_FAULT"]
cfg = Config.from_env()
real = filesystem_upstream(cfg.fs_root)
fired = []


async def upstream(tool, args):
    result = await real(tool, args)
    if FAULT == "lose_upstream_response" and tool == "write_file" and not fired:
        fired.append(tool)
        raise UpstreamUnknown("test fault: upstream response dropped after dispatch")
    return result


async def after_commit(tool, key):
    if FAULT == "cut_after_commit" and not fired:
        fired.append(key)
        raise ConnectionAbortedError("test fault: response stream cut after commit")


gw = Gateway(cfg, upstream=upstream, after_commit=after_commit)
build_server(gw).run(
    transport="streamable-http", host="127.0.0.1", port=int(os.environ["AMW_PORT"])
)
