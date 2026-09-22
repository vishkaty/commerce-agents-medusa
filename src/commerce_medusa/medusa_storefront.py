"""``StorefrontBackend`` over the Medusa v2 Store API.

Identity: the host logs each customer in (``CustomerDirectory.login``) and the backend
reads the JWT by ``session.user_id``; the model never sees it. A user without a token is
a guest: catalog and cart work, orders are empty. One Medusa cart per session id, created
on first use in the deployment's region. Cart writes take a variant id (or the id of a
plain product, which resolves to its only variant); a family id is held upstream by the
options gate before it reaches this class.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shopping_agent import (
    Cart,
    CheckoutHandoff,
    Disclosure,
    DisclosureRow,
    FulfillmentOption,
    Order,
    OrderStatus,
    Policy,
    Product,
    ProductDetails,
    SearchFilters,
    ShoppingSessionContext,
    UserPreferences,
)
from shopping_agent.backend import StorefrontBackend, Unavailable

from .medusa_client import MedusaClient, MedusaError
from .medusa_mapping import (
    PRODUCT_FIELDS,
    cart_from_medusa,
    order_from_medusa,
    plain_variant_id,
    product_from_medusa,
    variant_from_medusa,
    variant_in_stock,
)
from .order_placement import ShippingAddress, cheapest_shipping_option
from .order_requests import (
    Action,
    OrderActionRequest,
    OrderRequestStore,
    WaitlistEntry,
    WaitlistStore,
)
from .stripe_checkout import StripeClient, checkout_lines

ID_SHAPE = re.compile(r"[A-Za-z0-9_-]{1,64}")  # Medusa ids: prefix_ULID; nothing else is an id


@dataclass(frozen=True)
class PromoApplied:
    code: str
    discount: float
    cart: Cart


class CartStale(Unavailable, ValueError):
    """The cart no longer matches the catalog: a line went out of stock, is short, or its
    price moved since it was added. ``Unavailable`` for the contract; ``ValueError`` so
    ``run_presentation`` relays this message as written, rather than ``domain_error``
    relaying it with the add-to-cart wording ("Nothing was added: ...")."""


ORDER_FIELDS = ",".join(
    [
        "id",
        "display_id",
        "status",
        "payment_status",
        "fulfillment_status",
        "total",
        "currency_code",
        "created_at",
        "customer_id",
        "email",
        "*items",
        "*items.variant",
        "*items.variant.options",
    ]
)

_WORD = re.compile(r"[a-z0-9]+")


class CustomerDirectory:
    """What the host knows about each signed-in customer: the Medusa JWT and a display
    name. Filled by the host at sign-in; read by the backend by user id."""

    def __init__(self, entries: dict[str, dict[str, Any]] | None = None) -> None:
        self._entries: dict[str, dict[str, Any]] = dict(entries or {})

    async def login(
        self,
        client: MedusaClient,
        user_id: str,
        email: str,
        password: str,
        display_name: str | None = None,
        address: ShippingAddress | None = None,
    ) -> None:
        token = await client.login_customer(email, password)
        self._entries[user_id] = {
            "token": token,
            "email": email,
            "display_name": display_name or email.split("@")[0].title(),
            "address": address,
        }

    def address(self, user_id: str) -> ShippingAddress | None:
        return (self._entries.get(user_id) or {}).get("address")

    def alias(self, user_id: str, of: str) -> None:
        """Let another id (a demo profile name) act as an existing customer."""
        if of in self._entries:
            self._entries[user_id] = self._entries[of]

    def token(self, user_id: str) -> str | None:
        return (self._entries.get(user_id) or {}).get("token")

    def display_name(self, user_id: str) -> str | None:
        return (self._entries.get(user_id) or {}).get("display_name")

    def email(self, user_id: str) -> str | None:
        return (self._entries.get(user_id) or {}).get("email")


class MedusaStorefront(StorefrontBackend):
    def __init__(
        self,
        client: MedusaClient,
        *,
        region_id: str,
        customers: CustomerDirectory | None = None,
        policies_path: Path | None = None,
        requests: OrderRequestStore | None = None,
        waitlist: WaitlistStore | None = None,
        store_name: str = "Lab Store",
        stripe: StripeClient | None = None,
        host_url: str = "http://localhost:8010",
        carts: MutableMapping[str, str] | None = None,
    ) -> None:
        self.client = client
        self.region_id = region_id
        self.customers = customers or CustomerDirectory()
        self.store_name = store_name
        self.stripe = stripe
        self.host_url = host_url.rstrip("/")
        # session id -> Medusa cart id; durable when the host hands in a map
        self._carts: MutableMapping[str, str] = carts if carts is not None else {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._me_cache: dict[str | None, dict[str, Any]] = {}  # token -> customer record
        self._families: dict[str, str] = {}  # variant id -> family product id
        self._plain: dict[str, str] = {}  # plain product id -> its only variant id
        self._variant_options: dict[str, dict[str, str]] = {}  # variant id -> option values
        self._policies: list[Policy] = self._load_policies(policies_path)
        self.requests = requests  # order actions as durable requests
        self.waitlist = waitlist  # who waits for a sold-out product
        # The catalog as the demo host routes read it (``/api/products``): refreshed by
        # ``refresh_catalog`` at startup and after merchant writes.
        self.products: dict[str, ProductDetails] = {}
        self.on_reset: Callable[[str], Awaitable[None] | None] | None = None

    # -- helpers -----------------------------------------------------------------------

    @staticmethod
    def _load_policies(path: Path | None) -> list[Policy]:
        if path is None or not path.exists():
            return []
        rows = json.loads(path.read_text(encoding="utf-8"))
        return [Policy.model_validate(row) for row in rows]

    def _token(self, session: ShoppingSessionContext) -> str | None:
        return self.customers.token(session.user_id)

    def _remember(self, raw: dict[str, Any]) -> None:
        plain = plain_variant_id(raw)
        if plain:
            self._plain[str(raw["id"])] = plain
        for variant in raw.get("variants") or []:
            self._families[str(variant["id"])] = str(raw["id"])
            self._variant_options[str(variant["id"])] = variant_from_medusa(
                variant, raw
            ).option_values

    async def _fetch_products(self, **params: Any) -> list[dict[str, Any]]:
        query = {"region_id": self.region_id, "fields": PRODUCT_FIELDS, **params}
        data = await self.client.get("/store/products", params=query) or {}
        rows = data.get("products") or []
        for row in rows:
            self._remember(row)
        return rows

    async def _fetch_product(self, product_id: str) -> dict[str, Any] | None:
        if not ID_SHAPE.fullmatch(product_id):
            # The id goes into a URL path; anything but Medusa's id alphabet is unknown
            # by definition and never reaches the server.
            return None
        data = await self.client.get(
            f"/store/products/{product_id}",
            params={"region_id": self.region_id, "fields": PRODUCT_FIELDS},
            allow_404=True,
        )
        row = (data or {}).get("product")
        if row:
            self._remember(row)
        return row

    async def _family_of_variant(self, variant_id: str) -> dict[str, Any] | None:
        rows = await self._fetch_products(**{"variants.id": variant_id, "limit": 1})
        return rows[0] if rows else None

    async def _resolve_variant(self, product_id: str) -> str | None:
        """The variant id a cart write names, for a variant id or a plain product id."""
        if product_id.startswith("variant_"):
            return product_id
        if product_id in self._plain:
            return self._plain[product_id]
        row = await self._fetch_product(product_id)
        return plain_variant_id(row) if row else None

    def _session_lock(self, session: ShoppingSessionContext) -> asyncio.Lock:
        """One lock per session around cart creation and cart writes, so two concurrent
        writes cannot each create a cart and strand the other's line."""
        return self._locks.setdefault(session.session_id, asyncio.Lock())

    async def _cart_id(self, session: ShoppingSessionContext) -> str:
        cart_id = self._carts.get(session.session_id)
        if cart_id:
            return cart_id
        async with self._session_lock(session):
            return await self._cart_id_locked(session)

    async def _cart_id_locked(self, session: ShoppingSessionContext) -> str:
        """The session's cart id, creating the cart once; the caller holds the session lock."""
        cart_id = self._carts.get(session.session_id)
        if cart_id:
            return cart_id
        body: dict[str, Any] = {"region_id": self.region_id}
        email = self.customers.email(session.user_id)
        if email:
            body["email"] = email
        address = self.customers.address(session.user_id)
        if address is not None:
            body["shipping_address"] = address.as_medusa()
        data = await self.client.post("/store/carts", body, token=self._token(session))
        cart_id = str(data["cart"]["id"])
        self._carts[session.session_id] = cart_id
        return cart_id

    async def _raw_cart(self, session: ShoppingSessionContext) -> dict[str, Any]:
        cart_id = await self._cart_id(session)
        data = await self.client.get(f"/store/carts/{cart_id}", token=self._token(session)) or {}
        return data.get("cart") or {}

    async def _options_for_lines(self, lines: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
        """Option names for every line's variant; cart and order lines carry only the
        variant title, so the family record supplies the names (fetched once per variant)."""
        for line in lines:
            variant_id = line.get("variant_id")
            if variant_id and variant_id not in self._variant_options:
                try:
                    await self._family_of_variant(str(variant_id))
                except MedusaError:  # a line whose family cannot be read keeps its title
                    continue
        return {
            vid: self._variant_options[vid]
            for vid in {str(line.get("variant_id")) for line in lines}
            if vid in self._variant_options
        }

    async def _cart_model(self, cart: dict[str, Any]) -> Cart:
        return cart_from_medusa(cart, await self._options_for_lines(cart.get("items") or []))

    async def _order_model(self, order: dict[str, Any]) -> Order:
        return order_from_medusa(order, await self._options_for_lines(order.get("items") or []))

    @staticmethod
    def _line_for(cart: dict[str, Any], variant_id: str) -> dict[str, Any] | None:
        for line in cart.get("items") or []:
            if line.get("variant_id") == variant_id:
                return line
        return None

    # -- what the demo host routes use besides the backend interface ---------------------

    async def refresh_catalog(self, limit: int = 100) -> int:
        """Load every listing into ``products`` (families and plain, keyed by id)."""
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            batch = await self._fetch_products(limit=limit, offset=offset)
            rows.extend(batch)
            offset += len(batch)
            if len(batch) < limit:
                break
        self.products = {str(r["id"]): product_from_medusa(r) for r in rows}
        return len(self.products)

    def product(self, product_id: str) -> ProductDetails | None:
        """A listing or one of its variants from the cached catalog."""
        if product_id in self.products:
            return self.products[product_id]
        family_id = self._families.get(product_id)
        family = self.products.get(family_id) if family_id else None
        if family is None:
            return None
        variant = next((v for v in family.variants if v.product_id == product_id), None)
        return ProductDetails(**variant.model_dump()) if variant else None

    def reset_session(self, session_id: str) -> None:
        self._carts.pop(session_id, None)
        if self.on_reset is not None:
            result = self.on_reset(session_id)
            if hasattr(result, "__await__"):
                try:
                    asyncio.get_running_loop().create_task(result)
                except RuntimeError:  # no loop: nothing to schedule
                    pass

    def recent_orders(self, limit: int = 6) -> list[Order]:
        return []  # the merchant overview's order feed comes from the merchant adapter

    # -- catalog -----------------------------------------------------------------------

    async def search_products(
        self,
        session: ShoppingSessionContext,
        query: str,
        filters: SearchFilters | None = None,
        limit: int = 8,
    ) -> list[Product]:
        filters = filters or SearchFilters()
        params: dict[str, Any] = {"limit": max(limit * 3, 24)}
        if query.strip():
            params["q"] = query.strip()
        rows = await self._fetch_products(**params)
        products = [product_from_medusa(row) for row in rows]
        results = [p for p in products if self._matches(p, filters)]
        if filters.sort == "price_asc":
            results.sort(key=lambda p: p.price)
        elif filters.sort == "price_desc":
            results.sort(key=lambda p: -p.price)
        elif filters.sort == "rating":
            results.sort(key=lambda p: -(p.rating or 0))
        return [
            Product.model_validate(
                p.model_dump(exclude={"variants", "long_description", "specs", "review_highlights"})
            )
            for p in results[:limit]
        ]

    @staticmethod
    def _matches(product: ProductDetails, filters: SearchFilters) -> bool:
        if filters.category:
            wanted = filters.category.lower().rstrip("s")
            category = (product.category or "").lower().rstrip("s")
            title = product.title.lower()
            if wanted not in category and wanted not in title:
                return False
        if filters.min_price is not None and product.price < filters.min_price:
            return False
        if filters.max_price is not None and product.price > filters.max_price:
            return False
        if filters.min_rating is not None and (product.rating or 0) < filters.min_rating:
            return False
        for key, value in (filters.attributes or {}).items():
            key_l, value_l = key.lower(), str(value).lower()
            option_values = [
                v.lower() for k, vs in product.options.items() if k.lower() == key_l for v in vs
            ]
            attribute = (product.attributes.get(key) or "").lower()
            if option_values and value_l not in option_values:
                return False
            if not option_values and attribute and value_l not in attribute:
                return False
        return True

    async def get_product_details(
        self, session: ShoppingSessionContext, product_id: str
    ) -> ProductDetails | None:
        if product_id.startswith("variant_"):
            family = await self._family_of_variant(product_id)
            if not family:
                return None
            raw = next((v for v in family["variants"] if v["id"] == product_id), None)
            if raw is None:
                return None
            return ProductDetails(**variant_from_medusa(raw, family).model_dump())
        row = await self._fetch_product(product_id)
        return product_from_medusa(row) if row else None

    # -- cart --------------------------------------------------------------------------

    async def get_cart(self, session: ShoppingSessionContext) -> Cart:
        return await self._cart_model(await self._raw_cart(session))

    async def add_to_cart(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        variant_id = await self._resolve_variant(product_id)
        if variant_id is None:
            # A family: name the variants that can be bought instead (HS-S-02).
            row = await self._fetch_product(product_id)
            siblings = [
                str(v["id"]) for v in (row or {}).get("variants") or [] if variant_in_stock(v)
            ]
            if siblings:
                raise Unavailable(
                    f"{product_id} has options; add one of its variants: {', '.join(siblings)}"
                )
            raise Unavailable(f"{product_id} cannot be added to the cart")
        family = await self._family_of_variant(variant_id)
        if family is None:
            raise Unavailable(f"{variant_id} is not sold here")
        raw = next((v for v in family["variants"] if v["id"] == variant_id), None)
        if raw is None or not variant_in_stock(raw):
            siblings = [
                v["id"] for v in family["variants"] if v["id"] != variant_id and variant_in_stock(v)
            ]
            note = (
                f"; in stock: {', '.join(siblings)}"
                if siblings
                else "; no variant of this item is in stock"
            )
            raise Unavailable(f"{variant_id} is out of stock{note}")
        async with self._session_lock(session):
            cart_id = await self._cart_id_locked(session)
            data = await self.client.post(
                f"/store/carts/{cart_id}/line-items",
                {"variant_id": variant_id, "quantity": quantity},
                token=self._token(session),
            )
        return await self._cart_model(data.get("cart") or {})

    async def update_cart_item(
        self, session: ShoppingSessionContext, product_id: str, quantity: int
    ) -> Cart:
        if quantity < 1:
            raise ValueError("quantity must be at least 1; use remove_from_cart to drop a line")
        variant_id = await self._resolve_variant(product_id) or product_id
        cart = await self._raw_cart(session)
        line = self._line_for(cart, variant_id)
        if line is None:
            return await self._cart_model(cart)
        data = await self.client.post(
            f"/store/carts/{cart['id']}/line-items/{line['id']}",
            {"quantity": quantity},
            token=self._token(session),
        )
        return await self._cart_model(data.get("cart") or {})

    async def remove_from_cart(self, session: ShoppingSessionContext, product_id: str) -> Cart:
        variant_id = await self._resolve_variant(product_id) or product_id
        cart = await self._raw_cart(session)
        line = self._line_for(cart, variant_id)
        if line is None:
            return await self._cart_model(cart)
        data = await self.client.delete(
            f"/store/carts/{cart['id']}/line-items/{line['id']}", token=self._token(session)
        )
        # Medusa answers a delete with the parent cart.
        return await self._cart_model(data.get("parent") or data.get("cart") or {})

    async def checkout_handoff(
        self, session: ShoppingSessionContext, cart: Cart
    ) -> list[CheckoutHandoff]:
        """A Stripe Checkout Session (test mode) for the session's cart, with the cheapest
        shipping option as a line. The URL goes on the card; the model never sees it. No
        Stripe client configured means the host's own checkout route applies."""
        if not cart.items:
            return []
        await self._revalidate(session, cart)  # before any checkout, hosted or the host's own
        if self.stripe is None:
            return []
        cart_id = await self._cart_id(session)
        shipping_name: str | None = None
        shipping_fee = 0.0
        try:
            option = await cheapest_shipping_option(self.client, cart_id, self._token(session))
            shipping_name = str(option.get("name") or "Shipping")
            amount = option.get("amount")
            if amount is None:
                amount = (option.get("calculated_price") or {}).get("calculated_amount") or 0
            shipping_fee = float(amount)
        except Exception:  # no address yet: the host's checkout collects it
            shipping_name, shipping_fee = None, 0.0
        created = await self.stripe.create_checkout_session(
            lines=checkout_lines(cart, shipping_name, shipping_fee),
            currency=cart.currency,
            success_url=f"{self.host_url}/checkout/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{self.host_url}/checkout/cancel",
            metadata={
                "medusa_cart_id": cart_id,
                "lab_session_id": session.session_id,
                "lab_user_id": session.user_id,
            },
            customer_email=self.customers.email(session.user_id),
        )
        return [CheckoutHandoff(url=str(created["url"]), label="Pay with Stripe (test mode)")]

    async def _revalidate(self, session: ShoppingSessionContext, cart: Cart) -> None:
        """Every line against the live catalog before money moves (HS-S-07). A line that
        cannot be fulfilled is named; a line whose price moved is refreshed to the current
        price and named, so the next checkout shows what will be charged."""
        problems: list[str] = []
        refreshed = False
        for item in cart.items:
            variant_id = await self._resolve_variant(item.product_id) or item.product_id
            family = await self._family_of_variant(variant_id)
            raw = next(
                (v for v in (family or {}).get("variants") or [] if v["id"] == variant_id), None
            )
            if raw is None or not variant_in_stock(raw):
                problems.append(f"{item.product_id} is no longer available")
                continue
            available = raw.get("inventory_quantity")
            if raw.get("manage_inventory") and not raw.get("allow_backorder"):
                if isinstance(available, int | float) and available < item.quantity:
                    problems.append(f"only {int(available)} of {item.product_id} left")
                    continue
            current = (raw.get("calculated_price") or {}).get("calculated_amount")
            if current is not None and abs(float(current) - item.price) >= 0.005:
                await self._refresh_line(session, variant_id, item.quantity)
                refreshed = True
                problems.append(
                    f"{item.product_id} price changed {item.price:.2f} → {float(current):.2f}"
                )
        if problems:
            note = "; the cart now shows the current prices, check out again" if refreshed else ""
            raise CartStale("cart changed since it was reviewed: " + "; ".join(problems) + note)

    async def _refresh_line(
        self, session: ShoppingSessionContext, variant_id: str, quantity: int
    ) -> None:
        """Bring one line to the catalog's current price. Medusa runs
        ``refreshCartItemsWorkflow`` on a line-item update, so re-sending the quantity the
        line already has reprices it in place and keeps its id; a cart-level update or a
        plain read does not (verified against 2.20.1 on 2026-09-22). A line the cart no
        longer holds is added instead."""
        async with self._session_lock(session):
            raw_cart = await self._raw_cart(session)
            line = self._line_for(raw_cart, variant_id)
            if line is None:
                await self.client.post(
                    f"/store/carts/{raw_cart['id']}/line-items",
                    {"variant_id": variant_id, "quantity": quantity},
                    token=self._token(session),
                )
                return
            await self.client.post(
                f"/store/carts/{raw_cart['id']}/line-items/{line['id']}",
                {"quantity": quantity},
                token=self._token(session),
            )

    # -- promo codes ------------------------------------------------------------

    async def apply_promo_code(self, session: ShoppingSessionContext, code: str) -> PromoApplied:
        """Apply a platform promotion code to the session's cart; the discount shows on
        the line prices from then on. An unknown code is a ValueError naming it."""
        clean = code.strip().upper()
        before = await self.get_cart(session)
        cart_id = await self._cart_id(session)
        try:
            data = await self.client.post(
                f"/store/carts/{cart_id}/promotions",
                {"promo_codes": [clean]},
                token=self._token(session),
            )
        except MedusaError as refused:
            raise ValueError(f"{code} is not a valid promotion code") from refused
        after = await self._cart_model(data.get("cart") or {})
        return PromoApplied(
            code=clean, discount=round(before.subtotal - after.subtotal, 2), cart=after
        )

    async def remove_promo_code(self, session: ShoppingSessionContext, code: str) -> Cart:
        cart_id = await self._cart_id(session)
        data = await self.client.delete(
            f"/store/carts/{cart_id}/promotions",
            {"promo_codes": [code.strip().upper()]},
            token=self._token(session),
        )
        return await self._cart_model(data.get("cart") or await self._raw_cart(session))

    # -- disclosures ------------------------------------------------------------

    async def get_disclosure(
        self, session: ShoppingSessionContext, product_id: str
    ) -> Disclosure | None:
        """A facts box authored on the listing itself: ``disc:title``, ``disc:rows`` (a
        JSON list of label/value/note) and ``disc:sources`` in the product's metadata."""
        row = await self._fetch_product(product_id)
        metadata = (row or {}).get("metadata") or {}
        if not row or not metadata.get("disc:rows"):
            return None
        try:
            rows = json.loads(metadata["disc:rows"])
            sources = json.loads(metadata.get("disc:sources") or "[]")
        except (TypeError, json.JSONDecodeError):
            return None
        return Disclosure(
            title=str(metadata.get("disc:title") or "What to know"),
            product_id=product_id,
            rows=[
                DisclosureRow(
                    label=str(r.get("label", "")), value=str(r.get("value", "")), note=r.get("note")
                )
                for r in rows
                if isinstance(r, dict)
            ],
            sources=[str(s) for s in sources],
        )

    # -- sign-in ----------------------------------------------------------------

    async def merge_guest_cart(
        self, session: ShoppingSessionContext, *, from_session_id: str
    ) -> Cart:
        """Move a guest session's lines into the signed-in session's cart, once; the guest
        cart is left empty so a second call adds nothing."""
        guest_cart_id = self._carts.get(from_session_id)
        if guest_cart_id is None or guest_cart_id == self._carts.get(session.session_id):
            return await self.get_cart(session)
        guest = (
            await self.client.get(f"/store/carts/{guest_cart_id}", token=self._token(session)) or {}
        ).get("cart") or {}
        for line in guest.get("items") or []:
            if not line.get("variant_id"):
                continue
            async with self._session_lock(session):
                cart_id = await self._cart_id_locked(session)
                await self.client.post(
                    f"/store/carts/{cart_id}/line-items",
                    {"variant_id": line["variant_id"], "quantity": int(line.get("quantity") or 1)},
                    token=self._token(session),
                )
            await self.client.delete(
                f"/store/carts/{guest_cart_id}/line-items/{line['id']}", token=self._token(session)
            )
        return await self.get_cart(session)

    # -- fulfillment choice -----------------------------------------------------

    async def choose_fulfillment(
        self, session: ShoppingSessionContext, option: str
    ) -> FulfillmentOption:
        """Set the cart's shipping method to the option whose name matches ``option``; the
        options are re-read from the platform, so only what it offers can be chosen."""
        cart_id = await self._cart_id(session)
        data = await self.client.get("/store/shipping-options", params={"cart_id": cart_id}) or {}
        wanted = option.strip().lower()
        rows = data.get("shipping_options") or []
        match = next((r for r in rows if wanted and wanted in str(r.get("name", "")).lower()), None)
        if match is None:
            names = ", ".join(str(r.get("name")) for r in rows) or "none"
            raise ValueError(f"'{option}' is not offered; the options are: {names}")
        await self.client.post(
            f"/store/carts/{cart_id}/shipping-methods",
            {"option_id": match["id"]},
            token=self._token(session),
        )
        amount = match.get("amount")
        if amount is None:
            amount = (match.get("calculated_price") or {}).get("calculated_amount")
        name = str(match.get("name") or "Shipping")
        eta = "1 to 2 business days" if "express" in name.lower() else "3 to 5 business days"
        return FulfillmentOption(method="shipping", eta=f"{name}: {eta}", fee=float(amount or 0.0))

    # -- waiting for stock ------------------------------------------------------

    async def subscribe_availability(
        self, session: ShoppingSessionContext, product_id: str
    ) -> WaitlistEntry:
        """Put the customer on the list for a sold-out product or variant; a product that
        is in stock, or unknown, is refused with the reason."""
        if self.waitlist is None:
            raise ValueError("this store keeps no waitlist")
        variant_id = await self._resolve_variant(product_id)
        family = await self._family_of_variant(variant_id) if variant_id else None
        raw = next((v for v in (family or {}).get("variants") or [] if v["id"] == variant_id), None)
        if raw is None:
            raise ValueError(f"{product_id} is not a product that can be waited for")
        if variant_in_stock(raw):
            raise ValueError(f"{product_id} is in stock; add it to the cart instead")
        return self.waitlist.subscribe(user_id=session.user_id, product_id=product_id)

    # -- order actions ----------------------------------------------------------

    async def request_order_action(
        self,
        session: ShoppingSessionContext,
        order_id: str,
        action: Action,
        item_ids: list[str],
        reason: str,
    ) -> OrderActionRequest:
        """Record a cancellation, return or problem request for one of the customer's own
        orders. Nothing is written to the platform here; the merchant resolves it."""
        if self.requests is None:
            raise ValueError("this store does not take order requests")
        order = await self.get_order(session, order_id)
        if order is None:
            raise ValueError(f"{order_id} is not one of your orders")
        if action == "cancel" and order.status is not OrderStatus.PROCESSING:
            raise ValueError(
                f"{order_id} has already shipped ({order.status.value}); a return is the option"
            )
        if action == "return" and order.status not in {OrderStatus.SHIPPED, OrderStatus.DELIVERED}:
            raise ValueError(
                f"{order_id} has not shipped yet ({order.status.value}); "
                "a cancellation is the option"
            )
        known = {item.product_id for item in order.items}
        unknown = [i for i in item_ids if i not in known]
        if unknown:
            raise ValueError(f"{', '.join(unknown)} not in order {order_id}")
        return self.requests.create(
            user_id=session.user_id,
            order_id=order_id,
            action=action,
            item_ids=item_ids,
            reason=reason,
        )

    async def get_order_requests(self, session: ShoppingSessionContext) -> list[OrderActionRequest]:
        if self.requests is None:
            return []
        return self.requests.for_user(session.user_id)

    # -- customer context --------------------------------------------------------------

    async def get_preferences(self, session: ShoppingSessionContext) -> UserPreferences:
        token = self._token(session)
        display_name = self.customers.display_name(session.user_id)
        preferences: dict[str, str] = {}
        if token:
            data = await self.client.get("/store/customers/me", token=token, allow_404=True) or {}
            customer = data.get("customer") or {}
            display_name = customer.get("first_name") or display_name
            if customer.get("email"):
                preferences["email"] = str(customer["email"])
        else:
            preferences["account"] = "guest"
        return UserPreferences(
            user_id=session.user_id, display_name=display_name, preferences=preferences
        )

    # -- orders and policies -----------------------------------------------------------

    async def get_orders(self, session: ShoppingSessionContext, limit: int = 5) -> list[Order]:
        token = self._token(session)
        if not token:
            return []
        data = (
            await self.client.get(
                "/store/orders",
                params={"fields": ORDER_FIELDS, "limit": limit, "order": "-created_at"},
                token=token,
            )
            or {}
        )
        orders = [await self._order_model(row) for row in data.get("orders") or []]
        orders.sort(key=lambda o: o.placed_at, reverse=True)
        return orders[:limit]

    async def get_order(self, session: ShoppingSessionContext, order_id: str) -> Order | None:
        token = self._token(session)
        if not token or not ID_SHAPE.fullmatch(order_id):
            return None
        data = await self.client.get(
            f"/store/orders/{order_id}",
            params={"fields": ORDER_FIELDS},
            token=token,
            allow_404=True,
        )
        row = (data or {}).get("order")
        if not row or not await self._owns(session, row):
            return None
        return await self._order_model(row)

    async def _owns(self, session: ShoppingSessionContext, order: dict[str, Any]) -> bool:
        """The platform serves any order by id to any signed-in customer;
        ownership is checked here against the customer record, never the model's words."""
        me = await self._me(session)
        owner_id, owner_email = order.get("customer_id"), order.get("email")
        if owner_id:
            return str(owner_id) == str(me.get("id"))
        if owner_email:
            return str(owner_email).lower() == str(me.get("email") or "").lower()
        return True  # an order the platform recorded without an owner (legacy fixture)

    async def _me(self, session: ShoppingSessionContext) -> dict[str, Any]:
        token = self._token(session)
        if token in self._me_cache:
            return self._me_cache[token]
        data = await self.client.get("/store/customers/me", token=token) or {}
        me = data.get("customer") or {}
        self._me_cache[token] = me
        return me

    async def search_policies(self, session: ShoppingSessionContext, query: str) -> list[Policy]:
        terms = {_stem(w) for w in _WORD.findall(query.lower()) if len(w) > 2}
        scored: list[tuple[int, Policy]] = []
        for policy in self._policies:
            haystack = f"{policy.title} {policy.category or ''} {policy.content}".lower()
            words = {_stem(w) for w in _WORD.findall(haystack)}
            score = len(terms & words)
            if score:
                scored.append((score, policy))
        scored.sort(key=lambda pair: -pair[0])
        return [policy for _, policy in scored]

    # -- fulfillment -------------------------------------------------------------------

    async def get_fulfillment_options(
        self, session: ShoppingSessionContext, product_ids: list[str]
    ) -> list[FulfillmentOption]:
        cart_id = await self._cart_id(session)
        data = await self.client.get("/store/shipping-options", params={"cart_id": cart_id}) or {}
        options: list[FulfillmentOption] = []
        for row in data.get("shipping_options") or []:
            name = str(row.get("name") or "Shipping")
            amount = row.get("amount")
            if amount is None:
                amount = (row.get("calculated_price") or {}).get("calculated_amount")
            eta = "1 to 2 business days" if "express" in name.lower() else "3 to 5 business days"
            options.append(
                FulfillmentOption(method="shipping", eta=f"{name}: {eta}", fee=float(amount or 0.0))
            )
        return options


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word
