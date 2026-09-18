"""A thin async client for the Medusa v2 Store API. Every request carries the
publishable key; a customer's JWT goes on the request when the caller has one. The
adapter, not this client, decides what a missing record means."""

from __future__ import annotations

from typing import Any

import httpx


class MedusaError(RuntimeError):
    def __init__(self, status: int, message: str, path: str) -> None:
        super().__init__(f"{status} on {path}: {message}")
        self.status = status
        self.message = message
        self.path = path


class MedusaClient:
    def __init__(
        self,
        base_url: str,
        publishable_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"x-publishable-api-key": publishable_key},
            transport=transport,
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _auth(token: str | None) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        token: str | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        response = await self._http.request(
            method, path, params=params, json=json, headers=self._auth(token)
        )
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code >= 400:
            try:
                message = response.json().get("message", response.text)
            except ValueError:
                message = response.text
            raise MedusaError(response.status_code, str(message)[:300], path)
        return response.json() if response.content else {}

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        token: str | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        return await self._request("GET", path, params=params, token=token, allow_404=allow_404)

    async def post(
        self, path: str, json: dict[str, Any] | None = None, *, token: str | None = None
    ) -> dict[str, Any]:
        return await self._request("POST", path, json=json, token=token) or {}

    async def delete(
        self, path: str, body: dict[str, Any] | None = None, *, token: str | None = None
    ) -> dict[str, Any]:
        return await self._request("DELETE", path, json=body, token=token) or {}

    # -- auth and setup helpers ------------------------------------------------------

    async def login_customer(self, email: str, password: str) -> str:
        data = await self.post("/auth/customer/emailpass", {"email": email, "password": password})
        return str(data["token"])

    async def register_customer(
        self, email: str, password: str, first_name: str, last_name: str
    ) -> str:
        data = await self.post(
            "/auth/customer/emailpass/register", {"email": email, "password": password}
        )
        token = str(data["token"])
        await self.post(
            "/store/customers",
            {"email": email, "first_name": first_name, "last_name": last_name},
            token=token,
        )
        return await self.login_customer(email, password)

    async def first_region_id(self, currency: str | None = None) -> str:
        """The region to price and sell in: the first with ``currency`` when given (and
        present), else the store's first region."""
        data = await self.get("/store/regions") or {}
        regions = data.get("regions") or []
        if not regions:
            raise MedusaError(500, "the store has no region", "/store/regions")
        if currency:
            for region in regions:
                if str(region.get("currency_code", "")).lower() == currency.lower():
                    return str(region["id"])
        return str(regions[0]["id"])
