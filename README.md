# commerce-agents-medusa

Anthropic's [Claude Commerce Agents](https://github.com/anthropics/commerce-agents) over
a real store: a `StorefrontBackend` and a `MerchantBackend` for
[Medusa v2](https://medusajs.com), hosted checkout and order placement through Stripe,
a change ledger and sessions that survive a restart, shopper order requests, and a host
that serves the reference storefront and merchant portal unchanged with both agents on the
Claude Agent SDK.

The reference repository ships four mock backends and states that it "is not maintained
and does not accept contributions". This is the adapter it does not have, built test-first
against the contract and checked with
[commerce-agents-conformance](https://github.com/vishkaty/commerce-agents-conformance),
offline over recorded Medusa and Stripe responses and live against a running store.

## What is here

| Module | What it does |
|---|---|
| `commerce_medusa.medusa_storefront` | `StorefrontBackend` over the Store API: search, families and variants, carts per session, orders scoped to the signed-in customer, policies, fulfillment options, promo codes, disclosures from listing metadata, guest-cart merge, waitlist, order requests; the cart is re-validated at checkout (`CartStale`) |
| `commerce_medusa.medusa_merchant` | `MerchantBackend` over the Admin API: listings, alerts, metrics (live orders plus the reference fixture history), staged changes applied with freshness checks, undo, scheduling, campaigns, order-request resolution |
| `commerce_medusa.ledger`, `sessions`, `order_requests` | SQLite: a change ledger with claim and per-item progress (an apply is idempotent across processes and crashes), session and cart maps, shoppers' requests and waitlists |
| `commerce_medusa.order_placement`, `stripe_checkout` | a Stripe Checkout Session per cart; the paid event places the Medusa order once, held when the amount does not match the cart |
| `commerce_medusa.analysis` | the merchant's read-only analysis replica over Postgres (`[analysis]` extra) |
| `commerce_medusa.host` | the reference `demo_common` routes over these adapters, the Stripe webhook and return pages, the order-action tools, and a paid order announced on the shopper's next turn |
| `scripts/` | Medusa bootstrap, catalog import, seeding, two end-to-end checks against a live store, two SDK consoles |
| `platform/medusa-backend/` | the two files a stock Medusa app needs |
| `demo_common`, `commerce_medusa/skills`, `commerce_medusa/data/retail` | vendored from the reference at the pin so the package runs on its own |

## Install and run

```
pip install "commerce-agents-medusa @ git+https://github.com/vishkaty/commerce-agents-medusa"
cp .env.example .env            # Medusa keys; Stripe test keys for checkout
python -m uvicorn --factory commerce_medusa.host.store:default_app --port 8010
```

Nothing else is needed: the reference host routes, both agents' skills and the retail
example fixtures are vendored in the package (see `NOTICE`), and the reference packages
install from Anthropic's repository at the pinned commit. The agents run on the Claude
Agent SDK, so a Claude login is enough; no API key is read. Only the reference web apps
(`make web`, the storefront on :3000 and the portal on :3100) need a checkout of the
reference repository, which `make upstream` fetches.

No Medusa store yet? `scripts/bootstrap_medusa.sh` creates one against any Postgres 16
(`DATABASE_URL`) with `create-medusa-app`, which seeds a region, a sales channel, a
publishable key and shipping options itself, adds the order-ownership middleware, runs the
migrations, creates the admin user, and prints what to put in `.env`. `docker-compose.yml` starts Postgres and Redis
for it. Then `make import-catalog` loads the packaged retail catalog and `make seed` adds
the customers, orders and listings the tests use.

## Verified how

- Offline: 138 tests over recorded Medusa and Stripe responses (`make test`, no checkout needed), including
  a webhook and success page racing on one cart, an apply interrupted before its stamp
  and finished by a retry, and two processes racing on one session.
- Live: the same adapters against Medusa 2.20.1 with a Stripe test sandbox, through the
  conformance suite (137 statements) and two end-to-end scripts (`scripts/e2e_checkout.py`,
  `scripts/e2e_session_notice.py`).
- Findings against the platform and the reference are recorded where they were sent:
  Medusa's `GET /store/orders/:id` is open by design (their advisory reply; the middleware
  here closes it), and the reference's gaps are the pull requests linked from the
  conformance package.

## License

Apache-2.0; see `NOTICE` for what comes from the reference implementation and Medusa.
