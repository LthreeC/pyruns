from pathlib import Path


FRONTEND_GENERATOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "generator" / "GeneratorPage.tsx"
FRONTEND_POLLING = Path(__file__).resolve().parents[1] / "frontend" / "src" / "hooks" / "usePolling.ts"
FRONTEND_STORE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "store.ts"
FRONTEND_DASHBOARD = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "dashboard" / "DashboardPage.tsx"
FRONTEND_MONITOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "monitor" / "MonitorPage.tsx"
FRONTEND_LAUNCHER = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "launcher" / "LauncherPage.tsx"
FRONTEND_SIDEBAR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "layout" / "Sidebar.tsx"
FRONTEND_API = Path(__file__).resolve().parents[1] / "frontend" / "src" / "api.ts"
FRONTEND_CONFIRM_DIALOG = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "shared" / "ConfirmDialog.tsx"


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


def test_react_launcher_avoids_expensive_modal_blur():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "backdrop-blur" not in launcher


def test_react_modals_avoid_expensive_backdrop_blur():
    dashboard = FRONTEND_DASHBOARD.read_text(encoding="utf-8")
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "backdrop-blur" not in dashboard
    assert "backdrop-blur" not in launcher


def test_react_launcher_manual_path_buttons_are_clear_and_disabled_when_empty():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "launchMode" in launcher
    assert "const scriptPathReady = manualScriptPath.trim().length > 0" in launcher
    assert "const shellPathReady = manualShellRootPath.trim().length > 0" in launcher
    assert "pathReady={scriptPathReady}" in launcher
    assert "pathReady={shellPathReady}" in launcher
    assert "disabled={!pathReady}" in launcher
    assert "Select Script Path" in launcher
    assert "Open Folder Path" in launcher
    assert "Use Path" not in launcher


def test_react_launcher_keeps_path_controls_available_while_script_scan_runs():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "Scanning current directory..." in launcher
    assert "ModeActionPanel" in launcher
    assert "loading && (" not in launcher


def test_react_launcher_fetch_does_not_clobber_newer_selection():
    store = FRONTEND_STORE.read_text(encoding="utf-8")

    assert "let launcherRequestSeq = 0" in store
    assert "const requestId = ++launcherRequestSeq" in store
    assert "if (requestId !== launcherRequestSeq)" in store


def test_react_batch_preview_uses_readable_summary_and_structured_rows():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")
    dialog = FRONTEND_CONFIRM_DIALOG.read_text(encoding="utf-8")

    assert "function BatchPreviewContent" in generator
    assert "function BatchPreviewList" in generator
    assert "Tasks to create" in generator
    assert "Task samples" in generator
    assert "Previewed" not in generator
    assert "formatFullTaskTooltip" in generator
    assert "title={formatFullTaskTooltip(item)}" in generator
    assert "grid-cols-[72px_minmax(0,1fr)]" in generator
    assert "size=\"lg\"" in generator
    assert "#{item.index}: {item.preview}" not in generator
    assert "size?: 'md' | 'lg'" in dialog


def test_react_shell_mode_has_runtime_status_panel():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "function ShellRuntimePanel" in generator
    assert "Shell Runtime" in generator
    assert "Resolved file" in generator
    assert "Workspace folder" in generator
    assert "getShellConfigFilename" in generator


def test_react_launcher_does_not_show_fake_three_step_progress_for_quick_open():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "function LaunchChoiceTabs" in launcher
    assert "function ModeActionPanel" in launcher
    assert "launchMode === 'python'" in launcher
    assert "launchMode === 'shell'" in launcher
    assert "StepIndicator" not in launcher
    assert "PathLaunchPanel" not in launcher
    assert "PythonLaunchPanel" not in launcher
    assert "ShellLaunchPanel" not in launcher
    assert "Python Script" not in launcher
    assert "Shell Folder" not in launcher
    assert "Detected/manual scripts can choose configs" not in launcher
    assert "Folder opens directly in shell mode" not in launcher
    assert "Browse & Open Script" in launcher
    assert "Browse & Open Folder" in launcher
    assert "Select a script and configuration to get started" not in launcher


def test_react_sidebar_uses_direct_workspace_switching_without_launcher_button():
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "handlePickScript" in sidebar
    assert "Open Shell Mode" in sidebar
    assert "Exit Shell Mode" in sidebar
    assert "Open Launcher" not in sidebar
    assert "launcher=1" not in sidebar
