# The Medusa side

The adapters talk to a stock Medusa v2 application (tested against 2.20.1). Create one
with `npx create-medusa-app@latest`, then copy these two files into its `src/`:

- `src/api/middlewares.ts`: `GET /store/orders/:id` requires a signed-in customer and
  answers 404 unless the order is theirs. Medusa leaves that route open by design so a
  guest can view their confirmation page (its "Restrict Order Retrieval" guide prescribes
  this middleware); the adapter also checks ownership itself, so a store without the
  middleware is safe too.
- `src/migration-scripts/initial-data-seed.ts`: the seed the host was verified against
  (a region, a sales channel, a publishable key, shipping options). `create-medusa-app`
  now seeds a store of its own on creation (an EUR region, a sales channel, a publishable
  key, shipping options, a few products), which is enough, so `scripts/bootstrap_medusa.sh`
  does not run this one; use it with `npx medusa exec` on a store created another way.

`scripts/bootstrap_medusa.sh` does the whole of this against any Postgres 16 and prints
what goes into `.env`. Then `scripts/import_retail_catalog.py` loads the packaged retail
catalog through the Admin API, and `scripts/seed_lab_customers.py` adds the customers,
orders and listings the tests and the conformance suite need.
