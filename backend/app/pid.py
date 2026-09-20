"""Floatline PID controller.

Implements a standard Proportional-Integral-Derivative controller that
computes the transfer amount needed to drive the checking account balance
toward the configured setpoint ($1500 by default).

Convention (so error > 0 => sweep excess to savings):
    error = current_balance - setpoint
    - error > 0: checking is over-funded  -> positive output -> sweep TO savings
    - error < 0: checking is under-funded  -> negative output -> pull FROM savings

A deadband filter suppresses churn: if abs(output) < $50, return 0 so we do
not spam the banking API over sub-threshold wiggles.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PIDState:
    """Mutable PID state, persisted across polling cycles."""

    integral: float = 0.0
    prev_error: float | None = None
    last_output: float = 0.0


def calculate_pid(
    current_balance: float,
    setpoint: float,
    kp: float,
    ki: float,
    kd: float,
    state: PIDState,
    dt: float,
    deadband: float = 50.0,
    integral_clamp: float = 10000.0,
) -> float:
    """Compute the PID transfer output for one cycle.

    Args:
        current_balance: Current checking account balance.
        setpoint: Target checking balance.
        kp, ki, kd: PID gains.
        state: Mutable PIDState carried across calls.
        dt: Time delta since the last call, expressed in the SAME time unit the
            Ki/Kd gains are tuned in.  The control loop runs every
            ``settings.poll_interval`` seconds and passes ``poll_interval / 60``
            (i.e. minutes), which keeps the default gains well damped.  Passing
            raw seconds would multiply the effective integral gain by 60 and
            cause violent overshoot.
        deadband: If abs(output) < deadband, returns 0 (no transfer).
        integral_clamp: Bounds the integral term to prevent windup.

    Returns:
        Required transfer amount.  Positive => sweep to savings.
        Negative => pull from savings.  0 => within deadband, do nothing.
    """
    error = current_balance - setpoint

    # Integral with anti-windup clamping
    state.integral += error * dt
    state.integral = max(-integral_clamp, min(integral_clamp, state.integral))

    # Derivative (handle first call where prev_error is None)
    if state.prev_error is not None:
        derivative = (error - state.prev_error) / dt
    else:
        derivative = 0.0

    state.prev_error = error

    output = kp * error + ki * state.integral + kd * derivative
    state.last_output = output

    # Deadband filter — suppress sub-threshold transfers
    if abs(output) < deadband:
        return 0.0

    return output
