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

    async def get_positions(self) -> list[dict[str, Any]]:
        resp = await self._client.get("/positions")
        resp.raise_for_status()
        return resp.json()

    async def invested_market_value(self) -> float:
        """Sum of the market values of all open positions (GET /positions).

        Why NOT GET /account (portfolio_value / equity): the Alpaca paper
        account is permanently seeded with $100,000 of fake cash that cannot
        be deposited into via the API. A sweep merely converts that fake cash
        into shares, so account equity never reflects anything Floatline did.
        Tracking only the market value of the shares we actually own starts
        at $0.00 (no positions yet) and grows with every sweep - which is the
        number the dashboard should plot as the brokerage balance.
        """
        total = 0.0
        for position in await self.get_positions():
            try:
                total += float(position.get("market_value", 0.0))
            except (TypeError, ValueError):
                continue
        return round(total, 2)

    async def get_order(self, order_id: str) -> dict[str, Any]:
        """GET /orders/{id} - poll a single order's fill status."""
        resp = await self._client.get(f"/orders/{order_id}")
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {"_raw": data}

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