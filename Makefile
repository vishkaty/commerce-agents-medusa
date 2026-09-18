PY := .venv/bin/python
UPSTREAM ?= /tmp/commerce-agents
NOCC := env -u CLAUDECODE

setup: upstream     ## venv with the package, the reference packages (the two SDK runtimes editable from the checkout), and the dev tools
	test -d .venv || python3 -m venv .venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -e ".[dev]"
	$(PY) -m pip install -q -e $(UPSTREAM)/shopping-agent/runtime-agent-sdk -e $(UPSTREAM)/merchant-agent/runtime-agent-sdk

upstream:           ## a checkout of the reference implementation at the pinned commit
	test -d $(UPSTREAM) || git clone -q https://github.com/anthropics/commerce-agents.git $(UPSTREAM)
	git -C $(UPSTREAM) checkout -q fd4d59224ab96b43c6dc6888207c67b3bd5a24cf

test: upstream      ## offline tests over recorded Medusa and Stripe responses (live ones skip)
	COMMERCE_AGENTS=$(UPSTREAM) PYTHONPATH=$(UPSTREAM)/examples $(PY) -m pytest -q -p no:cacheprovider

test-live:          ## the same plus the tests that need a running Medusa (reads .env)
	COMMERCE_AGENTS=$(UPSTREAM) PYTHONPATH=$(UPSTREAM)/examples CONFORMANCE_LIVE_WRITES=1 $(PY) -m pytest -q -p no:cacheprovider

lint:
	$(PY) -m ruff check . && $(PY) -m ruff format --check .

host:               ## the host on :8010 (reads .env)
	COMMERCE_AGENTS=$(UPSTREAM) PYTHONPATH=$(UPSTREAM)/examples $(NOCC) $(PY) -m uvicorn --factory commerce_medusa.host.store:default_app --port 8010

web: upstream       ## the reference storefront (:3000) and portal (:3100) pointed at the host
	@echo "storefront http://localhost:3000  portal http://localhost:3100  (Ctrl-C stops both)"
	(cd $(UPSTREAM)/examples/retail/storefront-web && NEXT_PUBLIC_API_URL=http://localhost:8010 ../../node_modules/.bin/next dev --port 3000) & \
	(cd $(UPSTREAM)/examples/retail/merchant-web && NEXT_PUBLIC_API_URL=http://localhost:8010 ../../node_modules/.bin/next dev --port 3100) & \
	wait

import-catalog: upstream   ## load the reference retail catalog into Medusa through the Admin API
	COMMERCE_AGENTS=$(UPSTREAM) PYTHONPATH=$(UPSTREAM)/examples $(PY) scripts/import_retail_catalog.py $(ARGS)

seed:               ## a second customer with an order, a sold-out variant, a promo code, a disclosure
	$(PY) scripts/seed_lab_customers.py

e2e:                ## add a product, create a real Checkout Session, deliver the paid event, see the order
	COMMERCE_AGENTS=$(UPSTREAM) PYTHONPATH=$(UPSTREAM)/examples $(NOCC) $(PY) scripts/e2e_checkout.py

stripe-listen:      ## forward Stripe test events to the host
	STRIPE_API_KEY=$$STRIPE_SECRET_KEY stripe listen --forward-to localhost:8010/webhooks/stripe --events checkout.session.completed

.PHONY: setup upstream test test-live lint host web import-catalog seed e2e stripe-listen
