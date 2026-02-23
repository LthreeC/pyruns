import pytest
from pyruns.ui.state import AppState


def test_app_state_defaults():
    """Verify default values are set correctly without settings."""
    state = AppState()
    assert state.manager_columns == 5
    assert state.max_workers == 1
    assert state.execution_mode == "thread"
    assert state.active_tab == "generator"


def test_app_state_with_settings():
    """Verify settings dictionary overrides defaults properly."""
    settings = {
        "manager_columns": 3,
        "manager_max_workers": 4,
        "manager_execution_mode": "process"
    }
    state = AppState(_settings=settings)
    assert state.manager_columns == 3
    assert state.max_workers == 4
    assert state.execution_mode == "process"


def test_app_state_partial_settings():
    """Verify partial settings and type conversion."""
    settings = {
        "manager_columns": "4",  # Should be cast to int
    }
    state = AppState(_settings=settings)
    assert state.manager_columns == 4
    assert state.max_workers == 1  # Should retain default
    assert state.execution_mode == "thread"
