from pathlib import Path


FRONTEND_GENERATOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "generator" / "GeneratorPage.tsx"
FRONTEND_COMPONENTS_DIR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components"
FRONTEND_POLLING = Path(__file__).resolve().parents[1] / "frontend" / "src" / "hooks" / "usePolling.ts"
FRONTEND_STORE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "store.ts"
FRONTEND_DASHBOARD = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "dashboard" / "DashboardPage.tsx"
FRONTEND_MONITOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "monitor" / "MonitorPage.tsx"
FRONTEND_LAUNCHER = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "launcher" / "LauncherPage.tsx"
FRONTEND_APP_SHELL = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "layout" / "AppShell.tsx"
FRONTEND_SIDEBAR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "layout" / "Sidebar.tsx"
FRONTEND_TASK_DETAIL = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "manager" / "TaskDetailPanel.tsx"
FRONTEND_API = Path(__file__).resolve().parents[1] / "frontend" / "src" / "api.ts"
FRONTEND_TYPES = Path(__file__).resolve().parents[1] / "frontend" / "src" / "types.ts"
FRONTEND_CONFIRM_DIALOG = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "shared" / "ConfirmDialog.tsx"
FRONTEND_COMPACT_SECTION = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "shared" / "CompactSection.tsx"
FRONTEND_THEME_CSS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "theme" / "index.css"


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


def test_react_dashboard_uses_full_width_clear_workspace_layout():
    source = FRONTEND_DASHBOARD.read_text(encoding="utf-8")

    assert "max-w-[1600px]" not in source
    assert "mx-auto" not in source
    assert "flex w-full flex-col" in source
    assert "const workspaceKindLabel" in source
    assert "Shell Workspace" in source
    assert "border border-border-default bg-surface-raised" in source
    assert "Start New Task" in source
    assert "workspacePathSegments" in source


def test_react_dashboard_displays_working_root_and_labels_storage_root():
    source = FRONTEND_DASHBOARD.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "working_root?: string" in types
    assert "getWorkspaceWorkingPath" in source
    assert "getWorkspaceStoragePath" in source
    assert "const workspaceWorkingPath = getWorkspaceWorkingPath(workspace)" in source
    assert "const workspaceStoragePath = getWorkspaceStoragePath(workspace)" in source
    assert "splitPathSegments(workspaceWorkingPath)" in source
    assert "title={workspaceWorkingPath || ''}" in source
    assert "{workspaceWorkingPath || 'Open a workspace to start'}" in source
    assert 'InfoRow label="Working" value={workspaceWorkingPath || \'--\'} mono' in source
    assert 'InfoRow label="Storage" value={workspaceStoragePath || \'--\'} mono' in source
    assert "splitPathSegments(workspace?.run_root)" not in source


def test_react_sidebar_active_workspace_state_is_visually_clear():
    source = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "border-l-2 border-accent" in source
    assert "bg-accent/10 text-accent" in source
    assert "Shell mode active" in source


def test_react_gpu_process_dialog_shows_process_owner():
    dashboard = FRONTEND_DASHBOARD.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "user: string" in types
    assert "grid-cols-[88px_132px_minmax(0,1fr)_120px]" in dashboard
    assert "<span>User</span>" in dashboard
    assert "process.user || 'unknown'" in dashboard


def test_react_monitor_sidebar_width_is_clamped():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "Math.min(35, Math.max(10, sidebarWidthRaw))" in source


def test_react_monitor_uses_unfiltered_full_task_list():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "monitorTasks: Task[]" in store
    assert "fetchMonitorTasks: () => Promise<void>" in store
    assert "api.getTasks({ limit: 0, refresh: true })" in store
    assert "const { monitorTasks, fetchMonitorTasks } = useTaskStore()" in monitor
    assert "const selectedTask = monitorTasks.find" in monitor
    assert "usePolling(fetchMonitorTasks" in monitor


def test_react_components_avoid_large_forced_corner_radius():
    forbidden = ("rounded-xl", "rounded-2xl", "rounded-3xl")
    offenders = []

    for path in FRONTEND_COMPONENTS_DIR.rglob("*.tsx"):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                offenders.append(f"{path.relative_to(FRONTEND_COMPONENTS_DIR)}:{token}")

    assert offenders == []


def test_react_components_avoid_pill_borders_and_heavy_shadows():
    forbidden = ("rounded-full border", "shadow-lg", "shadow-2xl", "linear-gradient")
    offenders = []

    for path in FRONTEND_COMPONENTS_DIR.rglob("*.tsx"):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if token in source:
                offenders.append(f"{path.relative_to(FRONTEND_COMPONENTS_DIR)}:{token}")

    assert offenders == []


def test_react_workspace_surfaces_avoid_box_inside_box_chrome():
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")
    dashboard = FRONTEND_DASHBOARD.read_text(encoding="utf-8")
    compact = FRONTEND_COMPACT_SECTION.read_text(encoding="utf-8")
    dialog = FRONTEND_CONFIRM_DIALOG.read_text(encoding="utf-8")

    assert "rounded-xl border" not in sidebar
    assert "rounded-lg border border-border-subtle bg-surface-raised" not in sidebar
    assert "grid gap-2 rounded-lg border" not in launcher
    assert "rounded-lg border border-border-subtle bg-surface-overlay/50 p-3" not in launcher
    assert "'overflow-hidden rounded-md border'" not in compact
    assert "border-b border-border-subtle" not in compact
    assert "shadow-[0_24px_80px" not in dialog
    assert "rounded-lg border border-border-subtle bg-surface-raised" not in dashboard
    assert "rounded-lg border border-dashed border-border-subtle" not in dashboard
    assert "rounded-full border border-border-subtle bg-surface-overlay" not in dashboard


def test_react_inline_status_chips_do_not_add_extra_borders():
    inline_metric = (FRONTEND_COMPONENTS_DIR / "shared" / "InlineMetric.tsx").read_text(encoding="utf-8")
    status_badge = (FRONTEND_COMPONENTS_DIR / "shared" / "StatusBadge.tsx").read_text(encoding="utf-8")

    assert "rounded-full border" not in inline_metric
    assert "border-" not in inline_metric
    assert "rounded-full border" not in status_badge


def test_react_app_sidebar_can_be_resized_and_persisted():
    shell = FRONTEND_APP_SHELL.read_text(encoding="utf-8")
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "SIDEBAR_WIDTH_STORAGE_KEY" in shell
    assert "clampSidebarWidth" in shell
    assert "startSidebarResize" in shell
    assert "pointermove" in shell
    assert "localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY" in shell
    assert "aria-label=\"Resize navigation sidebar\"" in shell
    assert "cursor-col-resize" in shell
    assert "<Sidebar width={sidebarWidth}" in shell
    assert "width?: number" in sidebar
    assert "style={{ width }}" in sidebar
    assert "w-sidebar" not in sidebar


def test_react_task_detail_panel_can_be_resized_from_left_edge():
    source = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")

    assert "TASK_DETAIL_WIDTH_STORAGE_KEY" in source
    assert "clampPanelWidth" in source
    assert "startPanelResize" in source
    assert "pointermove" in source
    assert "localStorage.setItem(TASK_DETAIL_WIDTH_STORAGE_KEY" in source
    assert "aria-label=\"Resize task detail panel\"" in source
    assert "cursor-col-resize" in source
    assert "style={{ width: panelWidth }}" in source


def test_react_monitor_sidebar_can_be_resized_from_split_handle():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "MONITOR_SIDEBAR_WIDTH_STORAGE_KEY" in source
    assert "clampMonitorSidebarWidth" in source
    assert "startMonitorSidebarResize" in source
    assert "pointermove" in source
    assert "localStorage.setItem(MONITOR_SIDEBAR_WIDTH_STORAGE_KEY" in source
    assert "aria-label=\"Resize monitor sidebar\"" in source
    assert "cursor-col-resize" in source
    assert "style={{ width: `${monitorSidebarWidthPct}%` }}" in source


def test_react_code_editor_focuses_from_blank_editor_area():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")
    css = FRONTEND_THEME_CSS.read_text(encoding="utf-8")

    assert "function CodeEditorFrame" in generator
    assert "editorViewRef" in generator
    assert "focusEditorFromBlankArea" in generator
    assert "target.closest('.cm-content')" in generator
    assert "onCreateEditor={view => { editorViewRef.current = view }}" in generator
    assert "view.dispatch({ selection: { anchor: view.state.doc.length }, scrollIntoView: true })" in generator
    assert "onMouseDown={focusEditorFromBlankArea}" in generator
    assert "cursor-text" in generator
    assert ".generator-code-editor .cm-content" in css
    assert "width: 100%;" in css


def test_react_code_editor_has_no_horizontal_scrollbar():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")
    css = FRONTEND_THEME_CSS.read_text(encoding="utf-8")

    assert "EditorView.lineWrapping" in generator
    assert "overflow-x: hidden;" in css
    assert "white-space: pre-wrap;" in css


def test_react_launcher_supports_manual_shell_folder_paths():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "manualShellRootPath" in launcher
    assert "handleManualShellRoot" in launcher
    assert "openLauncherShellRoot(shellPath)" in launcher
    assert "openLauncherShellRoot" in api
    assert "/api/launcher/open-shell-root" in api


def test_react_launcher_browse_script_enters_config_selection_before_opening():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "pickLauncherScriptPath" in api
    assert "/api/launcher/pick-script-path" in api
    assert "const selection = await api.pickLauncherScriptPath()" in launcher
    assert "setManualScriptPath(selection.script_path)" in launcher
    assert "await selectScript(selection.script_path)" in launcher
    assert "Browse Script" in launcher
    assert "Browse & Open Script" not in launcher


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


def test_react_launcher_reuses_workspace_default_without_extra_config_prompt():
    store = FRONTEND_STORE.read_text(encoding="utf-8")

    assert "const workspaceDefault = res.items.find(item => item.kind === 'workspace_default')" in store
    assert "selectedConfig: workspaceDefault?.path || ''" in store
    assert "step: workspaceDefault ? 2 : 1" in store


def test_react_launcher_tracks_load_scripts_that_require_yaml_template():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "LauncherConfigsResponse" in api
    assert "requires_config_template" in types
    assert "requiresConfigTemplate" in store
    assert "requiresConfigTemplate: Boolean(res.requires_config_template)" in store
    assert "requiresConfigTemplate: false" in store


def test_react_launcher_clears_stale_error_after_valid_script_or_config_selection():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "const handleSelectScript = useCallback(async (scriptPath: string)" in launcher
    assert "setError('')" in launcher
    assert "await selectScript(scriptPath)" in launcher
    assert "const handleSelectConfig = useCallback((configPath: string)" in launcher
    assert "selectConfig(configPath)" in launcher
    assert "onClick={() => void handleSelectScript(script.script_path)}" in launcher
    assert "onClick={() => handleSelectConfig(config.path)}" in launcher


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
    assert "grid-cols-[56px_minmax(0,1fr)]" in generator
    assert "size=\"lg\"" in generator
    assert "#{item.index}: {item.preview}" not in generator
    assert "overflow-hidden rounded-lg border border-border-subtle bg-surface-overlay/60" not in generator
    assert "grid-cols-[72px_minmax(0,1fr)] gap-2 rounded-md border border-border-subtle bg-surface-raised" not in generator
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
    assert "Browse Script" in launcher
    assert "Browse & Open Folder" in launcher
    assert "Select a script and configuration to get started" not in launcher


def test_react_sidebar_workspace_card_opens_launcher_with_mode():
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")
    app = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "const openWorkspaceLauncher = (mode: 'python' | 'shell')" in sidebar
    assert "nextParams.set('launcher', '1')" in sidebar
    assert "nextParams.set('mode', mode)" in sidebar
    assert "nextParams.delete('script')" in sidebar
    assert "onClick={() => openWorkspaceLauncher(shellWorkspaceActive ? 'shell' : 'python')}" in sidebar
    assert "api.pickLauncherShellRoot()" not in sidebar
    assert "pickLauncherScriptPath" not in sidebar
    assert "openLauncherForConfig" not in sidebar
    assert "searchParams.delete('mode')" in app
    assert "const modeParam = searchParams.get('mode')" in launcher
    assert "if (modeParam === 'shell' || modeParam === 'python')" in launcher
    assert "setLaunchMode(modeParam)" in launcher
    assert "Open Shell Mode" in sidebar
    assert "Exit Shell Mode" in sidebar


def test_react_sidebar_routes_load_scripts_to_yaml_selection_without_red_error():
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "openWorkspaceLauncher('python')" in sidebar
    assert "nextParams.set('launcher', '1')" in sidebar
    assert "nextParams.set('mode', mode)" in sidebar
    assert "pyruns.load()" not in sidebar
    assert "bg-rose-500/10" not in sidebar


def test_react_sidebar_shows_real_picker_errors_but_ignores_cancel():
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "pickerError" in sidebar
    assert "showPickerError" in sidebar
    assert "No script selected." in sidebar
    assert "No directory selected." in sidebar
    assert "title={pickerError}" in sidebar


def test_react_launcher_config_step_explains_required_yaml_selection():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "requiresConfigTemplate" in launcher
    assert "Choose a YAML config" in launcher
    assert "This script needs a YAML config before first launch." in launcher
    assert "pyruns will save it as config_default.yaml" in launcher
    assert "Choose or enter a YAML config path first." in launcher
    assert "Path to config.yaml" in launcher
