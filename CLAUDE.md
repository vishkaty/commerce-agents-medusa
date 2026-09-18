# commerce-agents-medusa

Claude Commerce Agents over Medusa v2: a storefront backend adapter, a merchant
adapter, a durable ledger and sessions, Stripe order placement, and a host that runs
the agents on the Claude Agent SDK. Public, Apache-2.0. Global rules are in
`~/.claude/CLAUDE.md`; contributor rules are in `CONTRIBUTING.md`.

## How to run

- `make test` runs the unit suite. Tests marked `medusa` need a live store and skip
  without one; that skip is expected, not a failure.
- `make lint` runs ruff check and format. Both must pass before a commit.
- `make import-catalog [ARGS=...]` imports the retail catalog; use the make target,
  not the script directly, because it sets the upstream path.
- Scripts under `scripts/` run with `.venv/bin/python scripts/<name>.py` and read
  configuration from `.env` (see `.env.example`). No API key is read; the agents run
  on the Agent SDK and its own login.

## Layout

- `src/commerce_medusa/` the adapters, host, ledger, sessions, and analysis.
- `platform/medusa-backend/` the Medusa application for a local store (large,
  rebuildable).
- `docs/medusa-notes.md` notes on Medusa behaviour the adapters depend on.

## Conventions

- This package was extracted from a private lab. Nothing here may reference that lab:
  no numbered doc pointers, no `lab/` paths, no internal finding ids, no machine or
  login details. Scan before committing anything copied in.
- Commits are authored by a named person with a real email address.
