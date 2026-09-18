"""End-to-end checkout without the model: cart -> Stripe Checkout Session -> paid event
-> webhook -> Medusa order -> visible in the shopper's orders and the merchant snapshot.

    python scripts/e2e_checkout.py     # needs Medusa (:9000), the lab host (:8010), and
                                   # stripe listen --forward-to localhost:8010/webhooks/stripe

The paid event comes from ``stripe trigger checkout.session.completed`` with this cart's
id in the session metadata, so Stripe's own delivery path is exercised; the hosted
payment page itself is a browser step (open the printed URL and pay with 4242 4242 4242
4242 to run that path instead).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import httpx  # noqa: E402
from merchant_agent import MerchantAgentConfig, MerchantSessionContext  # noqa: E402
from shopping_agent import ShoppingSessionContext  # noqa: E402

from commerce_medusa.medusa_admin import MedusaAdmin  # noqa: E402
from commerce_medusa.medusa_client import MedusaClient  # noqa: E402
from commerce_medusa.medusa_merchant import MedusaMerchant  # noqa: E402
from commerce_medusa.medusa_storefront import CustomerDirectory, MedusaStorefront  # noqa: E402
from commerce_medusa.order_placement import ShippingAddress  # noqa: E402
from commerce_medusa.settings import LabSettings  # noqa: E402
from commerce_medusa.stripe_checkout import StripeClient  # noqa: E402

CUSTOMER = os.environ.get("CUSTOMER_ID", "customer")
OPERATOR = os.environ.get("OPERATOR", "operator")

STRIPE = os.path.expanduser("~/.local/bin/stripe")
TENT_HANDLE = "ar-1201"
ADDRESS = ShippingAddress("Priya", "Lab", "1 Main St", "Austin", "78701", "US", "tx")


def step(text: str) -> None:
    print(f"\n== {text}")


async def main() -> int:
    settings = LabSettings.load()
    for name in ("medusa_publishable_key", "stripe_secret_key", "stripe_webhook_secret"):
        if not getattr(settings, name):
            sys.exit(f"{name} missing in .env")
    try:
        httpx.get(f"{settings.lab_host_url}/health", timeout=2).raise_for_status()
    except httpx.HTTPError:
        sys.exit(f"lab host not reachable at {settings.lab_host_url}; run: make host")

    client = MedusaClient(settings.medusa_url, settings.medusa_publishable_key)
    customers = CustomerDirectory()
    await customers.login(
        client,
        CUSTOMER,
        settings.lab_customer_email,
        settings.lab_customer_password,
        address=ADDRESS,
    )
    region = await client.first_region_id(settings.lab_currency)
    stripe = StripeClient(settings.stripe_secret_key)
    storefront = MedusaStorefront(
        client,
        region_id=region,
        customers=customers,
        policies_path=settings.data_file("policies.json"),
        stripe=stripe,
        host_url=settings.lab_host_url,
    )
    admin = MedusaAdmin(client, settings.medusa_admin_email, settings.medusa_admin_password)
    merchant = MedusaMerchant(
        admin,
        config=MerchantAgentConfig(brand_name="Lab Store"),
        currency=settings.lab_currency,
        fixtures_dir=settings.fixtures_dir,
    )
    shopper = ShoppingSessionContext(session_id=f"e2e-{int(time.time())}", user_id=CUSTOMER)
    operator = MerchantSessionContext(
        session_id="e2e-m", merchant_id="lab-store", operator=OPERATOR
    )

    today = time.strftime("%Y-%m-%d")
    step("merchant snapshot before (today)")
    before = await merchant.get_business_snapshot(operator, f"{today}/{today}")
    orders_before = len(await storefront.get_orders(shopper, limit=50))
    print(f"orders in snapshot: {before.orders}; shopper orders: {orders_before}")

    step("shopper: search, details, add to cart")
    results = await storefront.search_products(shopper, "2-person backpacking tent")
    tent = next(p for p in results if p.attributes.get("sku") == "AR-1201")
    details = await storefront.get_product_details(shopper, tent.product_id)
    assert details is not None
    stock_before = await merchant.get_listing(operator, tent.product_id)
    cart = await storefront.add_to_cart(shopper, tent.product_id, 1)
    lines = [(i.title, i.quantity, i.price) for i in cart.items]
    print(f"cart: {lines} subtotal {cart.subtotal} {cart.currency}")

    step("checkout handoff: Stripe Checkout Session")
    handoffs = await storefront.checkout_handoff(shopper, cart)
    assert handoffs, "no handoff; is STRIPE_SECRET_KEY set?"
    cart_id = storefront._carts[shopper.session_id]
    print("hosted checkout URL (open in a browser to pay with 4242 4242 4242 4242):")
    print(f"  {handoffs[0].url}")
    print(f"medusa cart: {cart_id}")

    step("deliver the paid event for the real Checkout Session, signed, to the host")
    # The handoff created a real Checkout Session in the sandbox (the URL above). Paying it
    # takes a browser, and `stripe trigger` pays a fixture session of its own at 30.00,
    # which the host now holds as an amount mismatch. So the paid event is built
    # for our session, with the total the host will see after it adds shipping, and
    # delivered signed with the webhook secret, the way Stripe would deliver it.
    from commerce_medusa.order_placement import cheapest_shipping_option
    from commerce_medusa.stripe_checkout import sign_webhook

    option = await cheapest_shipping_option(client, cart_id, customers.token(CUSTOMER))
    shipping = option.get("amount")
    if shipping is None:
        shipping = (option.get("calculated_price") or {}).get("calculated_amount") or 0
    paid_cents = round((cart.subtotal + float(shipping)) * 100)
    session_id = handoffs[0].url.rsplit("/", 1)[-1].split("#")[0]
    print(
        f"paying {paid_cents} cents (items {cart.subtotal} + shipping {shipping}) on {session_id}"
    )
    event = {
        "id": f"evt_lab_{int(time.time())}",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": session_id,
                "payment_status": "paid",
                "amount_total": paid_cents,
                "currency": cart.currency.lower(),
                "metadata": {
                    "medusa_cart_id": cart_id,
                    "lab_session_id": "e2e",
                    "lab_user_id": CUSTOMER,
                },
                "customer_details": {"email": settings.lab_customer_email},
            }
        },
    }
    payload = json.dumps(event).encode()
    response = httpx.post(
        f"{settings.lab_host_url}/webhooks/stripe",
        content=payload,
        headers={"stripe-signature": sign_webhook(payload, settings.stripe_webhook_secret)},
        timeout=60,
    )
    print(f"webhook -> {response.status_code} {response.text[:160]}")
    if response.status_code != 200 or response.json().get("held"):
        return 1

    step("wait for the order")
    placed = None
    for _ in range(30):
        orders = await storefront.get_orders(shopper, limit=50)
        if len(orders) > orders_before:
            placed = orders[0]
            break
        await asyncio.sleep(1)
    if placed is None:
        print("no new order after 30s; check the host log and `stripe listen`")
        return 1
    items = [(i.title, i.quantity) for i in placed.items]
    print(f"order {placed.order_id}: {placed.status} total {placed.total} {placed.currency}")
    print(f"  items {items}")

    step("merchant sees it")
    after = await merchant.get_business_snapshot(operator, f"{today}/{today}")
    stock_after = await merchant.get_listing(operator, tent.product_id)
    print(f"orders in today's snapshot: {before.orders} -> {after.orders}")
    was = stock_before.stock if stock_before else "?"
    now = stock_after.stock if stock_after else "?"
    print(f"tent stock: {was} -> {now}")
    await client.aclose()
    await stripe.aclose()
    ok = (
        after.orders == before.orders + 1
        and stock_after is not None
        and stock_before is not None
        and stock_after.stock == stock_before.stock - 1
    )
    print("\nRESULT:", "PASS" if ok else "CHECK")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
