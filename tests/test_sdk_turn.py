"""The SDK turn adapter: one client per session, tool events re-emitted in the web
protocol, session state merged both ways, errors as events. The model is a fake runner
that drives the real toolset, so gates and enrichment run for real."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from commerce_common.agent_sdk import TurnResult
from merchant_agent import MerchantSessionContext, MerchantSessionState
from shopping_agent import ShoppingSessionContext, ShoppingSessionState

from commerce_medusa.host.sdk_turn import SdkMerchantAgent, SdkShoppingAgent, last_user_text
from commerce_medusa.host.store import merchant_config, shopping_config
from commerce_medusa.medusa_admin import MedusaAdmin
from commerce_medusa.medusa_client import MedusaClient
from commerce_medusa.medusa_merchant import MedusaMerchant
from commerce_medusa.medusa_storefront import CustomerDirectory, MedusaStorefront
from tests.test_medusa_merchant import DATA as MERCHANT_DATA
from tests.test_medusa_merchant import FakeAdmin
from tests.test_medusa_storefront import REGION, SHORTS, XL, FakeMedusa

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClient:
    """Stands in for ClaudeSDKClient: records lifecycle, never talks to a model."""

    instances: list[FakeClient] = []

    def __init__(self, options) -> None:
        self.options = options
        self.connected = False
        FakeClient.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False


def shopping_runner(calls: list[tuple[str, dict]], text: str = "Here you go."):
    async def run(client, message: str, toolset) -> TurnResult:
        toolset.begin_turn()
        result = TurnResult(text=text)
        for name, args in calls:
            outcome = await toolset.execute(name, args)
            result.tool_calls.append(name)
            result.tool_inputs.append(args)
            if outcome.is_error:
                result.tool_errors.append(f"{name} — {outcome.result_text}")
        result.cost_usd = 0.01
        return result

    return run


def make_shopping_agent(fake: FakeMedusa, runner, requests=None) -> SdkShoppingAgent:
    client = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(fake.handler)
    )
    customers = CustomerDirectory({"priya": {"token": "tok-priya", "display_name": "Priya"}})
    backend = MedusaStorefront(client, region_id=REGION, customers=customers, requests=requests)
    return SdkShoppingAgent(
        backend=backend, config=shopping_config(), client_factory=FakeClient, turn_runner=runner
    )


async def collect(agent, messages, session, state):
    return [event async for event in agent.stream_turn(messages, session, state)]


def test_last_user_text_joins_app_event_notes():
    assert last_user_text([{"role": "user", "content": "hi"}]) == "hi"
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "x"},
        {
            "role": "user",
            "content": [{"type": "text", "text": "[note]"}, {"type": "text", "text": "new"}],
        },
    ]
    assert last_user_text(messages) == "[note]\n\nnew"


async def test_search_turn_emits_tool_call_ui_cart_text_and_completion():
    FakeClient.instances.clear()
    fake = FakeMedusa()
    agent = make_shopping_agent(
        fake,
        shopping_runner(
            [
                ("search_products", {"query": "shorts"}),
                ("present_products", {"picks": [{"product_id": SHORTS["id"]}]}),
            ]
        ),
    )
    session = ShoppingSessionContext(session_id="s-1", user_id="priya")
    state = ShoppingSessionState()
    messages = [{"role": "user", "content": "shorts please"}]
    events = await collect(agent, messages, session, state)
    kinds = [e.type for e in events]
    assert kinds[0] == "tool_call" and events[0].data["tool"] == "search_products"
    assert "ui" in kinds and "cart_update" in kinds and "text_delta" in kinds
    assert kinds[-1] == "turn_complete"
    ui = next(e for e in events if e.type == "ui")
    assert ui.data["component"] == "products"
    assert ui.data["payload"]["items"][0]["product"]["product_id"] == SHORTS["id"]
    assert events[-1].data["usage"]["cost_usd_estimate"] == 0.01
    assert SHORTS["id"] in state.seen_products, "provenance flows back to the host's state"
    assert messages[-1]["role"] == "assistant"
    assert len(FakeClient.instances) == 1 and FakeClient.instances[0].connected


async def test_one_client_per_session_and_forget_disconnects():
    FakeClient.instances.clear()
    fake = FakeMedusa()
    agent = make_shopping_agent(fake, shopping_runner([]))
    a = ShoppingSessionContext(session_id="a", user_id="priya")
    b = ShoppingSessionContext(session_id="b", user_id="priya")
    await collect(agent, [{"role": "user", "content": "x"}], a, ShoppingSessionState())
    await collect(agent, [{"role": "user", "content": "y"}], a, ShoppingSessionState())
    await collect(agent, [{"role": "user", "content": "z"}], b, ShoppingSessionState())
    assert len(FakeClient.instances) == 2
    await agent.forget("a")
    assert not FakeClient.instances[0].connected
    await collect(agent, [{"role": "user", "content": "again"}], a, ShoppingSessionState())
    assert len(FakeClient.instances) == 3


async def test_host_state_provenance_reaches_the_toolset():
    fake = FakeMedusa()
    agent = make_shopping_agent(
        fake, shopping_runner([("add_to_cart", {"product_id": XL["id"], "quantity": 1})])
    )
    session = ShoppingSessionContext(session_id="s-2", user_id="priya")
    state = ShoppingSessionState()
    # The host saw the variant through a button or an earlier process: the gate must pass.
    from commerce_medusa.medusa_mapping import variant_from_medusa

    state.remember_products([variant_from_medusa(XL, SHORTS)])
    events = await collect(agent, [{"role": "user", "content": "add it"}], session, state)
    results = [e for e in events if e.type == "tool_result" and e.data["tool"] == "add_to_cart"]
    assert results and results[0].data["status"] == "ok"
    assert any(e.type == "cart_update" and e.data["cart"]["item_count"] == 1 for e in events)


async def test_runner_failure_becomes_an_error_event_and_drops_the_client():
    FakeClient.instances.clear()

    async def boom(client, text, toolset):
        raise RuntimeError("subprocess died")

    agent = make_shopping_agent(FakeMedusa(), boom)
    session = ShoppingSessionContext(session_id="s-3", user_id="priya")
    events = await collect(
        agent, [{"role": "user", "content": "hi"}], session, ShoppingSessionState()
    )
    assert [e.type for e in events] == ["error"]
    assert "subprocess died" in events[0].data["message"]
    assert not FakeClient.instances[0].connected


def merchant_runner(calls: list[tuple[str, dict]]):
    async def run(client, message: str, toolset) -> TurnResult:
        toolset.begin_turn()
        result = TurnResult(text="Staged.")
        for name, args in calls:
            await toolset.execute(name, args)
            result.tool_calls.append(name)
            result.tool_inputs.append(args)
        return result

    return run


async def test_merchant_turn_emits_change_update_and_syncs_approvals():
    fake = FakeAdmin()
    client = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(fake.handler)
    )
    backend = MedusaMerchant(
        MedusaAdmin(client, email="a@b", password="pw"),
        config=merchant_config(),
        fixtures_dir=MERCHANT_DATA,
    )
    await backend._load()
    decals = next(p for p in fake.products.values() if p["handle"] == "ar-2102")
    agent = SdkMerchantAgent(
        backend=backend,
        config=backend.config,
        client_factory=FakeClient,
        turn_runner=merchant_runner(
            [
                ("search_listings", {"query": "ocean wall decals"}),
                ("get_listing", {"listing_id": decals["id"]}),
                (
                    "stage_inventory_action",
                    {"items": [{"listing_id": decals["id"], "action": "restock", "quantity": 20}]},
                ),
            ]
        ),
    )
    session = MerchantSessionContext(session_id="m-1", merchant_id="lab-store", operator="vishal")
    state = MerchantSessionState()
    events = await collect(
        agent, [{"role": "user", "content": "restock the decals"}], session, state
    )
    changes = [e for e in events if e.type == "change_update"]
    assert changes and changes[0].data["change"]["status"] == "staged"
    change_id = changes[0].data["change"]["change_id"]
    assert change_id in state.seen_changes, "the host's state learns the staged change"
    assert decals["id"] in state.seen_listings
    # The host approves on its side; the next turn's toolset must see the mark.
    state.approved_change_ids.add(change_id)
    conversation = agent._conversations["m-1"]
    agent._sync_in(conversation.toolset, state)
    assert change_id in conversation.toolset.state.approved_change_ids


# -- the lab's order-action tool on the SDK path ----------------------------------


async def test_order_action_tool_needs_the_order_read_first_then_records(tmp_path):
    from commerce_medusa.order_requests import OrderRequestStore
    from tests.test_medusa_storefront import ORDERS

    order_id = ORDERS["orders"][0]["id"]
    store = OrderRequestStore(tmp_path / "requests.sqlite")
    calls = [
        ("request_order_action", {"order_id": order_id, "action": "cancel", "reason": "no"}),
        ("get_orders", {}),
        ("request_order_action", {"order_id": order_id, "action": "cancel", "reason": "no"}),
        ("get_order_requests", {}),
    ]
    agent = make_shopping_agent(FakeMedusa(), shopping_runner(calls), requests=store)
    session = ShoppingSessionContext(session_id="s-req", user_id="priya")
    events = await collect(
        agent, [{"role": "user", "content": "cancel my order"}], session, ShoppingSessionState()
    )
    outcomes = [e for e in events if e.type == "tool_result"]
    requests = [e for e in outcomes if e.data["tool"] == "request_order_action"]
    assert requests[0].data["reason"] == "provenance", requests[0].data
    assert requests[1].data["status"] != "blocked" and not requests[1].data["is_error"]
    assert store.open() and store.open()[0].order_id == order_id
    listed = next(e for e in outcomes if e.data["tool"] == "get_order_requests")
    assert not listed.data["is_error"]
    tool_names = {t for t in agent.allowed_tools() if "request_order_action" in t}
    assert tool_names, "the lab tool is on the allow-list the SDK enforces"


# -- memory extraction and mid-turn streaming on the SDK path ------------------------


class TalkingFakeClient(FakeClient):
    """A FakeClient whose receive_response yields canned assistant text, so the memory
    shim (a tool-less query) can be driven without a model."""

    replies: list[str] = []

    async def query(self, text: str) -> None:
        self.asked = text

    async def receive_response(self):
        from types import SimpleNamespace

        text = TalkingFakeClient.replies.pop(0) if TalkingFakeClient.replies else ""
        yield SimpleNamespace(content=[SimpleNamespace(text=text)])


async def test_memory_is_extracted_from_the_last_exchange_on_the_sdk_path():
    from commerce_medusa.host.sdk_turn import SdkMessagesShim

    TalkingFakeClient.replies = [
        '{"facts": [{"key": "colour", "value": "prefers blush bedding", "category": "preference"}]}'
    ]
    fake = FakeMedusa()
    client = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(fake.handler)
    )
    customers = CustomerDirectory({"priya": {"token": "tok-priya", "display_name": "Priya"}})
    backend = MedusaStorefront(client, region_id=REGION, customers=customers)
    agent = SdkShoppingAgent(
        backend=backend,
        config=shopping_config(),
        client_factory=TalkingFakeClient,
        turn_runner=shopping_runner([], text="Blush it is."),
        extract_memory=True,
    )
    session = ShoppingSessionContext(session_id="s-mem", user_id="priya")
    messages = [{"role": "user", "content": "I prefer blush bedding"}]
    events = await collect(agent, messages, session, ShoppingSessionState())
    assert events[-1].type == "turn_complete"
    await agent.update_memory(messages, session)  # the host spawns this after the stream
    facts = await agent.memory.store.get_facts("priya")
    assert [f.value for f in facts] == ["prefers blush bedding"]
    shim = SdkMessagesShim(TalkingFakeClient, agent_options=None)
    assert (
        shim.transcript_of(
            [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
                {"role": "assistant", "content": "four"},
            ]
        )
        == "Customer: three\nAssistant: four"
    ), "only the last exchange, never tool results"


async def test_text_streams_before_the_turn_completes():
    """The streaming runner hands text out as it arrives; the host sees text_delta events
    before turn_complete and no duplicate at the end."""
    from commerce_medusa.host.sdk_turn import streaming_shopping_runner

    class StreamingClient(FakeClient):
        async def query(self, text: str) -> None:
            self.asked = text

        async def receive_response(self):
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

            yield AssistantMessage(content=[TextBlock(text="Three tents ")], model="m")
            yield AssistantMessage(content=[TextBlock(text="fit your budget.")], model="m")
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="x",
                total_cost_usd=0.02,
            )

    agent = make_shopping_agent(FakeMedusa(), streaming_shopping_runner)
    agent._client_factory = StreamingClient
    session = ShoppingSessionContext(session_id="s-stream", user_id="priya")
    events = await collect(
        agent, [{"role": "user", "content": "tents"}], session, ShoppingSessionState()
    )
    kinds = [e.type for e in events]
    deltas = [e.data["text"] for e in events if e.type == "text_delta"]
    assert deltas == ["Three tents ", "fit your budget."], deltas
    assert kinds.index("text_delta") < kinds.index("turn_complete")
    assert events[-1].data["usage"]["cost_usd_estimate"] == 0.02


# -- fulfillment choice, promo codes, waitlist: the lab's shopper tools on the SDK path ------------


async def test_promo_fulfillment_and_waitlist_tools_run_through_the_toolset(tmp_path):
    from commerce_medusa.medusa_storefront import CustomerDirectory as Directory
    from commerce_medusa.order_requests import WaitlistStore

    fake = FakeMedusa()
    fake.promo_codes = {"LAB10": 10}
    fake.out_of_stock.add(SHORTS["variants"][2]["id"])
    client = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(fake.handler)
    )
    backend = MedusaStorefront(
        client,
        region_id=REGION,
        customers=Directory({"priya": {"token": "tok-priya", "display_name": "Priya"}}),
        waitlist=WaitlistStore(tmp_path / "w.sqlite"),
    )
    calls = [
        ("get_product_details", {"product_id": SHORTS["id"]}),
        ("add_to_cart", {"product_id": XL["id"], "quantity": 1}),
        ("apply_promo_code", {"code": "lab10"}),
        ("apply_promo_code", {"code": "NOPE"}),
        ("get_fulfillment_options", {"product_ids": [XL["id"]]}),
        ("choose_fulfillment", {"option": "express"}),
        ("subscribe_availability", {"product_id": SHORTS["variants"][2]["id"]}),
        ("subscribe_availability", {"product_id": "variant_never_seen"}),
    ]
    agent = SdkShoppingAgent(
        backend=backend,
        config=shopping_config(),
        client_factory=FakeClient,
        turn_runner=shopping_runner(calls),
    )
    session = ShoppingSessionContext(session_id="s-tools", user_id="priya")
    events = await collect(
        agent, [{"role": "user", "content": "go"}], session, ShoppingSessionState()
    )
    results = {(e.data["tool"], i): e.data for i, e in enumerate(events) if e.type == "tool_result"}
    by_tool = {}
    for (tool, _), data in results.items():
        by_tool.setdefault(tool, []).append(data)
    assert not by_tool["apply_promo_code"][0]["is_error"], by_tool["apply_promo_code"][0]
    assert (
        by_tool["apply_promo_code"][1]["is_error"]
        and "NOPE" in by_tool["apply_promo_code"][1]["summary"]
    )
    assert not by_tool["choose_fulfillment"][0]["is_error"]
    assert not by_tool["subscribe_availability"][0]["is_error"]
    assert by_tool["subscribe_availability"][1]["reason"] == "provenance", "unseen id is held"
    cart = await backend.get_cart(session)
    assert cart.items[0].price == pytest.approx(9.0)
    assert backend.waitlist.counts() == {SHORTS["variants"][2]["id"]: 1}
    names = agent.allowed_tools()
    assert all(
        any(n.endswith(t) for n in names)
        for t in ("apply_promo_code", "choose_fulfillment", "subscribe_availability")
    )


async def test_merchant_text_streams_before_the_turn_completes():
    from commerce_medusa.host.sdk_turn import streaming_merchant_runner
    from tests.test_medusa_merchant import DATA as MERCHANT_DATA
    from tests.test_medusa_merchant import FakeAdmin

    class StreamingClient(FakeClient):
        async def query(self, text: str) -> None:
            self.asked = text

        async def receive_response(self):
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

            yield AssistantMessage(content=[TextBlock(text="Sales are ")], model="m")
            yield AssistantMessage(content=[TextBlock(text="up 4%.")], model="m")
            yield ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id="x",
                total_cost_usd=0.01,
            )

    fake = FakeAdmin()
    client = MedusaClient(
        "http://medusa.test", "pk_test", transport=httpx.MockTransport(fake.handler)
    )
    from merchant_agent import MerchantSessionContext, MerchantSessionState

    from commerce_medusa.medusa_admin import MedusaAdmin
    from commerce_medusa.medusa_merchant import MedusaMerchant

    backend = MedusaMerchant(
        MedusaAdmin(client, email="a@b", password="pw"),
        config=merchant_config(),
        fixtures_dir=MERCHANT_DATA,
    )
    agent = SdkMerchantAgent(
        backend=backend,
        config=backend.config,
        client_factory=StreamingClient,
        turn_runner=streaming_merchant_runner,
    )
    session = MerchantSessionContext(session_id="m-stream", merchant_id="lab-store", operator="v")
    events = await collect(
        agent, [{"role": "user", "content": "how are sales?"}], session, MerchantSessionState()
    )
    kinds = [e.type for e in events]
    assert [e.data["text"] for e in events if e.type == "text_delta"] == ["Sales are ", "up 4%."]
    assert kinds.index("text_delta") < kinds.index("turn_complete")
