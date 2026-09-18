"""Admin-side access to Medusa for the merchant adapter: an admin JWT obtained once from
email and password and refreshed on a 401. The operator identity the agent stamps on
changes comes from the session, not from this credential."""

from __future__ import annotations

from typing import Any

from .medusa_client import MedusaClient, MedusaError


class MedusaAdmin:
    def __init__(self, client: MedusaClient, email: str = "", password: str = "", token: str = ""):
        self.client = client
        self._email = email
        self._password = password
        self._token = token

    async def login(self) -> None:
        if not self._email:
            raise MedusaError(401, "no admin credentials", "/auth/user/emailpass")
        data = await self.client.post(
            "/auth/user/emailpass", {"email": self._email, "password": self._password}
        )
        self._token = str(data["token"])

    async def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        if not self._token:
            await self.login()
        try:
            return await getattr(self.client, method)(path, token=self._token, **kwargs)
        except MedusaError as error:
            if error.status != 401 or not self._email:
                raise
            await self.login()
            return await getattr(self.client, method)(path, token=self._token, **kwargs)

    async def get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self._call("get", path, params=params or None) or {}

    async def get_or_none(self, path: str, **params: Any) -> dict[str, Any] | None:
        return await self._call("get", path, params=params or None, allow_404=True)

    async def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._call("post", path, json=body or {})

    async def delete(self, path: str) -> dict[str, Any]:
        return await self._call("delete", path)

    async def list_all(self, path: str, key: str, page: int = 100, **params: Any) -> list[dict]:
        """Every row of a paginated admin list."""
        rows: list[dict] = []
        offset = 0
        while True:
            data = await self.get(path, limit=page, offset=offset, **params)
            batch = data.get(key) or []
            rows.extend(batch)
            offset += len(batch)
            if not batch or offset >= int(data.get("count") or 0):
                return rows
