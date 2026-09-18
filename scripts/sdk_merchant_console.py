"""The merchant agent on the Claude Agent SDK over the lab Medusa store.

    .venv/bin/python scripts/sdk_merchant_console.py --once "What needs my attention this morning?"
    .venv/bin/python scripts/sdk_merchant_console.py      # chat; approves staged changes with y/N

Every staged change is shown and applied only after the operator types ``y``; the
approval mark is what ``apply_change`` checks. The agent runs on the Agent SDK and its
own login; no API key is read. The cost line is the SDK's list-price estimate, not a charge.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeSDKClient  # noqa: E402
from merchant_agent import MerchantAgentConfig  # noqa: E402
from merchant_agent_sdk import make_options, run_turn  # noqa: E402

from commerce_medusa.medusa_admin import MedusaAdmin  # noqa: E402
from commerce_medusa.medusa_client import MedusaClient  # noqa: E402
from commerce_medusa.medusa_merchant import MedusaMerchant  # noqa: E402
from commerce_medusa.settings import LabSettings  # noqa: E402

DATA = Path(os.environ.get("COMMERCE_AGENTS", "")).expanduser() / "examples" / "retail" / "data"
OPERATOR = os.environ.get("OPERATOR", "operator")


def lab_config() -> MerchantAgentConfig:
    return MerchantAgentConfig(
        brand_name="Lab Store",
        assistant_name="the Lab Store merchant assistant",
        enable_analysis=False,
    )


def build_backend(settings: LabSettings, config: MerchantAgentConfig) -> MedusaMerchant:
    client = MedusaClient(settings.medusa_url, settings.medusa_publishable_key)
    admin = MedusaAdmin(client, settings.medusa_admin_email, settings.medusa_admin_password)
    return MedusaMerchant(admin, config=config, currency=settings.lab_currency, fixtures_dir=DATA)


def print_turn(result) -> None:
    if result.text:
        print(f"\n{result.text}\n")
    for event in result.ui:
        print(f"--- ui:{event['component']} ---")
        print(json.dumps(event["payload"], indent=2, default=str, ensure_ascii=False))
        print()
    if result.tool_calls:
        print(f"[tools: {', '.join(result.tool_calls)}]")
    for error in result.tool_errors:
        print(f"[tool error: {error}]")
    if result.cost_usd is not None:
        print(f"[cost estimate at API list price, not a charge: ${result.cost_usd:.4f}]")


async def approve_staged(toolset, backend: MedusaMerchant, auto: bool) -> None:
    for change in backend.ledger.pending():
        if change.change_id in toolset.state.approved_change_ids:
            continue
        print(f"staged {change.change_id} [{change.kind}]: {change.summary}")
        for item in change.items:
            print(f"   {item.target} {item.field}: {item.before} -> {item.after}")
        if auto:
            answer = "y"
            print("   auto-approved (--approve-all)")
        else:
            answer = (await asyncio.to_thread(input, "   approve? [y/N] ")).strip().lower()
        if answer == "y":
            # The host's approval surface: mark it approved and apply it, as the upstream
            # portal's card button does, so the write reaches Medusa without another turn.
            toolset.host_approve(change.change_id)
            applied = await backend.apply_change(toolset.session, change.change_id)
            stamp = applied.applied_at.strftime("%H:%M:%S") if applied.applied_at else ""
            print(f"   applied {applied.change_id} by {applied.applied_by} at {stamp}")


async def main_async(once: str | None, approve_all: bool) -> None:
    settings = LabSettings.load()
    if not settings.medusa_admin_email:
        sys.exit("MEDUSA_ADMIN_EMAIL/PASSWORD missing in .env")
    config = lab_config()
    backend = build_backend(settings, config)
    options, toolset = make_options(backend=backend, config=config, operator=OPERATOR)
    async with ClaudeSDKClient(options=options) as client:
        if once:
            print_turn(await run_turn(client, once, toolset=toolset))
            await approve_staged(toolset, backend, approve_all)
            return
        print("Lab Store merchant agent over Medusa (Agent SDK path). Type 'exit' to quit.\n")
        while True:
            try:
                text = (await asyncio.to_thread(input, "you> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text or text.lower() in {"exit", "quit", "q"}:
                break
            print_turn(await run_turn(client, text, toolset=toolset))
            await approve_staged(toolset, backend, approve_all)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", metavar="QUERY")
    parser.add_argument(
        "--approve-all", action="store_true", help="mark every staged change approved"
    )
    args = parser.parse_args()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main_async(args.once, args.approve_all))
    return 0


if __name__ == "__main__":
    sys.exit(main())
