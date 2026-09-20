from __future__ import annotations

from sqlalchemy import BigInteger, Column, DateTime, Float, Text, text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class BalanceSnapshot(Base):
    """Time-series telemetry row (TimescaleDB hypertable).

    NOTE: ``timestamp`` is part of the composite primary key because TimescaleDB
    requires the partitioning column to be present in every unique index -
    including the primary key - for ``create_hypertable()`` to succeed.
    """

    __tablename__ = "balance_snapshots"

    id = Column(BigInteger, autoincrement=True, primary_key=True)
    timestamp = Column(DateTime(timezone=True), primary_key=True, nullable=False, index=True)
    checking_balance = Column(Float, nullable=False)
    savings_balance = Column(Float, nullable=False)
    brokerage_balance = Column(Float, nullable=False)
    setpoint = Column(Float, nullable=False)
    pid_output = Column(Float, nullable=False, server_default=text("0"))


class TransferLog(Base):
    """Audit log of every sweep / simulation / investment transfer."""

    __tablename__ = "transfer_logs"

    id = Column(BigInteger, autoincrement=True, primary_key=True)
    timestamp = Column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
        server_default=text("now()"),
        index=True,
    )
    source_account_id = Column(Text, nullable=False)
    destination = Column(Text, nullable=False)
    amount = Column(Float, nullable=False)
    reason = Column(Text, nullable=False)
    status = Column(Text, nullable=False)
    api_response = Column(Text)
    request_id = Column(Text)


class AppSetting(Base):
    """Runtime-editable settings (PID setpoint, savings cap).

    Values here override the .env defaults from ``app.config.Settings``, so the
    operator can retune the controller from the dashboard without a redeploy.
    Plain key/value table - deliberately NOT a hypertable (low write volume).
    """

    __tablename__ = "app_settings"

    key = Column(Text, primary_key=True)
    value = Column(Text, nullable=False)