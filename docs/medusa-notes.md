# Medusa v2 notes for the adapters

Learned on 2026-09-04 against Medusa 2.20.1. Store API unless stated.

## Auth and keys

- Every Store request needs `x-publishable-api-key`; the key must be linked to a sales
  channel (`POST /admin/api-keys/{id}/sales-channels`) or products come back empty.
- Customer JWT: `POST /auth/customer/emailpass/register` then `POST /store/customers`
  with that token, then `POST /auth/customer/emailpass` for a login token. The token goes
  on `Authorization: Bearer`.
- Admin JWT: `POST /auth/user/emailpass`.

## Products

- The default product response is thin; ask for fields explicitly. The adapter uses
  `PRODUCT_FIELDS` in `src/commerce_medusa/medusa_mapping.py`.
- Prices are per region: pass `region_id` and request `*variants.calculated_price`;
  `calculated_amount` is a plain decimal (10 means EUR 10.00, not cents).
- Stock: `+variants.inventory_quantity`, `+variants.manage_inventory`,
  `+variants.allow_backorder`.
- Filter by variant: `GET /store/products?variants.id=<variant id>`. There is no
  `variant_id` filter and no `/store/variants` endpoint.
- Relation depth is capped at three: `*items.variant.options` works,
  `*items.variant.options.option` is rejected. So cart and order lines never carry option
  titles, only `option_id` and `value`; the adapter resolves titles from the family
  record and caches them per variant.
- Text search is `q=` and matches titles; there is no relevance score.
- Categories, collections, tags and type need `*categories` etc. in `fields`.

## Carts and orders

- `POST /store/carts {region_id, email?}` creates a cart; one per agent session in the
  adapter. `POST /store/carts/{id}/line-items {variant_id, quantity}` adds;
  `POST /store/carts/{id}/line-items/{line_id} {quantity}` updates;
  `DELETE` returns the cart under `parent`.
- Completing a cart needs a shipping address, a shipping method
  (`GET /store/shipping-options?cart_id=`, `POST .../shipping-methods {option_id}`), a
  payment collection (`POST /store/payment-collections {cart_id}`), a payment session
  (`POST /store/payment-collections/{id}/payment-sessions {provider_id}`), then
  `POST /store/carts/{id}/complete` which returns `{type: "order", order}`. The seed
  installs the manual provider `pp_system_default`; the money moves on Stripe and the
  manual provider completes the cart (see below).
- `GET /store/orders` is the customer's own orders (JWT required). Order line items have
  `variant_id`, `product_id`, `product_title`, `variant_title`, `unit_price`, `quantity`.
- Inventory is decremented on order completion (verified: 1,000,000 to 999,999).

## Process

- `medusa develop` starts on :9000, admin at `/app`; without Redis it says
  "A fake redis instance will be used" and works for a single process.
- `create-medusa-app` prompts for the Next.js storefront even with `--no-browser`; pipe
  `n`. `--no-migrations` skips the admin-user prompt; then `npx medusa db:migrate` (which
  also runs the seed migration script) and `npx medusa user -e ... -p ...`.

## Learned while wiring checkout (2026-09-04, later)

- **Shipping profiles.** A product created through the Admin API without
  `shipping_profile_id` cannot be checked out: cart completion fails with "The cart items
  require shipping profiles that are not satisfied by the current shipping methods", even
  though `GET /store/shipping-options?cart_id=` still lists options. The importer now sets
  the default profile on create and on `--update-metadata`.
- **Completion sequence that works:** `POST /store/carts/{id}` with `email`,
  `shipping_address`, `billing_address`; `POST .../shipping-methods {option_id}`;
  `POST /store/payment-collections {cart_id}`; `POST /store/payment-collections/{id}/
  payment-sessions {provider_id: "pp_system_default", data: {reference}}`;
  `POST /store/carts/{id}/complete` -> `{type: "order", order}`. Stock decrements on
  completion. The manual provider is right when the money already moved on Stripe.
- **Automatic promotions** need `application_method.max_quantity` with allocation
  `each`; they discount the cart (`discount_total`, line `adjustments`) but do not change
  `calculated_price` on the product, so a product card still shows the list price.
- **Campaigns** (`POST /admin/campaigns`) hold a spend budget and identifier; they are
  not marketing campaigns in the merchant agent's sense, so the adapter records the draft
  and only writes a Medusa campaign for the budget line.

## Store orders by id are unscoped by design

`GET /store/orders/:id` serves any order to any signed-in customer; Medusa calls this
intentional (advisory GHSA-gxxh-4g7f-vxp5, 2026-09-10) and documents restricting it in the
application. `platform/medusa-backend/src/api/middlewares.ts` does so (authenticate the
customer, 404 unless the order is theirs). Any Medusa adapter for this agent needs one of
the two guards; this one keeps both.
