"""``MerchantBackend`` over the Medusa v2 Admin API.

Listings are Medusa products (families and plain) and variants, priced in the lab
currency from variant prices and stocked from inventory levels. Back-office facts the
storefront never shows (unit cost, 30-day sales pace, low-stock threshold, content
quality) live in ``metadata`` under ``mer:`` keys, written by the importer.

Metrics are two layers: the upstream fixture history (``merchant_metrics.json``,
rebased so it ends last week) for a believable trend, plus every real Medusa order
aggregated by day on top. Traffic exists only in the fixture layer and the merchant
context says so.

Writes are the reference's staged-change flow on the upstream ``ChangeLedger``;
``apply_change`` performs the Medusa write first and marks the change applied only when
that succeeded, so a failed write leaves it staged.
"""

from __future__ import annotations

import os
import re
import socket
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from demo_common.merchant_fixtures import (
    alert_counts,
    load_campaigns,
    load_issues,
    margin_pct,
    metric_window,
    rebase_daily,
    refuse_outside_range,
    snapshot_of,
    stage_campaign,
)
from demo_common.storefront_fixtures import load_json
from merchant_agent import (
    ActorKind,
    AnalysisTable,
    BusinessSnapshot,
    Campaign,
    CampaignDraft,
    ChangeItem,
    ChangeKind,
    ChangeStatus,
    DataLimitation,
    InventoryActionItem,
    InventoryAlert,
    Listing,
    ListingDetails,
    ListingFilters,
    MerchantAgentConfig,
    MerchantSessionContext,
    MetricPoint,
    MetricSeries,
    OrderIssue,
    PriceUpdateItem,
    PricingContext,
    PromotionDraft,
    StagedChange,
)
from merchant_agent.backend import MerchantBackend
from merchant_agent.changes import ChangeNotApplicable, GuardrailViolation

from .analysis import SCHEMA_NOTE, AnalysisReplica
from .ledger import SqliteLedger
from .medusa_admin import MedusaAdmin
from .medusa_mapping import DEFAULT_OPTION, product_options
from .order_requests import Decision, OrderActionRequest, OrderRequestStore, WaitlistStore

KNOWN_METRICS = frozenset(
    {
        "sales",
        "revenue",
        "orders",
        "traffic",
        "conversion",
        "conversion_rate",
        "average_order_value",
        "aov",
    }
)

ADMIN_PRODUCT_FIELDS = ",".join(
    [
        "id",
        "title",
        "handle",
        "status",
        "description",
        "subtitle",
        "thumbnail",
        "metadata",
        "*categories",
        "*options",
        "*options.values",
        "*variants",
        "*variants.options",
        "*variants.options.option",
        "*variants.prices",
        "*variants.inventory_items",
    ]
)
ADMIN_ORDER_FIELDS = ",".join(
    [
        "id",
        "display_id",
        "status",
        "payment_status",
        "fulfillment_status",
        "total",
        "subtotal",
        "currency_code",
        "created_at",
        "email",
        "*items",
    ]
)
MERCHANT_PREFIX = "mer:"
ATTRIBUTE_PREFIX = "attr:"
FAMILY_CONTENT_FIELDS = {"title", "description", "short_description", "category", "image_url"}
DELAYED_AFTER_DAYS = 3
SLOW_MOVER_DAYS_OF_COVER = 120
_WORD = re.compile(r"[a-z0-9]+")


def _meta(product: dict[str, Any]) -> dict[str, Any]:
    """The ``mer:`` facts of a product, typed."""
    metadata = product.get("metadata") or {}
    row: dict[str, Any] = {}
    for key, value in metadata.items():
        if not key.startswith(MERCHANT_PREFIX) or value in (None, ""):
            continue
        name = key[len(MERCHANT_PREFIX) :]
        if name in {"unit_cost"}:
            row[name] = float(value)
        elif name in {"sales_last_30d", "threshold"}:
            row[name] = int(float(value))
        elif name == "missing_attributes":
            row[name] = [part.strip() for part in str(value).split(",") if part.strip()]
        else:
            row[name] = str(value)
    return row


def _attributes(product: dict[str, Any]) -> dict[str, str]:
    metadata = product.get("metadata") or {}
    return {
        key[len(ATTRIBUTE_PREFIX) :]: str(value)
        for key, value in metadata.items()
        if key.startswith(ATTRIBUTE_PREFIX) and value is not None
    }


def _option_values(variant: dict[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for entry in variant.get("options") or []:
        name = (entry.get("option") or {}).get("title") or entry.get("option_id")
        if name and entry.get("value") is not None and name != DEFAULT_OPTION:
            values[str(name)] = str(entry["value"])
    return values


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)]
    return word


class MedusaMerchant(MerchantBackend):
    def __init__(
        self,
        admin: MedusaAdmin,
        *,
        config: MerchantAgentConfig,
        currency: str = "usd",
        store_name: str = "Lab Store",
        merchant_id: str = "lab-store",
        fixtures_dir: Path | None = None,
        default_threshold: int = 8,
        default_stock: int = 0,
        ledger_path: Path | str = ":memory:",
        owner: str | None = None,
        requests: OrderRequestStore | None = None,
        waitlist: WaitlistStore | None = None,
        analysis: AnalysisReplica | None = None,
    ) -> None:
        self.admin = admin
        self.config = config
        self.currency = currency.lower()
        self.store_name = store_name
        self.merchant_id = merchant_id
        self.default_threshold = default_threshold
        self.default_stock = default_stock
        self.ledger = SqliteLedger(config, ledger_path)
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}:{id(self):x}"
        self.interrupt_before_stamp = False  # test hook: die after the write, before the stamp
        self.requests = requests  # the shoppers' order requests
        self.waitlist = waitlist  # who waits for a sold-out product
        self.analysis = analysis  # the read-only replica for the analysis delegate
        self._campaigns: dict[str, Campaign] = load_campaigns(fixtures_dir) if fixtures_dir else {}
        self._fixture_issues: list[OrderIssue] = load_issues(fixtures_dir) if fixtures_dir else []
        self._history: list[dict[str, Any]] = (
            rebase_daily(load_json(fixtures_dir, "merchant_metrics.json")["daily"])
            if fixtures_dir
            else []
        )
        # Filled per read by _load(): admin product rows and inventory levels.
        self._products: dict[str, dict[str, Any]] = {}
        self._variants: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
        self._levels: dict[str, dict[str, Any]] = {}  # inventory item id -> level row
        self._location_id: str | None = None

    # -- loading -----------------------------------------------------------------------

    async def _load(self) -> None:
        products = await self.admin.list_all(
            "/admin/products", "products", fields=ADMIN_PRODUCT_FIELDS
        )
        items = await self.admin.list_all(
            "/admin/inventory-items", "inventory_items", page=200, fields="id,sku,*location_levels"
        )
        self._products = {str(p["id"]): p for p in products}
        self._variants = {str(v["id"]): (v, p) for p in products for v in p.get("variants") or []}
        levels: dict[str, dict[str, Any]] = {}
        for item in items:
            for level in item.get("location_levels") or []:
                if self._location_id is None:
                    self._location_id = str(level["location_id"])
                if str(level["location_id"]) == self._location_id:
                    levels[str(item["id"])] = level
        self._levels = levels

    def _product(self, listing_id: str) -> dict[str, Any] | None:
        return self._products.get(listing_id)

    def _variant(self, listing_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
        return self._variants.get(listing_id)

    def _is_family(self, product: dict[str, Any]) -> bool:
        return bool(product_options(product))

    def _price_of(self, variant: dict[str, Any]) -> float:
        for price in variant.get("prices") or []:
            if str(price.get("currency_code", "")).lower() == self.currency:
                return float(price["amount"])
        prices = variant.get("prices") or []
        return float(prices[0]["amount"]) if prices else 0.0

    def _stock_of(self, variant: dict[str, Any]) -> int:
        total = 0
        found = False
        for item in variant.get("inventory_items") or []:
            level = self._levels.get(str(item["inventory_item_id"]))
            if level is not None:
                found = True
                total += int(level.get("available_quantity") or level.get("stocked_quantity") or 0)
        return total if found else self.default_stock

    def _purchasable(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        return list(product.get("variants") or [])

    # -- listings ----------------------------------------------------------------------

    def _variant_listing(self, variant: dict[str, Any], product: dict[str, Any]) -> Listing:
        stock = self._stock_of(variant)
        status = (
            "paused"
            if product.get("status") == "draft"
            else ("active" if stock > 0 else "out_of_stock")
        )
        option_values = _option_values(variant)
        suffix = ", ".join(option_values.values())
        row = _meta(product)
        return Listing(
            listing_id=str(variant["id"]),
            title=f"{product['title']} ({suffix})" if suffix else str(product["title"]),
            status=status,
            price=self._price_of(variant),
            currency=self.currency.upper(),
            stock=stock,
            category=self._category(product),
            content_quality=row.get("content_quality", "good"),
            attributes=_attributes(product)
            | ({"sku": str(variant["sku"])} if variant.get("sku") else {}),
            image_url=variant.get("thumbnail") or product.get("thumbnail"),
            short_description=product.get("subtitle")
            or (product.get("description") or "")[:160]
            or None,
            option_values=option_values,
            variant_of=str(product["id"]),
        )

    def _listing(self, product: dict[str, Any]) -> Listing:
        variants = self._purchasable(product)
        row = _meta(product)
        if self._is_family(product):
            rows = [self._variant_listing(v, product) for v in variants]
            stock = sum(r.stock for r in rows)
            price = min((r.price for r in rows), default=0.0)
            active = any(r.status == "active" for r in rows)
            status = (
                "paused"
                if product.get("status") == "draft"
                else ("active" if active else "out_of_stock")
            )
            attributes = _attributes(product)
        else:
            variant = variants[0] if variants else {}
            stock = self._stock_of(variant) if variant else 0
            price = self._price_of(variant) if variant else 0.0
            status = (
                "paused"
                if product.get("status") == "draft"
                else ("active" if stock > 0 else "out_of_stock")
            )
            attributes = _attributes(product) | (
                {"sku": str(variant["sku"])} if variant.get("sku") else {}
            )
        return Listing(
            listing_id=str(product["id"]),
            title=str(product["title"]),
            status=status,
            price=price,
            currency=self.currency.upper(),
            stock=stock,
            category=self._category(product),
            content_quality=row.get("content_quality", "good"),
            attributes=attributes,
            image_url=product.get("thumbnail"),
            short_description=product.get("subtitle")
            or (product.get("description") or "")[:160]
            or None,
            options=product_options(product),
        )

    def _details(self, product: dict[str, Any]) -> ListingDetails:
        base = self._listing(product)
        row = _meta(product)
        variants = (
            [self._variant_listing(v, product) for v in self._purchasable(product)]
            if self._is_family(product)
            else []
        )
        return ListingDetails(
            **base.model_dump(),
            long_description=product.get("description"),
            missing_attributes=row.get("missing_attributes", []),
            variants=variants,
        )

    @staticmethod
    def _category(product: dict[str, Any]) -> str | None:
        categories = product.get("categories") or []
        return str(categories[0].get("handle") or categories[0].get("name")) if categories else None

    def _all_listings(self) -> list[Listing]:
        return [self._listing(p) for p in self._products.values()]

    def all_listings(self) -> list[Listing]:
        """What the portal router lists; from the last ``_load``."""
        return self._all_listings()

    def _resolve(
        self, listing_id: str
    ) -> tuple[Listing, dict[str, Any], dict[str, Any] | None] | None:
        """``(listing, product row, variant row or None)`` for a product or variant id."""
        product = self._product(listing_id)
        if product is not None:
            return self._listing(product), product, None
        pair = self._variant(listing_id)
        if pair is not None:
            variant, parent = pair
            return self._variant_listing(variant, parent), parent, variant
        return None

    # -- performance -------------------------------------------------------------------

    async def _orders(self) -> list[dict[str, Any]]:
        return await self.admin.list_all("/admin/orders", "orders", fields=ADMIN_ORDER_FIELDS)

    async def _daily_rows(self) -> list[dict[str, Any]]:
        """Fixture history plus real orders per day, in the lab currency."""
        rows = {row["date"]: dict(row) for row in self._history}
        by_day: dict[str, dict[str, float]] = defaultdict(lambda: {"sales": 0.0, "orders": 0})
        for order in await self._orders():
            if str(order.get("status", "")).lower() in {"canceled", "cancelled"}:
                continue
            if str(order.get("currency_code", "")).lower() != self.currency:
                continue
            day = str(order.get("created_at", ""))[:10]
            by_day[day]["sales"] += float(order.get("total") or 0)
            by_day[day]["orders"] += 1
        for day, totals in by_day.items():
            row = rows.setdefault(
                day, {"date": day, "sales": 0.0, "orders": 0, "traffic": 0, "kids_room_sales": 0.0}
            )
            row["sales"] = round(float(row.get("sales", 0)) + totals["sales"], 2)
            row["orders"] = int(row.get("orders", 0)) + int(totals["orders"])
        # Calendar-complete through today, so a period window counts days, not rows.
        if rows:
            first = date.fromisoformat(min(rows))
            last = max(date.fromisoformat(max(rows)), date.today())
            day = first
            while day <= last:
                key = day.isoformat()
                rows.setdefault(
                    key,
                    {"date": key, "sales": 0.0, "orders": 0, "traffic": 0, "kids_room_sales": 0.0},
                )
                day += timedelta(days=1)
        return [rows[day] for day in sorted(rows)]

    async def _alert_counts(self):
        alerts = self._compute_alerts()
        issues = await self._issues()
        return alert_counts(alerts, issues, self.ledger)

    async def get_business_snapshot(
        self, session: MerchantSessionContext, period: str | None = None
    ) -> BusinessSnapshot:
        await self._load()
        rows = await self._daily_rows()
        snapshot = snapshot_of(
            rows, period, currency=self.currency.upper(), alerts=await self._alert_counts()
        )
        return snapshot.model_copy(
            update={
                "note": (
                    "traffic and conversion are synthetic history; "
                    "sales and orders include live orders"
                )
            }
        )

    async def query_metrics(
        self,
        session: MerchantSessionContext,
        metric: str,
        period: str | None = None,
        granularity: str = "day",
        segment: str | None = None,
    ) -> MetricSeries:
        await self._load()
        rows = await self._daily_rows()
        current, _, label = metric_window(rows, period or "last_30_days")
        cleaned = metric.strip().lower().replace(" ", "_")
        segment_cleaned = (segment or "").strip().lower().replace(" ", "-") or None
        if cleaned not in KNOWN_METRICS:
            return MetricSeries(
                metric=cleaned,
                period=label,
                segment=segment_cleaned,
                points=[],
                note=f"no metric '{cleaned}'; available: {', '.join(sorted(KNOWN_METRICS))}",
            )

        def value_for(bucket: list[dict[str, Any]]) -> float:
            sales = sum(float(r.get("sales", 0)) for r in bucket)
            orders = sum(int(r.get("orders", 0)) for r in bucket)
            traffic = sum(int(r.get("traffic", 0)) for r in bucket)
            if segment_cleaned in {"kids-room", "kids_room"} and cleaned in {"sales", "revenue"}:
                return round(sum(float(r.get("kids_room_sales", 0)) for r in bucket), 2)
            if cleaned in {"sales", "revenue"}:
                return round(sales, 2)
            if cleaned == "orders":
                return float(orders)
            if cleaned == "traffic":
                return float(traffic)
            if cleaned in {"conversion", "conversion_rate"}:
                return round(orders / traffic * 100, 2) if traffic else 0.0
            if cleaned in {"average_order_value", "aov"}:
                return round(sales / orders, 2) if orders else 0.0
            return round(sales, 2)

        if granularity == "week":
            points = [
                MetricPoint(date=current[i]["date"], value=value_for(current[i : i + 7]))
                for i in range(0, len(current), 7)
            ]
        else:
            points = [MetricPoint(date=r["date"], value=value_for([r])) for r in current]
        note = None
        if segment_cleaned and segment_cleaned not in {"kids-room", "kids_room"}:
            note = "only the kids-room segment is broken out; other segments are not available"
            points = []
        if cleaned == "traffic":
            note = "traffic is synthetic history; live sessions are not counted"
        return MetricSeries(
            metric=cleaned,
            unit=self.currency.upper()
            if cleaned in {"sales", "revenue", "aov", "average_order_value"}
            else None,
            granularity="week" if granularity == "week" else "day",
            period=label,
            segment=segment_cleaned,
            points=points,
            note=note,
        )

    async def get_campaign_performance(
        self, session: MerchantSessionContext, campaign_id: str | None = None
    ) -> list[Campaign]:
        """Fixture campaigns plus the platform's own: a Medusa campaign reports
        its budget and what it used; revenue is not something Medusa attributes, so it
        stays None rather than a zero."""
        campaigns = list(self._campaigns.values())
        try:
            rows = await self.admin.list_all(
                "/admin/campaigns", "campaigns", fields="id,name,starts_at,ends_at,*budget"
            )
        except Exception:  # the platform has no campaign module or refused: fixtures only
            rows = []
        today = date.today().isoformat()
        for row in rows:
            budget = row.get("budget") or {}
            starts = str(row.get("starts_at") or "")[:10] or None
            ends = str(row.get("ends_at") or "")[:10] or None
            status = (
                "ended"
                if ends and ends < today
                else "draft"
                if starts and starts > today
                else "active"
            )
            campaigns.append(
                Campaign(
                    campaign_id=str(row["id"]),
                    name=str(row.get("name") or row["id"]),
                    status=status,
                    channel="medusa",
                    budget=float(budget.get("limit") or 0.0),
                    spend=float(budget["used"]) if budget.get("used") is not None else None,
                    revenue=None,
                    currency=str(budget.get("currency_code") or self.currency).upper(),
                    starts=starts,
                    ends=ends,
                )
            )
        if campaign_id:
            campaigns = [c for c in campaigns if c.campaign_id == campaign_id]
        return campaigns

    # -- catalog -----------------------------------------------------------------------

    async def search_listings(
        self,
        session: MerchantSessionContext,
        query: str,
        filters: ListingFilters | None = None,
        limit: int = 8,
    ) -> list[Listing]:
        await self._load()
        filters = filters or ListingFilters()
        terms = {_stem(w) for w in _WORD.findall(query.lower())}
        scored: list[tuple[float, Listing]] = []
        for product in self._products.values():
            listing = self._listing(product)
            haystack = " ".join(
                [
                    listing.title,
                    product.get("handle") or "",
                    listing.category or "",
                    (product.get("metadata") or {}).get("brand") or "",
                    " ".join(listing.attributes.values()),
                    product.get("description") or "",
                ]
            ).lower()
            words = {_stem(w) for w in _WORD.findall(haystack)}
            score = len(terms & words) if terms else 1
            if terms and not score:
                continue
            if filters.status and listing.status != filters.status:
                continue
            if (
                filters.category
                and filters.category.lower() not in (listing.category or "").lower()
            ):
                continue
            if filters.max_stock is not None and listing.stock > filters.max_stock:
                continue
            if filters.content_quality and listing.content_quality != filters.content_quality:
                continue
            scored.append((score, listing))
        key = {
            "sales_desc": lambda pair: (
                -(_meta(self._products[pair[1].listing_id]).get("sales_last_30d") or 0)
            ),
            "stock_asc": lambda pair: pair[1].stock,
            "price_desc": lambda pair: -pair[1].price,
            "price_asc": lambda pair: pair[1].price,
        }.get(filters.sort, lambda pair: -pair[0])
        scored.sort(key=key)
        return [listing for _, listing in scored[:limit]]

    async def get_listing(
        self, session: MerchantSessionContext, listing_id: str
    ) -> ListingDetails | None:
        await self._load()
        product = self._product(listing_id)
        if product is not None:
            return self._details(product)
        pair = self._variant(listing_id)
        if pair is None:
            return None
        variant, parent = pair
        return ListingDetails(
            **self._variant_listing(variant, parent).model_dump(),
            long_description=parent.get("description"),
        )

    # -- inventory and order health ----------------------------------------------------

    def _compute_alerts(self) -> list[InventoryAlert]:
        alerts: list[InventoryAlert] = []
        for product in self._products.values():
            row = _meta(product)
            threshold = int(row.get("threshold", self.default_threshold))
            sales_30d = row.get("sales_last_30d")
            pace = (sales_30d or 0) / 30
            family = self._is_family(product)
            for variant in self._purchasable(product):
                listing = (
                    self._variant_listing(variant, product) if family else self._listing(product)
                )
                stock = listing.stock
                visible = listing.status == "active"
                cover = round(stock / pace, 1) if pace else None
                common = dict(
                    listing_id=listing.listing_id,
                    title=listing.title,
                    option_values=listing.option_values,
                    variant_of=listing.variant_of,
                    stock=stock,
                    threshold=threshold,
                    days_of_cover=cover,
                    sales_last_30d=sales_30d,
                    storefront_visible=visible,
                )
                if stock <= threshold:
                    alerts.append(InventoryAlert(kind="low_stock", **common))
                elif cover is not None and cover > SLOW_MOVER_DAYS_OF_COVER:
                    alerts.append(InventoryAlert(kind="slow_mover", **common))
        alerts.sort(key=lambda a: (a.kind != "low_stock", -(a.sales_last_30d or 0)))
        return alerts

    async def get_inventory_alerts(self, session: MerchantSessionContext) -> list[InventoryAlert]:
        await self._load()
        return self._compute_alerts()

    async def _issues(self) -> list[OrderIssue]:
        issues: list[OrderIssue] = list(self._fixture_issues)
        now = datetime.now(UTC)
        for request in self.requests.open() if self.requests else []:
            verb = {"cancel": "Cancellation", "return": "Return", "problem": "Problem"}[
                request.action
            ]
            items = f" for {', '.join(request.item_ids)}" if request.item_ids else ""
            issues.append(
                OrderIssue(
                    issue_id=request.request_id,
                    order_id=request.order_id,
                    kind="buyer_message",
                    summary=f"{verb} requested on order {request.order_id}{items}",
                    buyer_message_excerpt=request.reason,
                    opened_at=request.created_at,
                )
            )
        for order in await self._orders():
            if str(order.get("fulfillment_status", "")).lower() not in {"not_fulfilled", ""}:
                continue
            if str(order.get("status", "")).lower() in {"canceled", "cancelled", "completed"}:
                continue
            placed = str(order.get("created_at", "")).replace("Z", "+00:00")
            try:
                opened = datetime.fromisoformat(placed)
            except ValueError:
                continue
            if now - opened < timedelta(days=DELAYED_AFTER_DAYS):
                continue
            first = (order.get("items") or [{}])[0]
            issues.append(
                OrderIssue(
                    issue_id=f"delay-{order.get('display_id', order['id'])}",
                    order_id=str(order["id"]),
                    kind="delayed",
                    summary=f"Order #{order.get('display_id')} unfulfilled for "
                    f"{(now - opened).days} days ({first.get('title', 'item')})",
                    listing_id=str(first["variant_id"]) if first.get("variant_id") else None,
                    opened_at=opened,
                )
            )
        return issues

    async def get_order_issues(self, session: MerchantSessionContext) -> list[OrderIssue]:
        return await self._issues()

    # -- pricing -----------------------------------------------------------------------

    def _pricing_context(self, listing: Listing, product: dict[str, Any]) -> PricingContext:
        row = _meta(product)
        unit_cost = row.get("unit_cost")
        sales_30d = row.get("sales_last_30d") or 0
        demand = "rising" if sales_30d >= 35 else "falling" if sales_30d <= 5 else "steady"
        margin = margin_pct(listing.price, unit_cost) if unit_cost and listing.price else None
        return PricingContext(
            listing_id=listing.listing_id,
            current_price=listing.price,
            currency=self.currency.upper(),
            unit_cost=unit_cost,
            margin_pct=margin,
            min_price=round(unit_cost * 1.15, 2) if unit_cost else None,
            min_price_basis="cost" if unit_cost else None,
            max_price=round(listing.price * 1.35, 2) if listing.price else None,
            max_price_delta_pct=self.config.max_price_delta_pct,
            max_promotion_discount_pct=self.config.max_promotion_discount_pct,
            demand_signal=demand,
            last_changed=(product.get("metadata") or {}).get("mer:last_price_change"),
            option_values=listing.option_values,
        )

    async def get_pricing_context(
        self, session: MerchantSessionContext, listing_id: str
    ) -> PricingContext | None:
        await self._load()
        resolved = self._resolve(listing_id)
        if resolved is None:
            return None
        listing, product, variant = resolved
        context = self._pricing_context(listing, product)
        if variant is None and self._is_family(product):
            context = context.model_copy(
                update={
                    "variants": [
                        self._pricing_context(self._variant_listing(v, product), product)
                        for v in self._purchasable(product)
                    ]
                }
            )
        return context

    # -- staged writes -----------------------------------------------------------------

    async def stage_listing_update(
        self,
        session: MerchantSessionContext,
        listing_id: str,
        fields: dict[str, Any],
        note: str | None = None,
    ) -> StagedChange:
        await self._load()
        self._own(session)
        resolved = self._resolve(listing_id)
        if resolved is None:
            raise ValueError(f"no listing {listing_id}")
        listing, _, variant = resolved
        if variant is not None and (shared := set(fields) & FAMILY_CONTENT_FIELDS):
            raise ChangeNotApplicable(
                f"{', '.join(sorted(shared))} is shared by every variant of "
                f"{listing.variant_of}; stage the edit against {listing.variant_of}."
            )
        items = [
            ChangeItem(
                target=listing.listing_id,
                field=name,
                before=getattr(listing, name, listing.attributes.get(name)),
                after=value,
            )
            for name, value in fields.items()
        ]
        return self.ledger.stage(
            kind=ChangeKind.LISTING_UPDATE,
            summary=note or f"Update listing content on {listing.listing_id}",
            items=items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
        )

    async def stage_price_update(
        self,
        session: MerchantSessionContext,
        items: list[PriceUpdateItem],
        note: str | None = None,
    ) -> StagedChange:
        await self._load()
        self._own(session)
        # Prices are money: two decimals, never the model's float as written (HS-M-07).
        items = [item.model_copy(update={"new_price": round(item.new_price, 2)}) for item in items]
        change_items: list[ChangeItem] = []
        margin_impact = 0.0
        costed = True
        margins: list[tuple[float, float]] = []
        notes: list[str] = []
        for item in items:
            resolved = self._resolve(item.listing_id)
            if resolved is None:
                raise ValueError(f"no listing {item.listing_id}")
            listing, product, variant = resolved
            if variant is None and self._is_family(product):
                raise ValueError(f"{listing.listing_id} is priced per variant")
            refuse_outside_range(
                listing.listing_id, item.new_price, self._pricing_context(listing, product)
            )
            row = _meta(product)
            unit_cost = row.get("unit_cost")
            pace = (row.get("sales_last_30d") or 0) / 30
            if unit_cost is None:
                costed = False
            else:
                margin_impact += (item.new_price - listing.price) * pace * 7
                before_pct, after_pct = (
                    margin_pct(listing.price, unit_cost),
                    margin_pct(item.new_price, unit_cost),
                )
                margins.append((before_pct, after_pct))
                notes.append(
                    f"{listing.listing_id} margin: {before_pct}% → {after_pct}% "
                    f"({after_pct - before_pct:+.1f} pts)"
                )
            change_items.append(
                ChangeItem(
                    target=listing.listing_id,
                    field="price",
                    before=listing.price,
                    after=item.new_price,
                )
            )
        return self.ledger.stage(
            kind=ChangeKind.PRICE_UPDATE,
            summary=note or f"Price update for {len(items)} listing(s)",
            items=change_items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
            currency=self.currency.upper(),
            margin_impact=round(margin_impact, 2) if costed else None,
            margin_before_pct=margins[0][0] if len(margins) == 1 else None,
            margin_after_pct=margins[0][1] if len(margins) == 1 else None,
            guardrail_notes=notes if len(margins) > 1 else None,
        )

    async def stage_inventory_action(
        self,
        session: MerchantSessionContext,
        items: list[InventoryActionItem],
        note: str | None = None,
    ) -> StagedChange:
        await self._load()
        self._own(session)
        change_items: list[ChangeItem] = []
        for item in items:
            resolved = self._resolve(item.listing_id)
            if resolved is None:
                raise ValueError(f"no listing {item.listing_id}")
            listing, product, variant = resolved
            if item.action == "restock":
                if variant is None and self._is_family(product):
                    raise ValueError(f"{listing.listing_id} is restocked per variant")
                if not item.quantity:
                    raise ValueError(
                        f"a restock of {listing.listing_id} needs a quantity above zero"
                    )
                change_items.append(
                    ChangeItem(
                        target=listing.listing_id,
                        field="stock",
                        before=listing.stock,
                        after=listing.stock + (item.quantity or 0),
                    )
                )
            else:
                after = "paused" if item.action == "pause" else "active"
                change_items.append(
                    ChangeItem(
                        target=listing.listing_id,
                        field="status",
                        before=listing.status,
                        after=after,
                    )
                )
        return self.ledger.stage(
            kind=ChangeKind.INVENTORY_ACTION,
            summary=note or f"Inventory action for {len(items)} listing(s)",
            items=change_items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
        )

    async def stage_promotion(
        self, session: MerchantSessionContext, promotion: PromotionDraft
    ) -> StagedChange:
        await self._load()
        self._own(session)
        if promotion.ends < date.today().isoformat():
            raise ValueError(f"the promotion already ended on {promotion.ends}")
        if promotion.ends < promotion.starts:  # ISO dates compare as text
            raise ValueError(
                f"the promotion ends ({promotion.ends}) before it starts ({promotion.starts})"
            )
        targets: list[tuple[Listing, dict[str, Any]]] = []
        for requested in promotion.listing_ids:
            resolved = self._resolve(requested)
            if resolved is None:
                raise ValueError(f"no listing {requested}")
            listing, product, variant = resolved
            if variant is None and self._is_family(product):
                for v in self._purchasable(product):
                    pair = (self._variant_listing(v, product), product)
                    if pair[0].listing_id not in {t[0].listing_id for t in targets}:
                        targets.append(pair)
            elif listing.listing_id not in {t[0].listing_id for t in targets}:
                targets.append((listing, product))
        items: list[ChangeItem] = []
        margin_impact = 0.0
        margins: list[tuple[float, float]] = []
        notes: list[str] = []
        for listing, product in targets:
            row = _meta(product)
            pace = (row.get("sales_last_30d") or 0) / 30
            discount_value = listing.price * promotion.discount_pct / 100
            margin_impact -= discount_value * pace * 7
            promo_price = round(listing.price * (1 - promotion.discount_pct / 100), 2)
            unit_cost = row.get("unit_cost") or 0.0
            floor = (
                round(unit_cost * 1.15, 2) if unit_cost else None
            )  # the pricing context's min_price
            if floor is not None and promo_price < floor:
                raise GuardrailViolation(
                    [
                        f"{listing.listing_id} at {promo_price:.2f} would be under its floor of "
                        f"{floor:.2f}; the most this listing can take is "
                        f"{(1 - floor / listing.price) * 100:.0f}%"
                    ]
                )
            if unit_cost and promo_price > 0:
                before_pct, after_pct = (
                    margin_pct(listing.price, unit_cost),
                    margin_pct(promo_price, unit_cost),
                )
                margins.append((before_pct, after_pct))
                notes.append(
                    f"{listing.listing_id} margin: {before_pct}% → {after_pct}% "
                    f"({after_pct - before_pct:+.1f} pts) for the window"
                )
            items.append(
                ChangeItem(
                    target=listing.listing_id,
                    field="promotion_price",
                    before=listing.price,
                    after=promo_price,
                )
            )
        return self.ledger.stage(
            kind=ChangeKind.PROMOTION,
            summary=(
                f"{promotion.name} ({promotion.discount_pct:.0f}% off, "
                f"{promotion.starts} to {promotion.ends})"
            ),
            items=items,
            actor=session.operator,
            actor_kind=ActorKind.AGENT,
            currency=self.currency.upper(),
            margin_impact=round(margin_impact, 2),
            margin_before_pct=margins[0][0] if len(margins) == 1 else None,
            margin_after_pct=margins[0][1] if len(margins) == 1 else None,
            guardrail_notes=notes if len(margins) > 1 else None,
        )

    async def stage_campaign(
        self, session: MerchantSessionContext, campaign: CampaignDraft
    ) -> StagedChange:
        self._own(session)
        if campaign.campaign_id and campaign.campaign_id not in self._campaigns:
            raise ChangeNotApplicable(f"no campaign {campaign.campaign_id} to change")
        return stage_campaign(
            self.ledger,
            self._campaigns,
            campaign,
            actor=session.operator,
            currency=self.currency.upper(),
        )

    def _own(self, session: MerchantSessionContext) -> None:
        """This backend fronts one merchant; a session for another sees and changes nothing
        (HS-M-01). The host binds the merchant at session start, so this is defence in
        depth for a backend called directly."""
        if session.merchant_id != self.merchant_id:
            raise ChangeNotApplicable(
                f"session is for merchant {session.merchant_id}; this store is {self.merchant_id}"
            )

    async def get_pending_changes(self, session: MerchantSessionContext) -> list[StagedChange]:
        if session.merchant_id != self.merchant_id:
            return []
        return self.ledger.pending()

    async def apply_change(self, session: MerchantSessionContext, change_id: str) -> StagedChange:
        """The platform write, then the stamp, with the change id as the idempotency key:
        a durable claim keeps a second process out, and per-item progress records let a
        retry after a crash finish without writing again."""
        self._own(session)
        change = self.ledger.get(change_id)
        if change is None or change.status != "staged":
            raise ChangeNotApplicable(f"{change_id} is not a staged change")
        if not self.ledger.claim(change_id, self.owner):
            holder = self.ledger.claimed_by(change_id)
            raise ChangeNotApplicable(
                f"{change_id} is being applied by {holder or 'another process'}; retry later"
            )
        try:
            await self._load()
            notes = self._check_freshness(change, self.ledger.progress(change_id))  # moved value
            await self._apply_to_medusa(change)  # raises on a failed write; stays staged
            if self.interrupt_before_stamp:
                raise RuntimeError("simulated crash between the platform write and the stamp")
            return self.ledger.apply(change_id, actor=session.operator, notes=notes)
        finally:
            self.ledger.release(change_id)

    def _check_freshness(self, change: StagedChange, done: dict[str, Any]) -> list[str]:
        """A price or status the platform changed since staging makes the change stale:
        refuse it and name the current value, so the operator re-stages against what is
        live. Stock is a moving number (sales), so a restock applies its delta to the
        current level and says so."""
        notes: list[str] = []
        for index, item in enumerate(change.items):
            if f"item:{index}" in done or done.get("listing") or done.get("promotion"):
                continue  # already written by an earlier attempt; the platform holds `after`
            resolved = self._resolve(item.target)
            if resolved is None:
                continue
            listing, _, _ = resolved
            if item.field == "price" and item.before is not None:
                # Stale means a third value: neither what was staged against nor what an
                # earlier, interrupted attempt already wrote.
                moved = abs(float(listing.price) - float(item.before)) >= 0.005
                already = abs(float(listing.price) - float(item.after)) < 0.005
                if moved and not already:
                    raise ChangeNotApplicable(
                        f"{item.target} price moved to {listing.price:.2f} since this change "
                        f"was staged against {float(item.before):.2f}; stage it again"
                    )
            elif item.field == "status" and item.before is not None:
                if listing.status not in {item.before, item.after}:
                    raise ChangeNotApplicable(
                        f"{item.target} is now {listing.status}, not {item.before}; stage again"
                    )
            elif item.field == "stock" and item.before is not None:
                if int(listing.stock) != int(item.before):
                    delta = int(item.after) - int(item.before)
                    notes.append(
                        f"{item.target} stock is now {listing.stock} (was {item.before} at "
                        f"staging); restocking by {delta:+d} to {int(listing.stock) + delta}"
                    )
        return notes

    async def undo_change(self, session: MerchantSessionContext, change_id: str) -> StagedChange:
        """Stage the inverse of an applied change; applying it puts the values back.
        Promotions and campaigns create platform objects and cannot be inverted here."""
        self._own(session)
        change = self.ledger.get(change_id)
        if change is None or change.status is not ChangeStatus.APPLIED:
            raise ChangeNotApplicable(f"{change_id} is not an applied change")
        if change.kind in {ChangeKind.PROMOTION, ChangeKind.CAMPAIGN}:
            raise ChangeNotApplicable(f"a {change.kind.value} cannot be undone here")
        if "undone_by" in self.ledger.progress(change_id):
            raise ChangeNotApplicable(f"{change_id} was already undone")
        inverse = [
            ChangeItem(target=i.target, field=i.field, before=i.after, after=i.before)
            for i in change.items
        ]
        staged = self.ledger.stage(
            kind=change.kind,
            summary=f"Undo {change_id}: {change.summary}",
            items=inverse,
            actor=session.operator,
            actor_kind=ActorKind.OPERATOR,
            currency=change.currency,
        )
        self.ledger.record_progress(change_id, "undone_by", staged.change_id)
        return staged

    async def schedule_change(
        self, session: MerchantSessionContext, change_id: str, at: datetime
    ) -> StagedChange:
        """The operator's decision to apply a staged change at a time."""
        self._own(session)
        self.ledger.schedule(change_id, at=at, by=session.operator)
        change = self.ledger.get(change_id)
        assert change is not None
        return change

    async def apply_due_changes(
        self, session: MerchantSessionContext, now: datetime | None = None
    ) -> list[StagedChange]:
        """Apply every scheduled change whose time has come; a refusal (stale, guardrail)
        leaves it staged and unscheduled so it does not retry forever."""
        applied: list[StagedChange] = []
        for change_id in self.ledger.due(now or datetime.now(UTC)):
            try:
                applied.append(await self.apply_change(session, change_id))
            except ChangeNotApplicable:
                self.ledger.unschedule(change_id)
                raise
            self.ledger.unschedule(change_id)
        return applied

    async def discard_change(
        self,
        session: MerchantSessionContext,
        change_id: str,
        actor_kind: ActorKind = ActorKind.OPERATOR,
    ) -> StagedChange:
        self._own(session)
        return self.ledger.discard(change_id, actor=session.operator, actor_kind=actor_kind)

    # -- order requests ---------------------------------------------------------

    async def resolve_order_request(
        self,
        session: MerchantSessionContext,
        request_id: str,
        decision: Decision,
        note: str | None = None,
    ) -> OrderActionRequest:
        """The operator's decision on a shopper's request; an approved cancellation is
        written to the platform here, on the merchant's side, never by the shopper."""
        self._own(session)
        if self.requests is None:
            raise ChangeNotApplicable("this store keeps no order requests")
        request = self.requests.get(request_id)
        if request is None or request.status != "requested":
            raise ChangeNotApplicable(f"{request_id} is not an open request")
        if decision == "approved" and request.action == "cancel":
            await self.admin.post(f"/admin/orders/{request.order_id}/cancel", {})
        resolved = self.requests.resolve(
            request_id, decision=decision, by=session.operator, note=note
        )
        if resolved is None:
            raise ChangeNotApplicable(f"{request_id} was resolved by someone else")
        return resolved

    # -- the platform writes -----------------------------------------------------------

    def _variant_for_write(self, target: str) -> tuple[dict[str, Any], dict[str, Any]]:
        pair = self._variant(target)
        if pair is not None:
            return pair
        product = self._product(target)
        if product is None or self._is_family(product) or not self._purchasable(product):
            raise ChangeNotApplicable(f"{target} has no single variant to write to")
        return self._purchasable(product)[0], product

    async def _apply_to_medusa(self, change: StagedChange) -> None:
        """Every write is recorded in the ledger's progress table under a key per item as
        soon as it lands, and skipped when the key is already there, so the same change
        applied twice (a retry, a second host) writes once."""
        done = self.ledger.progress(change.change_id)

        def mark(key: str, value: Any = True) -> None:
            done[key] = value
            self.ledger.record_progress(change.change_id, key, value)

        if change.kind is ChangeKind.PRICE_UPDATE:
            for index, item in enumerate(change.items):
                if f"item:{index}" in done:
                    continue
                variant, product = self._variant_for_write(item.target)
                prices = [
                    {"currency_code": p["currency_code"], "amount": p["amount"]}
                    for p in variant.get("prices") or []
                    if str(p.get("currency_code", "")).lower() != self.currency
                ]
                prices.append({"currency_code": self.currency, "amount": float(item.after)})
                await self.admin.post(
                    f"/admin/products/{product['id']}/variants/{variant['id']}", {"prices": prices}
                )
                await self.admin.post(
                    f"/admin/products/{product['id']}",
                    {
                        "metadata": {
                            **(product.get("metadata") or {}),
                            "mer:last_price_change": date.today().isoformat(),
                        }
                    },
                )
                mark(f"item:{index}")
        elif change.kind is ChangeKind.INVENTORY_ACTION:
            for index, item in enumerate(change.items):
                if f"item:{index}" in done:
                    continue
                if item.field == "stock":
                    variant, _ = self._variant_for_write(item.target)
                    delta = int(item.after) - int(item.before or 0)
                    for entry in variant.get("inventory_items") or []:
                        level = self._levels.get(str(entry["inventory_item_id"]))
                        if level is None or self._location_id is None:
                            raise ChangeNotApplicable(
                                f"{item.target} has no inventory level to restock"
                            )
                        await self.admin.post(
                            f"/admin/inventory-items/{entry['inventory_item_id']}/location-levels/{self._location_id}",
                            {"stocked_quantity": int(level.get("stocked_quantity") or 0) + delta},
                        )
                elif item.field == "status":
                    resolved = self._resolve(item.target)
                    if resolved is None:
                        raise ChangeNotApplicable(f"{item.target} is unknown")
                    _, product, _ = resolved
                    status = "draft" if item.after == "paused" else "published"
                    await self.admin.post(f"/admin/products/{product['id']}", {"status": status})
                mark(f"item:{index}")
        elif change.kind is ChangeKind.LISTING_UPDATE:
            if "listing" in done:
                return
            resolved = self._resolve(change.items[0].target) if change.items else None
            if resolved is None:
                raise ChangeNotApplicable("nothing to update")
            _, product, _ = resolved
            body: dict[str, Any] = {}
            metadata = dict(product.get("metadata") or {})
            for item in change.items:
                if item.field in {"title", "description"}:
                    body[item.field] = item.after
                elif item.field == "short_description":
                    body["subtitle"] = item.after
                else:
                    metadata[f"{ATTRIBUTE_PREFIX}{item.field}"] = item.after
            body["metadata"] = metadata
            await self.admin.post(f"/admin/products/{product['id']}", body)
            mark("listing")
        elif change.kind is ChangeKind.PROMOTION:
            if "promotion" in done:
                return
            product_ids: list[str] = []
            for item in change.items:
                _, product = self._variant_for_write(item.target)
                if product["id"] not in product_ids:
                    product_ids.append(product["id"])
            first = change.items[0]
            pct = (
                round((1 - float(first.after) / float(first.before)) * 100, 2)
                if first.before
                else 0.0
            )
            code = re.sub(r"[^A-Z0-9]+", "", change.summary.split("(")[0].upper())[:20] or "PROMO"
            code = f"{code}{change.change_id[-4:]}"
            created = await self.admin.post(
                "/admin/promotions",
                {
                    "code": code,
                    "type": "standard",
                    "is_automatic": True,
                    "status": "active",
                    "application_method": {
                        "type": "percentage",
                        "target_type": "items",
                        "allocation": "each",
                        "max_quantity": 100,
                        "value": pct,
                        "currency_code": self.currency,
                        "target_rules": [
                            {
                                "attribute": "items.product.id",
                                "operator": "in",
                                "values": product_ids,
                            }
                        ],
                    },
                },
            )
            mark("promotion", {"id": (created.get("promotion") or {}).get("id"), "code": code})
        elif change.kind is ChangeKind.CAMPAIGN:
            if "campaign" in done:
                return
            budget = next(
                (i for i in change.items if i.field == "budget" and i.after is not None), None
            )
            target = change.items[0].target
            if budget is not None:
                identifier = re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")[:40]
                await self.admin.post(
                    "/admin/campaigns",
                    {
                        "name": target[:80],
                        "campaign_identifier": f"{identifier}-{change.change_id}",
                        "budget": {
                            "type": "spend",
                            "limit": float(budget.after),
                            "currency_code": self.currency,
                        },
                    },
                )
                existing = self._campaigns.get(target)
                if existing is not None:
                    self._campaigns[target] = existing.model_copy(
                        update={"budget": float(budget.after)}
                    )
            mark("campaign")

    # -- context -----------------------------------------------------------------------

    def _waitlist_by_listing(self) -> dict[str, int]:
        """Waiting customers per listing: variants roll up to their product."""
        counts: dict[str, int] = {}
        for product_id, n in (self.waitlist.counts() if self.waitlist else {}).items():
            pair = self._variants.get(product_id)
            listing = str(pair[1]["id"]) if pair else product_id
            counts[listing] = counts.get(listing, 0) + n
        return counts

    async def get_analysis_schema(self, session: MerchantSessionContext) -> str | None:
        return SCHEMA_NOTE if self.analysis is not None else None

    async def execute_analysis_query(
        self, session: MerchantSessionContext, sql: str
    ) -> AnalysisTable | None:
        if self.analysis is None:
            return None
        self._own(session)
        return await self.analysis.query(sql)

    async def get_merchant_context(self, session: MerchantSessionContext) -> dict[str, Any] | None:
        await self._load()
        counts = await self._alert_counts()
        rows = await self._daily_rows()
        latest = rows[-1]["date"] if rows else date.today().isoformat()
        week_start = (date.fromisoformat(latest) - timedelta(days=6)).isoformat()
        return {
            "store": self.store_name,
            "operator": session.operator,
            "current_period": f"{week_start}/{latest}",
            "catalog_size": len(self._products),
            "limitations": [
                DataLimitation(
                    source="traffic",
                    note="traffic and conversion come from synthetic history, not live sessions",
                ).model_dump(),
                DataLimitation(
                    source="campaigns",
                    note="campaign spend and revenue are not reported by this store",
                ).model_dump(),
            ],
            "alerts": counts.model_dump(),
            "waitlist": self._waitlist_by_listing(),
        }
