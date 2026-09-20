from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> parents[0]=app, [1]=backend, [2]=repo root
REPO_ROOT: Path = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Application configuration (env + .env, case-insensitive)."""

    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_required=False,
        extra="ignore",
        case_sensitive=False,
    )

    # Capital One Nessie (test) API. NOTE: the sandbox refuses HTTP :80 and the
    # Nessie sandbox only serves HTTPS; the default is therefore https. Override
    # with NESSIE_BASE_URL=http://api.nessieisreal.com if your network allows HTTP.
    nessie_base_url: str = "https://api.nessieisreal.com"
    nessie_api_key: str
    nessie_customer_id: str
    nessie_checking_account_id: str
    nessie_savings_account_id: str

    # Alpaca (paper)
    apca_api_base_url: str = "https://paper-api.alpaca.markets/v2"
    apca_api_key_id: str
    apca_api_secret_key: str
    alpaca_order_symbol: str = "SPY"

    # Database
    database_url: str

    # PID controller
    pid_setpoint: float = 1500.0
    pid_kp: float = 0.5
    pid_ki: float = 0.1
    pid_kd: float = 0.05
    pid_deadband: float = 50.0

    # APScheduler control-loop cadence
    poll_interval: int = 60
    control_loop_enabled: bool = True

    # Balance source for the controller:
    #   "ledger" -> Floatline's TimescaleDB shadow ledger (default)
    #   "live"   -> read straight from Nessie/Alpaca every cycle
    # See tasks._current_balances for why "ledger" is the default against the
    # Capital One Nessie sandbox.
    balance_source: str = "ledger"

    # Liquidity cascade
    savings_cap: float = 5000.0


settings = Settings()