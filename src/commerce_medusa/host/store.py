"""The store host: the upstream storefront routes and merchant router, over Medusa,
with both agents on the Claude Agent SDK, plus the Stripe checkout routes.

    uvicorn --factory commerce_medusa.host.store:default_app --port 8010

Point the upstream web apps at it:

    cd upstream/examples/retail/storefront-web
    NEXT_PUBLIC_API_URL=http://localhost:8010 npx next dev --port 3000   # portal: 3100

Routes (all from ``demo_common``): ``/api/session``, ``/api/chat`` (SSE), ``/api/products``,
``/api/cart``, ``/api/orders``, ``/api/memory``, ``/api/reset``, ``/api/health``, and under
``/api/merchant``: ``/session``, ``/chat``, ``/overview``, ``/listings``, ``/alerts``,
``/changes/{id}/apply|discard``, ``/reset``, ``/health``. Plus ``/webhooks/stripe``,
``/checkout/success``, ``/checkout/cancel`` from ``host.app``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any

from commerce_common.memory import JsonFileMemoryStore  # noqa: E402
from fastapi import FastAPI, HTTPException, Request
from merchant_agent import MerchantAgentConfig, MerchantSessionContext  # noqa: E402
from merchant_agent.changes import ChangeNotApplicable  # noqa: E402
from shopping_agent import ShoppingAgentConfig  # noqa: E402
from shopping_agent.serialization import cart_payload  # noqa: E402

from commerce_medusa.analysis import AnalysisReplica  # noqa: E402
from commerce_medusa.host.app import (  # noqa: E402
    HostServices,
    ProcessedEvents,
    default_address,
    install_checkout_routes,
)
from commerce_medusa.host.sdk_turn import SdkMerchantAgent, SdkShoppingAgent  # noqa: E402
from commerce_medusa.medusa_admin import MedusaAdmin  # noqa: E402
from commerce_medusa.medusa_client import MedusaClient  # noqa: E402
from commerce_medusa.medusa_merchant import MedusaMerchant  # noqa: E402
from commerce_medusa.medusa_storefront import CustomerDirectory, MedusaStorefront  # noqa: E402
from commerce_medusa.order_placement import PlacedOrder  # noqa: E402
from commerce_medusa.order_requests import OrderRequestStore, WaitlistStore  # noqa: E402
from commerce_medusa.sessions import SqliteCartMap, SqliteSessionStore  # noqa: E402
from commerce_medusa.settings import LabSettings  # noqa: E402
from commerce_medusa.stripe_checkout import StripeClient  # noqa: E402
from demo_common import (  # noqa: E402
    MemorySeeder,
    MerchantIdentity,
    SessionConflictError,
    UnknownSessionError,
    build_merchant_router,
    build_storefront_host,
)

logger = logging.getLogger("commerce_medusa.host.store")
STORE_NAME = os.environ.get("STORE_NAME", "Demo Store")
# The reference storefront's profile picker names these; both act as the single customer.
PROFILE_ALIASES = ("demo-user", "demo-user-2")


def shopping_config() -> ShoppingAgentConfig:
    return ShoppingAgentConfig(
        brand_name=STORE_NAME,
        assistant_name="the Lab Store assistant",
        brand_voice="warm, concise, and plain about trade-offs",
        enable_disclosures=True,  # facts boxes authored on the listing
    )


def merchant_config() -> MerchantAgentConfig:
    return MerchantAgentConfig(
        brand_name=STORE_NAME,
        assistant_name="the Lab Store merchant assistant",
        enable_analysis=False,
    )


def install_cart_merge_route(app: FastAPI, host: Any, storefront: MedusaStorefront) -> None:
    """Guest-cart merge: after sign-in the host starts a new session and asks the store to carry the
    guest session's lines over, once."""

    @app.post("/api/cart/merge")
    async def merge_cart(request: Request) -> dict[str, Any]:
        body = await request.json()
        try:
            record = host.sessions.require(request.headers.get("X-Session-Id", ""))
        except (KeyError, ValueError, LookupError) as unknown:
            raise HTTPException(401, "no session") from unknown
        session = host.context(record)
        cart = await storefront.merge_guest_cart(
            session, from_session_id=str(body.get("from_session_id") or "")
        )
        return cart_payload(cart)


def install_order_request_routes(app: FastAPI, merchant: MedusaMerchant, *, operator: str) -> None:
    """The operator's decision on a shopper's order request: a portal action, like
    approving a change, never the assistant's."""

    @app.get("/api/merchant/waitlist")
    async def waitlist_counts() -> dict[str, Any]:
        """Who is waiting for what, per product; the notification is the host's."""
        return {"waitlist": merchant.waitlist.counts() if merchant.waitlist else {}}

    @app.get("/api/merchant/requests")
    async def list_requests() -> dict[str, Any]:
        rows = merchant.requests.open() if merchant.requests else []
        return {"requests": [r.model_dump(mode="json") for r in rows]}

    @app.post("/api/merchant/requests/{request_id}/{decision}")
    async def resolve_request(request_id: str, decision: str, note: str = "") -> dict[str, Any]:
        if decision not in {"approved", "declined"}:
            raise HTTPException(400, "decision is approved or declined")
        session = MerchantSessionContext(
            session_id="portal", merchant_id=merchant.merchant_id, operator=operator
        )
        try:
            resolved = await merchant.resolve_order_request(session, request_id, decision, note)
        except ChangeNotApplicable as refused:
            raise HTTPException(409, str(refused)) from refused
        return {"request": resolved.model_dump(mode="json")}


def money(placed: PlacedOrder) -> str:
    symbol = {"usd": "$", "eur": "€", "gbp": "£"}.get((placed.currency or "").lower())
    return (
        f"{symbol}{placed.total:.2f}" if symbol else f"{placed.total:.2f} {placed.currency.upper()}"
    )


@contextmanager
def durable_sessions(path: Path | None) -> Iterator[None]:
    """While active, ``demo_common``'s hosts get a ``SqliteSessionStore`` over ``path``
    wherever they would construct their in-memory ``SessionStore``; ``None`` keeps the
    in-memory store (tests that do not care)."""
    if path is None:
        yield
        return
    import demo_common.merchant as merchant_module
    import demo_common.storefront as storefront_module

    def factory(state_type: type[Any]) -> SqliteSessionStore:
        return SqliteSessionStore(state_type, path)

    originals = (storefront_module.SessionStore, merchant_module.SessionStore)
    storefront_module.SessionStore = factory  # type: ignore[assignment,misc]
    merchant_module.SessionStore = factory  # type: ignore[assignment,misc]
    try:
        yield
    finally:
        storefront_module.SessionStore, merchant_module.SessionStore = originals  # type: ignore[assignment,misc]


def build_store_app(
    settings: LabSettings,
    *,
    medusa: MedusaClient,
    region_id: str,
    customers: CustomerDirectory,
    stripe: StripeClient | None,
    storefront: MedusaStorefront | None = None,
    merchant: MedusaMerchant | None = None,
    shopping_agent: SdkShoppingAgent | None = None,
    merchant_agent: SdkMerchantAgent | None = None,
    events_path: Path | None = None,
    on_startup: list[Any] | None = None,
    sessions_path: Path | None = None,
) -> FastAPI:
    data = settings.data_dir
    data.mkdir(parents=True, exist_ok=True)
    requests = OrderRequestStore(data / "requests.sqlite")  # shoppers' order requests
    waitlist = WaitlistStore(data / "requests.sqlite")  # who waits for stock
    analysis = (  # the read-only replica, once scripts/setup_analysis_replica.py has run
        AnalysisReplica(settings.database_url_ro, merchant_config())
        if settings.database_url_ro
        else None
    )
    storefront = storefront or MedusaStorefront(
        medusa,
        region_id=region_id,
        customers=customers,
        policies_path=settings.data_file("policies.json"),
        store_name=STORE_NAME,
        stripe=stripe,
        host_url=settings.lab_host_url,
        requests=requests,
        waitlist=waitlist,
        carts=SqliteCartMap(sessions_path) if sessions_path else None,  # durable
    )
    admin = MedusaAdmin(medusa, settings.medusa_admin_email, settings.medusa_admin_password)
    merchant = merchant or MedusaMerchant(
        admin,
        config=merchant_config(),
        currency=settings.lab_currency,
        store_name=STORE_NAME,
        fixtures_dir=settings.fixtures_dir,
        ledger_path=data / "ledger.sqlite",  # survives restarts
        requests=requests,
        waitlist=waitlist,
        analysis=analysis,
    )
    shopping_agent = shopping_agent or SdkShoppingAgent(
        backend=storefront,
        config=shopping_config(),
        memory_store=JsonFileMemoryStore(data / ".memory-store.json"),
        extract_memory=True,  # the post-turn pass on the SDK path
    )
    merchant_agent = merchant_agent or SdkMerchantAgent(
        backend=merchant,
        config=merchant.config,
        memory_store=JsonFileMemoryStore(data / ".merchant-memory-store.json"),
    )
    storefront.on_reset = shopping_agent.forget

    # Sessions survive a restart. Upstream's hosts construct ``SessionStore`` themselves
    # with no way to hand one in, so the durable store is substituted for the constructor
    # while the host and the router are built (a `sessions=` parameter is proposed upstream).
    with durable_sessions(sessions_path):
        host = build_storefront_host(
            title="Lab Store API",
            example_root=data,
            backend=storefront,
            agent=shopping_agent,
            memory_seeder=MemorySeeder(
                settings.data_file("memory-seed.json"), marker=data / ".memory-seeded.json"
            ),
        )
        app = host.app
        app.include_router(
            build_merchant_router(
                storefront=storefront,
                backend=merchant,
                agent=merchant_agent,
                identity=MerchantIdentity(
                    merchant_id=merchant.merchant_id, operator=settings.operator
                ),
                example_dir="commerce_medusa",
            ),
            prefix="/api/merchant",
        )
    app.state.sessions = host.sessions

    def announce_order(placed: PlacedOrder, checkout_session: dict[str, Any]) -> None:
        """Tell the session that started the checkout, on its next turn, that its
        order was placed and paid; the host injects pending app events as a note ahead of
        the customer's message. Nothing to do without a session id or for a session the
        store no longer has; a racing write is retried once."""
        session_id = str((checkout_session.get("metadata") or {}).get("lab_session_id") or "")
        if not session_id:
            return
        note = f"Order #{placed.display_id} was placed and paid ({money(placed)})."
        for _attempt in range(2):
            try:
                record = host.sessions.require(session_id)
            except UnknownSessionError:
                return
            if note in record.pending_app_events:
                return
            record.pending_app_events.append(note)
            try:
                host.sessions.save(record)
                return
            except SessionConflictError:
                continue
        logger.warning(
            "session %s kept racing; order %s not announced", session_id, placed.order_id
        )

    app.state.announce_order = announce_order
    services = HostServices(
        settings=settings,
        medusa=medusa,
        stripe=stripe or StripeClient(settings.stripe_secret_key),
        customers=customers,
        events=ProcessedEvents(events_path or data / "host_events.json"),
        on_order_placed=announce_order,
    )
    install_checkout_routes(app, services)
    install_order_request_routes(app, merchant, operator=settings.operator)
    install_cart_merge_route(app, host, storefront)
    app.state.storefront = storefront
    app.state.merchant = merchant
    app.state.shopping_agent = shopping_agent
    app.state.merchant_agent = merchant_agent

    inner = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        for step in on_startup or []:
            await step()
        count = await storefront.refresh_catalog()
        await merchant._load()
        logger.info("lab store ready: %d listings", count)
        async with inner(_app):
            yield
        await shopping_agent.close()
        await merchant_agent.close()

    app.router.lifespan_context = lifespan
    return app


async def default_startup(
    settings: LabSettings, medusa: MedusaClient, customers: CustomerDirectory
) -> None:
    customer = settings.customer_id
    if settings.lab_customer_email and customers.token(customer) is None:
        await customers.login(
            medusa,
            customer,
            settings.lab_customer_email,
            settings.lab_customer_password,
            address=default_address(settings),
        )
        for alias in PROFILE_ALIASES:
            customers.alias(alias, customer)


def default_app() -> FastAPI:
    settings = LabSettings.load()
    medusa = MedusaClient(settings.medusa_url, settings.medusa_publishable_key)
    customers = CustomerDirectory()
    stripe = StripeClient(settings.stripe_secret_key) if settings.stripe_secret_key else None
    # The region is resolved at startup (the client cannot be used before the loop runs).
    holder: dict[str, str] = {}

    async def resolve_region() -> None:
        holder["region"] = await medusa.first_region_id(settings.lab_currency)
        app.state.storefront.region_id = holder["region"]
        await default_startup(settings, medusa, customers)

    app = build_store_app(
        settings,
        medusa=medusa,
        region_id="pending",
        customers=customers,
        stripe=stripe,
        on_startup=[resolve_region],
        sessions_path=settings.data_dir / "sessions.sqlite",  # sessions survive a restart
    )
    return app
