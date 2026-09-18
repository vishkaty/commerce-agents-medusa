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
| `scripts/` | catalog import, seeding, two end-to-end checks against a live store, two SDK consoles |
| `platform/medusa-backend/` | the two files a stock Medusa app needs |

## Install and run

```
git clone https://github.com/vishkaty/commerce-agents-medusa && cd commerce-agents-medusa
make setup                      # venv; clones the reference repo to /tmp/commerce-agents at the pin
cp .env.example .env            # fill in the Medusa keys; Stripe test keys for checkout
make host                       # the host on :8010
make web                        # the reference storefront (:3000) and portal (:3100)
```

The package needs a checkout of the reference repository at the pinned commit
(`fd4d592`): its `examples/` directory is not a package and supplies `demo_common` (the
routes) and the retail fixtures, and the two Agent SDK runtimes find their skills next to
themselves there, so `make setup` installs those two editable from the checkout
(`UPSTREAM=` to point elsewhere). The agents run on the
Claude Agent SDK, so a Claude login is enough; no API key is read.

## Verified how

- Offline: 138 tests over recorded Medusa and Stripe responses (`make test`), including
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
