"""
Dry-Run Sandbox Example
=======================

Demonstrates how agents can safely test billing operations without
affecting real wallet balances or triggering velocity monitoring.

.. note::
   The dry-run endpoints live on the billing router, which is a **proof
   surface**: production-like deploys keep ``ENABLE_PROOF_SURFACES=false`` and
   do not mount it. Run the API with ``ENABLE_PROOF_SURFACES=true`` or every
   call here returns 404. See ``docs/PROOF_SURFACES.md``.

Usage:
    ENABLE_PROOF_SURFACES=true uvicorn app.main:app     # in another shell
    B2A_API_KEY=<bootstrap-key> python examples/dry_run_example.py
"""

import asyncio
import os
import sys

# The SDK sources live under b2a_sdk/src; inserting the repo root instead makes
# "b2a_sdk" resolve to the bare directory and the import fails.
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "b2a_sdk", "src"
    ),
)

from b2a_sdk import AgentMiddlewareClient
from b2a_sdk.decorators import billable


b2a = AgentMiddlewareClient(
    api_key=os.getenv("B2A_API_KEY", "test-key"),
    base_url=os.getenv("B2A_API_URL", "http://localhost:8000"),
)


async def demo_simulation():
    """Demonstrate dry-run simulation."""
    print("\n" + "=" * 60)
    print("B2A Dry-Run Sandbox Demo")
    print("=" * 60)

    # The server assigns the wallet id (e.g. "spn-bde42b5c4606"); it cannot be
    # chosen by the caller. Everything below bills against this exact id —
    # using a made-up one returns 404 wallet_not_found.
    wallet = await b2a.create_sponsor_wallet(
        sponsor_name="Demo Sponsor",
        email="demo@example.com",
        initial_credits=10000.0,
    )
    wallet_id = wallet["wallet_id"]
    print(f"\n✓ Created sponsor wallet {wallet_id} with 10,000 credits")

    # @billable captures wallet_id at decoration time, so these are defined
    # here rather than at import, where the wallet does not exist yet.
    @billable(b2a, wallet_id=wallet_id, service_category="content_factory", units=10.0)
    async def generate_video_hook(url: str, style: str = "cinematic") -> dict:
        """Simulate a video generation call."""
        return {
            "video_url": f"https://example.com/{hash(url)}.mp4",
            "style": style,
            "status": "generated",
        }

    @billable(b2a, wallet_id=wallet_id, service_category="media_engine", units=5.0)
    async def distribute_clip(clip_id: str, platform: str = "youtube") -> dict:
        """Simulate a clip distribution call."""
        return {
            "clip_id": clip_id,
            "platform": platform,
            "status": "distributed",
        }

    @billable(b2a, wallet_id=wallet_id, service_category="iot_bridge", units=1.0)
    async def send_iot_message(device_id: str, message: str) -> dict:
        """Simulate an IoT message send."""
        return {
            "device_id": device_id,
            "message": message,
            "status": "sent",
        }

    print("\n--- Scenario 1: Estimate Cost of Multi-Step Workflow ---")
    print("Agent wants to: generate_video → distribute_clip → send_iot_message")
    print("But doesn't know if it fits the budget...")

    async with b2a.simulate_session(wallet_id=wallet_id) as sim:
        print(f"\nSession started: {sim.session_id}")
        print(f"Initial virtual balance: {sim.total_cost} credits")

        result1 = await generate_video_hook("https://example.com/video1.mp4")
        print("\n1. Simulated generate_video:")
        print(f"   Result: {result1}")
        print(f"   Session total: {sim.total_cost} credits")

        result2 = await distribute_clip("clip-123", platform="tiktok")
        print("\n2. Simulated distribute_clip:")
        print(f"   Result: {result2}")
        print(f"   Session total: {sim.total_cost} credits")

        result3 = await send_iot_message("device-001", "status=ok")
        print("\n3. Simulated send_iot_message:")
        print(f"   Result: {result3}")
        print(f"   Session total: {sim.total_cost} credits")

        print(f"\n{'=' * 60}")
        print("SIMULATION SUMMARY")
        print(f"{'=' * 60}")
        print(f"Total estimated cost: {sim.total_cost} credits")
        print(f"Number of operations: {sim.charge_count}")
        print(f"Would succeed: {sim.would_succeed}")

        if sim.would_succeed:
            print(
                "\n✅ Agent decides: Budget is sufficient. Proceeding with real execution!"
            )
        else:
            print("\n❌ Agent decides: Insufficient budget. Need to top up first.")

    print("\n--- Scenario 2: Single-Shot Estimation ---")
    print("Quick estimate without session tracking:")

    result = await b2a.simulate_charge(
        wallet_id=wallet_id,
        service_category="content_factory",
        units=100.0,
        description="Bulk video generation",
    )

    print("\nSingle charge simulation:")
    print(f"  Would charge: {result['credits_would_charge']} credits")
    print(f"  Virtual balance before: {result['simulated_balance_before']}")
    print(f"  Virtual balance after: {result['simulated_balance_after']}")
    print(f"  Would succeed: {result['would_succeed']}")

    print("\n--- Scenario 3: Compare Workflows ---")
    print("Comparing two different workflow strategies...")

    workflow_a_total = 0
    async with b2a.simulate_session(wallet_id=wallet_id) as sim:
        for i in range(3):
            await generate_video_hook(f"url-{i}")
        workflow_a_total = sim.total_cost

    workflow_b_total = 0
    async with b2a.simulate_session(wallet_id=wallet_id) as sim:
        for i in range(3):
            await send_iot_message(f"device-{i}", "ping")
        workflow_b_total = sim.total_cost

    print(f"\nWorkflow A (3x video generation): {workflow_a_total} credits")
    print(f"Workflow B (3x IoT messages): {workflow_b_total} credits")

    if workflow_a_total < workflow_b_total:
        print("\n✅ Workflow A is more cost-effective!")
    else:
        print("\n✅ Workflow B is more cost-effective!")

    print("\n" + "=" * 60)
    print("Demo complete!")
    print("=" * 60)


async def main():
    try:
        await demo_simulation()
    except Exception as e:
        print(f"\nError: {e}")
        print("Make sure the B2A API is running at http://localhost:8000")
        print("Or set B2A_API_URL environment variable.")


if __name__ == "__main__":
    asyncio.run(main())
