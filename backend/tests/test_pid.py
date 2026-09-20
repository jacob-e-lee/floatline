"""Unit tests for the Floatline PID controller (pure math, no network)."""
from __future__ import annotations

import pytest

from app.pid import PIDState, calculate_pid


def test_positive_error_sweeps_to_savings():
    """Over-funded checking -> positive output (sweep TO savings)."""
    state = PIDState()
    output = calculate_pid(2000.0, 1500.0, 1.0, 0.0, 0.0, state, dt=60.0, deadband=0.0)
    assert output == pytest.approx(500.0)


def test_negative_error_pulls_from_savings():
    """Under-funded checking -> negative output (pull FROM savings)."""
    state = PIDState()
    output = calculate_pid(1000.0, 1500.0, 1.0, 0.0, 0.0, state, dt=60.0, deadband=0.0)
    assert output == pytest.approx(-500.0)


def test_at_setpoint_returns_zero():
    state = PIDState()
    assert calculate_pid(1500.0, 1500.0, 0.5, 0.1, 0.05, state, dt=60.0) == 0.0


def test_deadband_suppresses_sub_threshold_transfer():
    """error=40 * kp=0.5 -> 20, below the $50 deadband -> no transfer."""
    state = PIDState()
    assert calculate_pid(1540.0, 1500.0, 0.5, 0.0, 0.0, state, dt=60.0, deadband=50.0) == 0.0


def test_output_at_deadband_boundary_executes():
    """Exactly $50 of output is not below the deadband, so it executes."""
    state = PIDState()
    output = calculate_pid(1600.0, 1500.0, 0.5, 0.0, 0.0, state, dt=60.0, deadband=50.0)
    assert output == pytest.approx(50.0)


def test_integral_windup_is_clamped():
    """A sustained huge error must not let the integral run away."""
    state = PIDState()
    for _ in range(1000):
        calculate_pid(100000.0, 1500.0, 0.0, 1.0, 0.0, state, dt=60.0, deadband=0.0, integral_clamp=10000.0)
    assert state.integral == pytest.approx(10000.0)


def test_derivative_reacts_to_error_velocity():
    """kd * (delta_error / dt): error jumps 0 -> 120 over dt=60 -> 2.0."""
    state = PIDState()
    calculate_pid(1500.0, 1500.0, 0.0, 0.0, 1.0, state, dt=60.0, deadband=0.0)
    output = calculate_pid(1620.0, 1500.0, 0.0, 0.0, 1.0, state, dt=60.0, deadband=0.0)
    assert output == pytest.approx(2.0)


def test_state_carries_across_cycles():
    state = PIDState()
    calculate_pid(1800.0, 1500.0, 0.5, 0.1, 0.05, state, dt=1.0, deadband=50.0)
    assert state.prev_error == pytest.approx(300.0)
    assert state.integral == pytest.approx(300.0)
    assert state.last_output != 0.0


def test_configured_gains_converge_into_deadband():
    """With the shipped tuning and dt in MINUTES, the loop settles near $1500."""
    state = PIDState()
    balance = 1800.0
    for _ in range(5):
        output = calculate_pid(balance, 1500.0, 0.5, 0.1, 0.05, state, dt=1.0, deadband=50.0)
        balance -= output
    assert abs(balance - 1500.0) < 50.0