"""The shopping agent on the Claude Agent SDK over the lab Medusa store.

    .venv/bin/python scripts/sdk_console.py --once "a t-shirt in medium"
    .venv/bin/python scripts/sdk_console.py        # chat; 'exit' to quit

The agent runs on the Agent SDK and its own login; no API key is read. The
``[cost: $x]`` line is the SDK's list-price estimate, not a charge.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys

from claude_agent_sdk import ClaudeSDKClient  # noqa: E402
from shopping_agent import ShoppingAgentConfig  # noqa: E402
from shopping_agent_sdk import make_options, run_turn  # noqa: E402

from commerce_medusa.medusa_client import MedusaClient  # noqa: E402
from commerce_medusa.medusa_storefront import CustomerDirectory, MedusaStorefront  # noqa: E402
from commerce_medusa.order_placement import ShippingAddress  # noqa: E402
from commerce_medusa.settings import LabSettings  # noqa: E402
from commerce_medusa.stripe_checkout import StripeClient  # noqa: E402

USER_ID = os.environ.get("CUSTOMER_ID", "customer")
LAB_ADDRESS = ShippingAddress("Priya", "Lab", "1 Main St", "Austin", "78701", "US", "tx")


async def build_backend(settings: LabSettings) -> MedusaStorefront:
    client = MedusaClient(settings.medusa_url, settings.medusa_publishable_key)
    customers = CustomerDirectory()
    if settings.lab_customer_email and settings.lab_customer_password:
        await customers.login(
            client,
            USER_ID,
            settings.lab_customer_email,
            settings.lab_customer_password,
            address=LAB_ADDRESS,
        )
    region = await client.first_region_id(settings.lab_currency)
    stripe = StripeClient(settings.stripe_secret_key) if settings.stripe_secret_key else None
    return MedusaStorefront(
        client,
        region_id=region,
        customers=customers,
        policies_path=settings.data_file("policies.json"),
        stripe=stripe,
        host_url=settings.lab_host_url,
    )


def lab_config() -> ShoppingAgentConfig:
    return ShoppingAgentConfig(
        brand_name="Lab Store",
        assistant_name="the Lab Store assistant",
        brand_voice="warm, concise, and plain about trade-offs",
    )


def print_turn(result) -> None:
    if result.text:
        print(f"\n{result.text}\n")
    for event in result.ui:
        print(f"--- ui:{event['component']} ---")
        print(json.dumps(event["payload"], indent=2, default=str, ensure_ascii=False))
        for handoff in event["payload"].get("handoffs") or []:
            print(f"\n>>> {handoff.get('label', 'Checkout')}: {handoff['url']}\n")
        print()
    if result.tool_calls:
        print(f"[tools: {', '.join(result.tool_calls)}]")
    for error in result.tool_errors:
        print(f"[tool error: {error}]")
    if result.cost_usd is not None:
        print(f"[cost estimate at API list price, not a charge: ${result.cost_usd:.4f}]")


async def main_async(once: str | None) -> None:
    settings = LabSettings.load()
    if not settings.medusa_publishable_key:
        sys.exit("MEDUSA_PUBLISHABLE_KEY missing in .env; see .env.example")
    backend = await build_backend(settings)
    options, toolset = make_options(backend=backend, config=lab_config(), user_id=USER_ID)
    async with ClaudeSDKClient(options=options) as client:
        if once:
            print_turn(await run_turn(client, once, toolset=toolset))
            return
        print("Lab Store shopping agent over Medusa (Agent SDK path). Type 'exit' to quit.\n")
        while True:
            try:
                text = (await asyncio.to_thread(input, "you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text or text.lower() in {"exit", "quit", "q"}:
                break
            print_turn(await run_turn(client, text, toolset=toolset))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", metavar="QUERY", help="run a single query and exit")
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main_async(args.once))
    return 0


if __name__ == "__main__":
    sys.exit(main())
