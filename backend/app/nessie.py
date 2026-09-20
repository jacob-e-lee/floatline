from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import settings


@dataclass
class NessieResult:
    """Normalized response envelope from the Nessie API.

    Nessie returns either a bare array (e.g. account lists), a single object,
    or `{"results": [...]}`. This wrapper keeps the parsed payload and the HTTP
    status code together regardless of shape, so callers never have to mutate
    the envelope to store metadata.
    """

    payload: Any
    status_code: int
    raw: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def as_list(self) -> list[dict[str, Any]]:
        if isinstance(self.payload, list):
            return self.payload
        if isinstance(self.payload, dict):
            if "results" in self.payload and isinstance(self.payload["results"], list):
                return self.payload["results"]
            return [self.payload]
        return [self.payload]

    @property
    def as_dict(self) -> dict[str, Any]:
        if isinstance(self.payload, dict):
            return self.payload
        return {"value": self.payload}


class NessieClient:
    """Thin wrapper around the Capital One Nessie (test) API.

    The API key is passed as the ``key`` query parameter on every request,
    against the base URL https://api.nessieisreal.com (the sandbox serves HTTPS).
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.base_url = settings.nessie_base_url.rstrip("/")
        self.api_key = settings.nessie_api_key
        self.customer_id = settings.nessie_customer_id
        self.checking_id = settings.nessie_checking_account_id
        self.savings_id = settings.nessie_savings_account_id
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=30.0)

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- low level ---------------------------------------------------------
    async def _request(self, method: str, path: str, **kwargs: Any) -> NessieResult:
        resp = await self._client.request(method, path, params={"key": self.api_key}, **kwargs)
        resp.raise_for_status()
        try:
            payload: Any = resp.json()
        except ValueError:
            payload = {"_raw": resp.text}
        return NessieResult(payload=payload, status_code=resp.status_code, raw=resp.text)

    # --- accounts / balances -----------------------------------------------
    async def get_accounts(self) -> list[dict[str, Any]]:
        """GET /customers/{customer_id}/accounts -> list of account objects."""
        result = await self._request("GET", f"/customers/{self.customer_id}/accounts")
        return result.as_list

    async def get_account(self, account_id: str) -> dict[str, Any]:
        """Resolve a single account by its account_number.

        This Nessie sandbox 403s on both the bare ``/accounts/{id}`` path and
        the customer-scoped ``/customers/{cust}/accounts/{id}`` path, so the
        canonical source of truth is the account list, which returns full
        account objects (including ``balance``) keyed by ``account_number``.
        """
        for acct in await self.get_accounts():
            if str(acct.get("account_number")) == str(account_id):
                return acct
        return {}

    async def get_balance(self, account_id: str) -> float:
        data = await self.get_account(account_id)
        for key in ("balance", "account_balance"):
            if key in data:
                return float(data[key])
        return 0.0

    async def checking_balance(self) -> float:
        return await self.get_balance(self.checking_id)

    async def savings_balance(self) -> float:
        return await self.get_balance(self.savings_id)

    # --- money movement -----------------------------------------------------
    def _tx_body(self, amount: float, description: str, medium: str = "Balance") -> dict[str, Any]:
        # Nessie validates the exact envelope:
        #   - deposits/transfers require transaction_date + status; medium="Balance"
        #   - withdrawals use medium="balance" (lowercase enum)
        # The API generates its own transaction_id; we must NOT send one.
        from datetime import datetime, timezone
        return {
            "amount": float(round(amount, 2)),
            "medium": medium,
            "description": description or "transaction",
            "transaction_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "status": "completed",
        }

    async def create_deposit(self, account_id: str, amount: float, description: str = "") -> NessieResult:
        return await self._request("POST", f"/accounts/{account_id}/deposits", json=self._tx_body(amount, description))

    async def create_withdrawal(self, account_id: str, amount: float, description: str = "") -> NessieResult:
        return await self._request("POST", f"/accounts/{account_id}/withdrawals", json=self._tx_body(amount, description, medium="balance"))

    async def create_transfer(self, source_account_id: str, payee_id: str, amount: float, description: str = "") -> dict[str, Any]:
        """Move money between two Nessie accounts.

        The Nessie sandbox transfer endpoint does NOT move balances between
        accounts (it creates a transfer record with zero delta). To sweep money
        between checking and savings reliably, we withdraw from the source and
        deposit into the destination — producing real balance changes that the
        PID controller can observe.
        """
        amount = float(round(amount, 2))
        descr = description or "transfer"
        withdrawal = await self.create_withdrawal(source_account_id, amount, f"{descr} (out)")
        deposit = await self.create_deposit(payee_id, amount, f"{descr} (in)")
        return {
            "status_code": deposit.status_code,
            "withdrawal": withdrawal.payload,
            "deposit": deposit.payload,
        }

    # --- simulate endpoints (demo conveniences) ----------------------------
    async def simulate_income(self, amount: float) -> NessieResult:
        return await self.create_deposit(self.checking_id, amount, "simulated income")

    async def simulate_expense(self, amount: float) -> NessieResult:
        return await self.create_withdrawal(self.checking_id, amount, "simulated expense")