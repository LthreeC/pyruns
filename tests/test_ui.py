from pathlib import Path


FRONTEND_GENERATOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "generator" / "GeneratorPage.tsx"
FRONTEND_POLLING = Path(__file__).resolve().parents[1] / "frontend" / "src" / "hooks" / "usePolling.ts"
FRONTEND_STORE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "store.ts"
FRONTEND_DASHBOARD = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "dashboard" / "DashboardPage.tsx"
FRONTEND_MONITOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "monitor" / "MonitorPage.tsx"
FRONTEND_LAUNCHER = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "launcher" / "LauncherPage.tsx"
FRONTEND_API = Path(__file__).resolve().parents[1] / "frontend" / "src" / "api.ts"


def test_react_generator_pin_promotes_params_without_duplicates():
    source = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "function PinnedParameters" in source
    assert "Pinned Parameters" in source
    assert "collectPinnedRows(data, pinnedParams" in source
    assert "const pinnedRowKeys = useMemo(() => new Set(pinnedRows.map(row => row.fullKey))" in source
    assert ".filter(key => !key.startsWith('_meta') && !pinnedRowKeys.has(key))" in source
    assert "if (pinnedRowKeys.has(fullKey))" in source


def test_react_polling_hook_prevents_overlapping_async_ticks():
    source = FRONTEND_POLLING.read_text(encoding="utf-8")

    assert "callback: () => void | Promise<void>" in source
    assert "inFlightRef" in source
    assert "if (inFlightRef.current) {" in source
    assert "Promise.resolve(result)" in source


def test_react_ui_column_preferences_are_clamped():
    source = FRONTEND_STORE.read_text(encoding="utf-8")

    assert "function clampInteger" in source
    assert "readStoredNumber(MANAGER_COLS_STORAGE_KEY, 5, 1, 8)" in source
    assert "readStoredNumber(GENERATOR_COLS_STORAGE_KEY, 5, 2, 8)" in source
    assert "const next = clampInteger(n, 5, 1, 8)" in source
    assert "const next = clampInteger(n, 5, 2, 8)" in source


def test_react_task_fetch_ignores_stale_responses():
    source = FRONTEND_STORE.read_text(encoding="utf-8")

    assert "let taskRequestSeq = 0" in source
    assert "const requestId = ++taskRequestSeq" in source
    assert "if (requestId !== taskRequestSeq) {" in source


def test_react_dashboard_polling_waits_for_network_work():
    source = FRONTEND_DASHBOARD.read_text(encoding="utf-8")

    assert "const refreshDashboard = useCallback(async () => {" in source
    assert "await Promise.all([" in source
    assert "api.getMetrics().then(setMetrics)" in source


def test_react_monitor_sidebar_width_is_clamped():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "Math.min(35, Math.max(10, sidebarWidthRaw))" in source


def test_react_launcher_supports_manual_shell_folder_paths():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "manualShellRootPath" in launcher
    assert "handleManualShellRoot" in launcher
    assert "openLauncherShellRoot(shellPath)" in launcher
    assert "openLauncherShellRoot" in api
    assert "/api/launcher/open-shell-root" in api
