from unittest.mock import MagicMock, patch

from pyruns.ui.state import AppState
import pyruns.ui.components.header as header
from pyruns.ui.components.header import _shared_metrics_snapshot


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


def test_shared_metrics_snapshot_uses_cache():
    sampler = MagicMock()
    sampler.sample.return_value = {"cpu_percent": 12.0, "mem_percent": 34.0, "gpus": []}
    header._METRICS_CACHE["at"] = 0.0
    header._METRICS_CACHE["data"] = {"cpu_percent": 0.0, "mem_percent": 0.0, "gpus": []}
    with patch("pyruns.ui.components.header.time.monotonic", side_effect=[1.0, 1.5]):
        first = _shared_metrics_snapshot(sampler)
        second = _shared_metrics_snapshot(sampler)
    assert first == second
    assert sampler.sample.call_count == 1
