import pytest
from app.db.database import get_session_factory
from app.db.models import WalletModel
from app.services import audit_chain
from app.services.audit_log import record_audit_event


@pytest.mark.parametrize("initial_events", [0, 1])
async def test_append_during_verification_does_not_report_truncation(
    monkeypatch, clean_database, initial_events
):
    factory = get_session_factory()
    async with factory() as session:
        session.add(WalletModel(wallet_id="qa_snapshot", wallet_type="agent"))
        await session.commit()
    if initial_events:
        await record_audit_event(event="qa.first", wallet_id="qa_snapshot")
    assert (await audit_chain.verify_audit_chain(wallet_id="qa_snapshot")).valid
    injected = False

    class InterleavedSession:
        def __init__(self):
            self.inner = factory()

        async def __aenter__(self):
            await self.inner.__aenter__()
            return self

        async def __aexit__(self, *args):
            return await self.inner.__aexit__(*args)

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def execute(self, stmt, *args, **kwargs):
            nonlocal injected
            result = await self.inner.execute(stmt, *args, **kwargs)
            if not injected:
                injected = True
                await record_audit_event(event="qa.concurrent", wallet_id="qa_snapshot")
            return result

    with monkeypatch.context() as patch:
        patch.setattr(audit_chain, "get_session_factory", lambda: InterleavedSession)
        during = await audit_chain.verify_audit_chain(wallet_id="qa_snapshot")
    after = await audit_chain.verify_audit_chain(wallet_id="qa_snapshot")
    assert injected and after.valid and after.checked_events == initial_events + 1
    assert during.valid, {"during": during, "after": after}
