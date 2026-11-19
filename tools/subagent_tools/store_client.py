"""The only place in the orders agent that talks HTTP to the store (and the only httpx import).

`auth` is the full Authorization header value, forwarded exactly as received. It is passed
on every call and never kept on the client, so one user's token cannot reach another user's call.
The one place the store's legacy `200 + {"status": false}` reply is turned into a failure is `lookup`.
"""
import httpx


class UpstreamUnavailable(Exception):
    """The store timed out, refused the connection, or answered 5xx."""


class StoreClient:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    @classmethod
    def from_url(cls, base_url: str) -> "StoreClient":
        return cls(httpx.AsyncClient(base_url=base_url, timeout=5.0))

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, path: str, auth: str, retry: bool = False, **kwargs) -> httpx.Response:
        attempts = 2 if retry else 1  # only idempotent reads are retried
        for attempt in range(attempts):
            try:
                resp = await self.http.request(method, path, headers={"Authorization": auth}, **kwargs)
            except httpx.TransportError as exc:  # timeout, connection refused, ...
                if attempt + 1 == attempts:
                    raise UpstreamUnavailable(str(exc)) from exc
                continue
            if resp.status_code >= 500:
                raise UpstreamUnavailable(f"store answered {resp.status_code}")
            return resp
        raise UpstreamUnavailable("unreachable")

    async def me(self, auth: str) -> dict | None:
        """{user, merchant}, or None if the token is not valid."""
        resp = await self._request("GET", "/auth/me", auth, retry=True)
        return resp.json() if resp.status_code == 200 else None

    async def list_orders(self, auth: str, status: str | None = None) -> list[dict]:
        resp = await self._request("GET", "/orders", auth, retry=True, params={"status": status} if status else None)
        if resp.status_code != 200:
            raise UpstreamUnavailable(f"store answered {resp.status_code}")
        return resp.json()

    async def lookup(self, auth: str, order_id: int) -> dict | None:
        """The order, or None when it is missing. Never trusts the HTTP status or truthiness."""
        resp = await self._request("GET", "/orders/lookup", auth, retry=True, params={"id": order_id})
        if resp.status_code != 200:
            raise UpstreamUnavailable(f"store answered {resp.status_code}")
        body = resp.json()
        if not isinstance(body, dict) or body.get("status") is False or not isinstance(body.get("id"), int):
            return None
        return body

    async def refund(self, auth: str, order_id: int) -> tuple[int, dict]:
        resp = await self._request("POST", "/orders/refund", auth, json={"id": order_id})
        return resp.status_code, resp.json()

    async def cancel(self, auth: str, order_id: int) -> tuple[int, dict]:
        resp = await self._request("POST", "/orders/cancel", auth, json={"id": order_id})
        return resp.status_code, resp.json()
