"""Settings from the environment and a ``.env`` in the working directory (never committed).
Only what the adapters, the host and the scripts need; the Anthropic credential is
deliberately absent (the agents run on the Claude Agent SDK's own login or the SDK's
credential chain)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path

from dotenv import dotenv_values

ENV_KEYS = {
    "MEDUSA_URL", "MEDUSA_PUBLISHABLE_KEY", "MEDUSA_ADMIN_EMAIL", "MEDUSA_ADMIN_PASSWORD",
    "LAB_CUSTOMER_EMAIL", "LAB_CUSTOMER_PASSWORD", "STRIPE_SECRET_KEY", "DATABASE_URL_RO",
    "STRIPE_WEBHOOK_SECRET", "STRIPE_WEBHOOK_SECRETS", "LAB_CURRENCY", "LAB_HOST_URL",
    "CUSTOMER_ID", "CUSTOMER_NAME", "OPERATOR", "STORE_NAME", "DATA_DIR", "COMMERCE_AGENTS",
}  # fmt: skip


@dataclass(frozen=True)
class LabSettings:
    medusa_url: str = "http://localhost:9000"
    medusa_publishable_key: str = ""
    medusa_admin_email: str = ""
    medusa_admin_password: str = ""
    lab_customer_email: str = ""
    lab_customer_password: str = ""
    stripe_secret_key: str = ""
    database_url_ro: str = ""
    stripe_webhook_secret: str = ""
    stripe_webhook_secrets: str = ""  # comma separated: real endpoint(s) besides the CLI listener
    lab_currency: str = "usd"
    lab_host_url: str = "http://localhost:8010"
    # Identities the host stamps: the single customer the demo profiles map to, and the
    # operator name on merchant approvals.
    customer_id: str = "customer"
    customer_name: str = "Customer"
    operator: str = "operator"
    store_name: str = "Demo Store"
    # Where the host keeps its files (ledger, sessions, requests, memory, processed events).
    data_dir: Path = field(default_factory=lambda: Path("data"))
    # A checkout of anthropics/commerce-agents: its ``examples/retail/data`` supplies the
    # merchant fixtures (metrics history, campaigns, issues); optional.
    commerce_agents: Path | None = None

    @classmethod
    def load(cls, env_path: Path | None = None) -> LabSettings:
        """Values from ``.env`` (the working directory's unless ``env_path`` is given),
        overridden by the process environment."""
        env_path = env_path or Path(".env")
        values = dict(dotenv_values(env_path)) if env_path.exists() else {}
        values.update({k: v for k, v in os.environ.items() if k in ENV_KEYS})

        def get(key: str, default: str = "") -> str:
            return (values.get(key) or default).strip()

        return cls(
            medusa_url=get("MEDUSA_URL", cls.medusa_url).rstrip("/"),
            medusa_publishable_key=get("MEDUSA_PUBLISHABLE_KEY"),
            medusa_admin_email=get("MEDUSA_ADMIN_EMAIL"),
            medusa_admin_password=get("MEDUSA_ADMIN_PASSWORD"),
            lab_customer_email=get("LAB_CUSTOMER_EMAIL"),
            lab_customer_password=get("LAB_CUSTOMER_PASSWORD"),
            stripe_secret_key=get("STRIPE_SECRET_KEY"),
            database_url_ro=get("DATABASE_URL_RO"),
            stripe_webhook_secret=get("STRIPE_WEBHOOK_SECRET"),
            stripe_webhook_secrets=get("STRIPE_WEBHOOK_SECRETS"),
            lab_currency=get("LAB_CURRENCY", cls.lab_currency).lower(),
            lab_host_url=get("LAB_HOST_URL", cls.lab_host_url).rstrip("/"),
            customer_id=get("CUSTOMER_ID", cls.customer_id),
            customer_name=get("CUSTOMER_NAME", cls.customer_name),
            operator=get("OPERATOR", cls.operator),
            store_name=get("STORE_NAME", cls.store_name),
            data_dir=Path(get("DATA_DIR", "data")),
            commerce_agents=Path(get("COMMERCE_AGENTS")).expanduser()
            if get("COMMERCE_AGENTS")
            else None,
        )

    @property
    def fixtures_dir(self) -> Path:
        """The reference retail fixtures: a configured checkout's when it has them, else the
        copy packaged here."""
        if self.commerce_agents is not None:
            candidate = self.commerce_agents / "examples" / "retail" / "data"
            if candidate.is_dir():
                return candidate
        return Path(str(files("commerce_medusa") / "data" / "retail"))

    def data_file(self, name: str) -> Path:
        """``data_dir/name`` when present, else the packaged default of that name."""
        local = self.data_dir / name
        if local.exists():
            return local
        return Path(str(files("commerce_medusa") / "data" / name))

    @property
    def webhook_secrets(self) -> list[str]:
        """Every signing secret a webhook may carry: the CLI listener's and each real
        endpoint's (a Stripe endpoint signs with its own)."""
        candidates = [self.stripe_webhook_secret, *self.stripe_webhook_secrets.split(",")]
        return [c.strip() for c in candidates if c and c.strip()]
