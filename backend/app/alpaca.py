from __future__ import annotations

from typing import Any

import httpx

from app.config import settings


class AlpacaClient:
    """Thin wrapper around the Alpaca Trading API v2 (paper)."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = settings.apca_api_base_url.rstrip("/")
        self.api_key = settings.apca_api_key_id
        self.secret_key = settings.apca_api_secret_key
        self.order_symbol = settings.alpaca_order_symbol
        headers = {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._client = client or httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_account(self) -> dict[str, Any]:
        """GET /account -> account info (cash, portfolio_value, ...)."""
        resp = await self._client.get("/account")
        resp.raise_for_status()
        return resp.json()

    def brokerage_balance(self, account: dict[str, Any]) -> float:
        for key in ("portfolio_value", "cash"):
            if key in account:
                try:
                    return float(account[key])
                except (TypeError, ValueError):
                    continue
        return 0.0

    async def get_positions(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/positions")
        resp.raise_for_status()
        return resp.json()

    async def submit_notional_market_buy(self, notional: float, symbol: str | None = None) -> dict[str, Any]:
        """POST /v2/orders - dollar-amount market buy into the configured symbol."""
        body = {
            "symbol": symbol or self.order_symbol,
            "notional": float(round(notional, 2)),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
        }
        resp = await self._client.post("/orders", json=body)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {"_raw": resp.text, "_status": resp.status_code}