"""Token check for the chat API: ask the store who the token belongs to, on every request."""
import httpx


class StoreUnavailable(Exception):
    pass


class StoreAuth:
    def __init__(self, http: httpx.AsyncClient):
        self.http = http

    async def whoami(self, auth: str) -> dict | None:
        """{user, merchant} for a valid token that has a merchant. None otherwise (fail closed)."""
        try:
            resp = await self.http.get("/auth/me", headers={"Authorization": auth})
        except httpx.TransportError as exc:
            raise StoreUnavailable(str(exc)) from exc
        if resp.status_code != 200:
            return None
        me = resp.json()
        return me if me.get("merchant") else None
