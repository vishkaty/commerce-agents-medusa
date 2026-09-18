"""Lab host: Stripe webhook and checkout return pages over the Medusa store.

    uvicorn host.app:app --app-dir lab --port 8010

Flow: the shopping agent's ``checkout`` card carries a Stripe Checkout Session URL
(``MedusaStorefront.checkout_handoff``). The customer pays there in test mode. Stripe
posts ``checkout.session.completed`` here; the signature is verified, the event id is
remembered so a redelivery does nothing, and the Medusa cart named in the session's
metadata is completed into an order. ``/checkout/success`` does the same from the
browser return so the order exists even if the webhook is late.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from commerce_medusa.medusa_client import MedusaClient, MedusaError
from commerce_medusa.medusa_storefront import CustomerDirectory
from commerce_medusa.order_placement import (
    PaymentMismatch,
    PlacedOrder,
    ShippingAddress,
    place_order,
)
from commerce_medusa.settings import LabSettings
from commerce_medusa.stripe_checkout import StripeClient, verify_webhook_signature

logger = logging.getLogger("commerce_medusa.host")


def default_address(settings: LabSettings) -> ShippingAddress:
    """The demo shipping address the single customer checks out with."""
    return ShippingAddress(
        settings.customer_name, "Demo", "1 Main St", "Austin", "78701", "US", "tx"
    )


class ProcessedEvents:
    """Webhook event ids already handled, kept in a JSON file so a restart does not
    replay an order. A deployment keeps this in its database."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._ids: dict[str, str] = {}
        if path and path.exists():
            self._ids = json.loads(path.read_text())

    def seen(self, event_id: str) -> str | None:
        return self._ids.get(event_id)

    def record(self, event_id: str, order_id: str) -> None:
        self._ids[event_id] = order_id
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._ids, indent=1))


@dataclass
class HostServices:
    settings: LabSettings
    medusa: MedusaClient
    stripe: StripeClient
    customers: CustomerDirectory
    events: ProcessedEvents
    orders_by_cart: dict[str, PlacedOrder] = field(default_factory=dict)
    held_by_cart: dict[str, str] = field(default_factory=dict)  # cart id -> why it is held
    # Called once per placed order with the Checkout Session, so the host can tell
    # the session that started the checkout on its next turn.
    on_order_placed: Callable[[PlacedOrder, dict[str, Any]], None] | None = None
    _cart_locks: dict[str, asyncio.Lock] = field(default_factory=dict)

    async def ensure_customer(self) -> None:
        customer = self.settings.customer_id
        if self.settings.lab_customer_email and self.customers.token(customer) is None:
            await self.customers.login(
                self.medusa,
                customer,
                self.settings.lab_customer_email,
                self.settings.lab_customer_password,
                address=default_address(self.settings),
            )

    async def fulfil(self, checkout_session: dict[str, Any]) -> PlacedOrder:
        """Place the Medusa order for a paid Checkout Session; idempotent per cart, and
        serialised per cart so the webhook and the success page arriving together place
        one order (E2E-10)."""
        metadata = checkout_session.get("metadata") or {}
        cart_id = metadata.get("medusa_cart_id") or checkout_session.get("client_reference_id")
        if not cart_id:
            raise HTTPException(400, "checkout session carries no medusa_cart_id")
        async with self._cart_locks.setdefault(cart_id, asyncio.Lock()):
            return await self._fulfil_locked(cart_id, checkout_session)

    async def _fulfil_locked(self, cart_id: str, checkout_session: dict[str, Any]) -> PlacedOrder:
        metadata = checkout_session.get("metadata") or {}
        if cart_id in self.orders_by_cart:
            return self.orders_by_cart[cart_id]
        user_id = metadata.get("lab_user_id") or self.settings.customer_id
        await self.ensure_customer()
        email = (
            (checkout_session.get("customer_details") or {}).get("email")
            or checkout_session.get("customer_email")
            or self.customers.email(user_id)
            or self.settings.lab_customer_email
        )
        amount_total = checkout_session.get("amount_total")
        try:
            placed = await place_order(
                self.medusa,
                cart_id=cart_id,
                email=email,
                address=self.customers.address(user_id) or default_address(self.settings),
                token=self.customers.token(user_id),
                payment_reference=str(checkout_session.get("id") or ""),
                paid_amount_cents=int(amount_total) if amount_total is not None else None,
            )
        except PaymentMismatch as mismatch:
            self.held_by_cart[cart_id] = str(mismatch)
            logger.warning("payment held for review: %s", mismatch)
            raise
        self.held_by_cart.pop(cart_id, None)
        self.orders_by_cart[cart_id] = placed
        if self.on_order_placed is not None:
            try:
                self.on_order_placed(placed, checkout_session)
            except Exception:  # the order is placed; a failed notice must not fail the webhook
                logger.exception("order %s placed but the session was not told", placed.order_id)
        return placed


def install_checkout_routes(app: FastAPI, services: HostServices) -> None:
    """The Stripe webhook and the checkout return pages on any FastAPI app."""

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "medusa": services.settings.medusa_url,
            "orders_placed": len(services.orders_by_cart),
            "orders_held": len(services.held_by_cart),
        }

    @app.post("/webhooks/stripe")
    async def stripe_webhook(request: Request) -> JSONResponse:
        payload = await request.body()
        signature = request.headers.get("stripe-signature", "")
        secrets = services.settings.webhook_secrets
        if not secrets or not any(
            verify_webhook_signature(payload, signature, secret) for secret in secrets
        ):
            raise HTTPException(400, "invalid Stripe signature")
        event = json.loads(payload)
        event_id = str(event.get("id") or "")
        if event_id and (order_id := services.events.seen(event_id)):
            return JSONResponse({"received": True, "duplicate": True, "order_id": order_id})
        if event.get("type") != "checkout.session.completed":
            return JSONResponse({"received": True, "ignored": event.get("type")})
        checkout_session = (event.get("data") or {}).get("object") or {}
        if checkout_session.get("payment_status") not in {"paid", "no_payment_required"}:
            return JSONResponse({"received": True, "ignored": "unpaid"})
        try:
            placed = await services.fulfil(checkout_session)
        except PaymentMismatch as mismatch:
            # Acknowledged so Stripe does not retry; the payment waits for a person.
            return JSONResponse({"received": True, "held": True, "reason": str(mismatch)})
        except MedusaError as error:
            logger.exception("order placement failed for %s", checkout_session.get("id"))
            raise HTTPException(502, f"order placement failed: {error.message}") from error
        if event_id:
            services.events.record(event_id, placed.order_id)
        logger.info(
            "order %s placed for checkout session %s", placed.order_id, checkout_session.get("id")
        )
        return JSONResponse(
            {"received": True, "order_id": placed.order_id, "display_id": placed.display_id}
        )

    @app.get("/checkout/success", response_class=HTMLResponse)
    async def checkout_success(session_id: str) -> str:
        checkout_session = await services.stripe.retrieve_checkout_session(session_id)
        if checkout_session.get("payment_status") not in {"paid", "no_payment_required"}:
            status = checkout_session.get("payment_status")
            return f"<h1>Payment not completed</h1><p>Stripe reports {status}.</p>"
        try:
            placed = await services.fulfil(checkout_session)
        except PaymentMismatch:
            return (
                "<h1>Payment received</h1><p>The cart changed between checkout and payment, "
                "so the order is held for review. Nobody is charged twice; the store will "
                "confirm or refund.</p>"
            )
        except MedusaError as error:
            return (
                f"<h1>Payment received</h1><p>The order could not be placed yet: "
                f"{error.message}. It will be placed when the webhook arrives.</p>"
            )
        return (
            f"<h1>Thank you</h1><p>Payment received in Stripe test mode.</p>"
            f"<p>Order #{placed.display_id} placed in the lab store: "
            f"total {placed.total:.2f} {placed.currency}.</p>"
            f"<p>Ask the shopping assistant where your order stands.</p>"
        )

    @app.get("/checkout/cancel", response_class=HTMLResponse)
    async def checkout_cancel() -> str:
        return (
            "<h1>Checkout cancelled</h1>"
            "<p>Your cart is still there; ask the assistant to check out again.</p>"
        )


def build_app(services: HostServices) -> FastAPI:
    app = FastAPI(title="Commerce agents over Medusa", version="0.1.0")
    app.state.services = services
    install_checkout_routes(app, services)
    return app


def default_services(settings: LabSettings | None = None) -> HostServices:
    settings = settings or LabSettings.load()
    return HostServices(
        settings=settings,
        medusa=MedusaClient(settings.medusa_url, settings.medusa_publishable_key),
        stripe=StripeClient(settings.stripe_secret_key),
        customers=CustomerDirectory(),
        events=ProcessedEvents(settings.data_dir / "host_events.json"),
    )


app = build_app(default_services())
