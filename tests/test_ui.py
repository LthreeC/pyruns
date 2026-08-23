from pathlib import Path


FRONTEND_GENERATOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "generator" / "GeneratorPage.tsx"
FRONTEND_APP = Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.tsx"
FRONTEND_COMPONENTS_DIR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components"
FRONTEND_POLLING = Path(__file__).resolve().parents[1] / "frontend" / "src" / "hooks" / "usePolling.ts"
FRONTEND_LOG_STREAM = Path(__file__).resolve().parents[1] / "frontend" / "src" / "hooks" / "useWebSocket.ts"
FRONTEND_STORE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "store.ts"
FRONTEND_DASHBOARD = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "dashboard" / "DashboardPage.tsx"
FRONTEND_MONITOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "monitor" / "MonitorPage.tsx"
FRONTEND_MONITOR_SEARCH = Path(__file__).resolve().parents[1] / "frontend" / "src" / "utils" / "monitorSearch.ts"
FRONTEND_MANAGER = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "manager" / "ManagerPage.tsx"
FRONTEND_LAUNCHER = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "launcher" / "LauncherPage.tsx"
FRONTEND_APP_SHELL = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "layout" / "AppShell.tsx"
FRONTEND_SIDEBAR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "layout" / "Sidebar.tsx"
FRONTEND_UPDATE_CONTROL = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "layout" / "UpdateControl.tsx"
FRONTEND_TASK_DETAIL = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "manager" / "TaskDetailPanel.tsx"
FRONTEND_API = Path(__file__).resolve().parents[1] / "frontend" / "src" / "api.ts"
FRONTEND_TYPES = Path(__file__).resolve().parents[1] / "frontend" / "src" / "types.ts"
FRONTEND_CONFIRM_DIALOG = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "shared" / "ConfirmDialog.tsx"
FRONTEND_TOAST_HOST = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "shared" / "ToastHost.tsx"
FRONTEND_CODE_EDITOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "shared" / "CodeTextEditor.tsx"
FRONTEND_PAGINATION = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "shared" / "Pagination.tsx"
FRONTEND_SEARCH_INPUT = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "shared" / "SearchInput.tsx"
FRONTEND_THEME_CSS = Path(__file__).resolve().parents[1] / "frontend" / "src" / "theme" / "index.css"
FRONTEND_TAILWIND = Path(__file__).resolve().parents[1] / "frontend" / "tailwind.config.ts"
FRONTEND_INDEX = Path(__file__).resolve().parents[1] / "frontend" / "index.html"
STATIC_INDEX = Path(__file__).resolve().parents[1] / "pyruns" / "web" / "static" / "index.html"


def test_react_generator_pin_promotes_params_without_duplicates():
    source = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "function PinnedParameters" in source
    assert "Pinned Parameters" in source
    assert 'title="Pinned Parameters"' in source
    assert "count={rows.length}" in source
    assert 'className="mb-3 rounded-md border border-accent/20 bg-accent/5 p-2"' in source
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
    assert source.count("taskRequestSeq += 1") >= 3
    assert "const isCurrentRequest = () => {" in source
    assert "requestId === taskRequestSeq" in source
    assert "current.query === query" in source
    assert "current.statusFilter === statusFilter" in source
    assert "current.offset === requestedOffset" in source
    assert "current.limit === limit" in source
    assert "if (!isCurrentRequest()) {" in source


def test_react_task_detail_displays_source_state_in_run_history():
    source = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")
    types_source = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "source_states?: string[]" in types_source
    assert "durations?: (number | null)[]" in types_source
    assert "exit_codes?: (number | null)[]" in types_source
    assert "task.durations?.length ?? 0" in source
    assert "task.exit_codes?.length ?? 0" in source
    assert "duration: task.durations?.[index]" in source
    assert "exitCode: task.exit_codes?.[index]" in source
    assert ">Duration</span>" in source
    assert ">Exit Code</span>" in source
    assert "task.source_states?.length ?? 0" in source
    assert "source: task.source_states?.[index] || ''" in source
    assert ">Source</span>" in source


def test_react_dashboard_polling_waits_for_network_work():
    source = FRONTEND_DASHBOARD.read_text(encoding="utf-8")

    assert "const refreshDashboard = useCallback(() => {" in source
    assert "if (dashboardRefreshPromiseRef.current)" in source
    assert "const refreshPromise = Promise.allSettled([" in source
    assert "api.getMetrics(false)" in source
    assert "api.getMetrics(true)" in source
    assert "setMetricsError('')" in source
    assert "errorMessage(metricsResult.reason, 'System metrics unavailable.')" in source
    assert "Metrics refresh failed. Showing last values." in source
    assert "System metrics unavailable." in source


def test_react_app_lazy_loads_routes_and_runtime_panel():
    source = FRONTEND_APP.read_text(encoding="utf-8")
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "lazy," in source
    assert "Suspense," in source
    assert "function lazyWithReload" in source
    assert "const DashboardPage = lazyWithReload('dashboard'" in source
    assert "const GeneratorPage = lazyWithReload('generator'" in source
    assert "const ManagerPage = lazyWithReload('manager'" in source
    assert "const MonitorPage = lazyWithReload('monitor'" in source
    assert "const LauncherPage = lazyWithReload('launcher'" in source
    assert "window.sessionStorage.setItem(storageKey, '1')" in source
    assert "window.location.reload()" in source
    assert "class RouteErrorBoundary" in source
    assert '<RouteErrorBoundary key={location.pathname}>' in source
    assert "<Suspense fallback={<RouteLoadingFallback />}>" in source
    assert "function RouteLoadingFallback()" in source
    assert "const RuntimePanel = lazy(() => import('./RuntimePanel'))" in sidebar
    assert "import RuntimePanel from './RuntimePanel'" not in sidebar
    assert "<Suspense fallback={null}>" in sidebar
    assert "{runtimeOpen && (" in sidebar


def test_react_update_control_requires_confirmation_and_full_instance_restart():
    source = FRONTEND_UPDATE_CONTROL.read_text(encoding="utf-8")
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "<UpdateControl compact={compact} />" in sidebar
    assert "requestConfirmation({" in source
    assert "hasUnsavedWork" in source
    assert "api.checkPyrunsUpdate()" in source
    assert "api.updatePyruns()" in source
    assert "info.instance_id !== previousInstanceId" in source
    assert "window.location.reload()" in source
    assert "motion-reduce:animate-none" in source
    assert "if (!systemInfo.update_supported) return null" in source
    assert "'/api/system/info'" in api
    assert "'/api/system/update/check'" in api
    assert "'/api/system/update'" in api


def test_frontend_entrypoints_use_local_lightweight_assets():
    indexes = [
        FRONTEND_INDEX.read_text(encoding="utf-8"),
        STATIC_INDEX.read_text(encoding="utf-8"),
    ]

    for index in indexes:
        assert "fonts.googleapis.com" not in index
        assert "fonts.gstatic.com" not in index
        assert 'href="http' not in index
        assert "href='http" not in index
        assert 'src="http' not in index
        assert "src='http" not in index

    assert "vendor-codemirror" not in indexes[1]
    icon = FRONTEND_INDEX.parent / "public" / "pyruns.svg"
    assert '<link rel="icon" type="image/svg+xml" href="/pyruns.svg" />' in indexes[0]
    assert icon.exists()
    assert "<svg" in icon.read_text(encoding="utf-8")


def test_react_app_supports_direct_launcher_route():
    source = FRONTEND_APP.read_text(encoding="utf-8")

    assert "useLocation" in source
    assert "useNavigate" in source
    assert "location.pathname === '/launcher'" in source
    assert "setShowLauncher(searchParams.get('launcher') === '1' || location.pathname === '/launcher')" in source
    assert '<Route path="launcher" element={<DashboardPage />} />' in source
    assert "navigate('/', { replace: true })" in source


def test_react_launcher_keeps_separate_recent_paths():
    source = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "LAUNCH_HISTORY_LIMIT = 50" in source
    assert "'pyruns.launcher.history.python'" in source
    assert "'pyruns.launcher.history.shell'" in source
    assert "'pyruns.launcher.history.yaml'" in source
    assert "function readLaunchHistory" in source
    assert "function writeLaunchHistory" in source
    assert "const nextHistory = writeLaunchHistory(kind, path)" in source
    assert "function RecentPathList" in source
    assert "recentPaths={launchHistory.python}" in source
    assert "recentPaths={launchHistory.shell}" in source
    assert "recentPaths={launchHistory.yaml}" in source
    assert "onRecentPathOpen={openPythonPath}" in source
    assert "onRecentPathOpen={openShellPath}" in source
    assert "onRecentPathOpen={handleSelectConfig}" in source
    assert "kind=\"yaml\"" in source
    assert "Recent YAML" in source
    assert "max-h-60 space-y-1 overflow-y-auto" in source


def test_react_modal_surfaces_support_backdrop_and_escape_dismissal():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")
    runtime_panel = (FRONTEND_COMPONENTS_DIR / "layout" / "RuntimePanel.tsx").read_text(encoding="utf-8")
    confirm_dialog = FRONTEND_CONFIRM_DIALOG.read_text(encoding="utf-8")
    dashboard = FRONTEND_DASHBOARD.read_text(encoding="utf-8")

    assert "backdropPointerStartedRef.current = event.target === event.currentTarget" in launcher
    assert "window.addEventListener('keydown', handleKeyDown)" in launcher
    assert 'aria-modal="true"' in launcher
    assert "panelRef" in runtime_panel
    assert "closeGestureRef" in runtime_panel
    assert "const pointerListenerTimer = window.setTimeout" in runtime_panel
    assert "document.addEventListener('pointerdown', handleDocumentPointerDown)" in runtime_panel
    assert "document.addEventListener('pointermove', handleDocumentPointerMove)" in runtime_panel
    assert "document.addEventListener('pointerup', handleDocumentPointerUp)" in runtime_panel
    assert "document.addEventListener('pointercancel', handleDocumentPointerCancel)" in runtime_panel
    assert "gesture.startedInside || gesture.dragged" in runtime_panel
    assert "window.clearTimeout(pointerListenerTimer)" in runtime_panel
    assert "document.addEventListener('keydown', handleKeyDown)" in runtime_panel
    assert "onClick={event => event.stopPropagation()}" in runtime_panel
    assert 'aria-label="Runtime settings"' in runtime_panel
    assert "onCancel={event =>" in confirm_dialog
    assert "event.preventDefault()" in confirm_dialog
    assert "backdropPointerStartedRef.current = event.target === event.currentTarget" in confirm_dialog
    assert 'aria-modal="true"' in confirm_dialog
    assert "onConfirm: () => void | Promise<void>" in confirm_dialog
    assert "const [pending, setPending]" in confirm_dialog
    assert "dialog && !dialog.open" in confirm_dialog
    assert "if (dialog?.open)" in confirm_dialog
    assert "disabled={pending}" in confirm_dialog
    assert "aria-busy={pending || undefined}" in confirm_dialog
    assert ".catch(() => undefined)" in confirm_dialog
    assert "Loader2" in confirm_dialog
    assert "window.addEventListener('keydown', handleKeyDown)" in dashboard
    assert "backdropPointerStartedRef.current && event.target === event.currentTarget" in dashboard


def test_react_toasts_cover_command_feedback_without_blocking_ui():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    app = FRONTEND_APP.read_text(encoding="utf-8")
    toast_host = FRONTEND_TOAST_HOST.read_text(encoding="utf-8")
    dashboard = FRONTEND_DASHBOARD.read_text(encoding="utf-8")
    manager = FRONTEND_MANAGER.read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")
    runtime_panel = (FRONTEND_COMPONENTS_DIR / "layout" / "RuntimePanel.tsx").read_text(encoding="utf-8")
    task_detail = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")

    assert "export const useToastStore" in store
    assert "toasts: [" in store
    assert "].slice(0, 4)" in store
    assert "ToastHost" in app
    assert "pointer-events-none fixed bottom-3 right-3" in toast_host
    assert "flex-col-reverse" in toast_host
    assert "pointer-events-none flex w-[min(380px,calc(100vw-2rem))]" in toast_host
    assert "pointer-events-auto inline-flex h-11 w-11" in toast_host
    assert "focus-visible:ring-2 focus-visible:ring-accent/35" in toast_host
    assert "text-emerald-700 dark:text-emerald-300" in toast_host
    assert "text-rose-700 dark:text-rose-300" in toast_host
    assert "role={toast.tone === 'error' ? 'alert' : 'status'}" in toast_host
    assert "TOAST_TIMEOUT_MS" in toast_host
    assert "Tasks queued" in manager
    assert "Could not start tasks" in manager
    assert "Tasks moved to trash" in manager
    assert "Could not move task" in manager
    assert "Could not load task details" in manager
    assert "Could not load task logs" in manager
    assert "Could not load task logs" in dashboard
    assert "Log copied" in monitor
    assert "CSV exported" in monitor
    assert "Could not load log file" in monitor
    assert "Could not load task details" in monitor
    assert "Workspace env saved" in runtime_panel
    assert "Could not save runtime" in runtime_panel
    assert "Notes saved" in task_detail
    assert "Could not rename task" in task_detail


def test_react_dashboard_uses_full_width_clear_workspace_layout():
    source = FRONTEND_DASHBOARD.read_text(encoding="utf-8")

    assert "flex min-h-full w-full flex-col" in source
    assert "const workspaceKindLabel" in source
    assert "Shell Workspace" in source
    assert "border border-border-default bg-surface-raised" in source
    assert "Start New Task" in source
    assert "Recent Tasks" in source
    assert "GPU & System" in source
    assert "ResourceTile" in source
    assert "h-full overflow-y-auto bg-surface-base" in source
    assert "flex shrink-0 flex-col overflow-hidden rounded-md border border-border-default bg-surface-raised" in source
    assert '<div className="p-3">' in source
    assert "w-full rounded-md border border-border-subtle" in source
    assert "flex min-h-0 flex-1 flex-col overflow-hidden rounded-md border border-border-default bg-surface-raised" in source
    assert "min-h-0 flex-1 divide-y divide-border-subtle overflow-y-auto" in source
    assert "Quick status glance." in source
    assert "queuedCount" in source
    assert "pendingCount" in source


def test_react_dashboard_keeps_workspace_chrome_compact():
    source = FRONTEND_DASHBOARD.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "working_root?: string" in types
    assert "getWorkspaceWorkingPath" in source
    assert "const workspaceWorkingPath = getWorkspaceWorkingPath(workspace)" in source
    assert "title={workspaceWorkingPath || ''}" in source
    assert "{workspaceWorkingPath || 'Choose a workspace to start'}" in source


def test_react_workspace_chrome_distinguishes_uninitialized_roots():
    dashboard = FRONTEND_DASHBOARD.read_text(encoding="utf-8")
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "workspace?.workspace_ready === true" in dashboard
    assert "Workspace Needed" in dashboard
    assert "Choose a workspace to start" in dashboard
    assert "workspace?.workspace_ready === true" in sidebar
    assert "Choose workspace" in sidebar
    assert "Workspace needed" in sidebar


def test_react_sidebar_active_workspace_state_is_visually_clear():
    source = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "border-l-2 border-accent" in source
    assert "bg-accent/10 text-accent" in source
    assert "workspaceModeLabel" in source
    assert "Workspace" in source
    assert "runtimeLabel" in source
    assert "SlidersHorizontal" in source
    assert "rounded-md px-2 py-2" in source


def test_react_gpu_process_dialog_shows_process_owner():
    dashboard = FRONTEND_DASHBOARD.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "user: string" in types
    assert "grid-cols-[88px_132px_minmax(0,1fr)_120px_88px]" in dashboard
    assert "<span>User</span>" in dashboard
    assert "process.user || 'unknown'" in dashboard
    assert "<span className=\"text-right\">Share</span>" in dashboard
    assert "process.memory_mb == null || gpu.mem_total <= 0" in dashboard
    assert "formatPercent((process.memory_mb / gpu.mem_total) * 100)" in dashboard
    assert "sortedProcesses.map(process =>" in dashboard


def test_react_dashboard_gpu_cards_handle_multi_gpu_density():
    dashboard = FRONTEND_DASHBOARD.read_text(encoding="utf-8")

    assert "gpuCount > 1 && 'md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4'" in dashboard
    assert "wide={gpuCount === 1}" in dashboard
    assert "metrics.gpus.map(gpu =>" in dashboard
    assert "key={gpuKey(gpu)}" in dashboard
    assert "aria-label={`Inspect GPU ${gpu.index} ${gpu.name}`}" in dashboard
    assert "title={gpu.name}" in dashboard
    assert '<div className="p-3">' in dashboard
    assert "wide ? 'min-h-[7.5rem]' : 'h-[10.5rem]'" in dashboard


def test_react_gpu_process_dialog_is_viewport_bounded_and_scrollable():
    dashboard = FRONTEND_DASHBOARD.read_text(encoding="utf-8")

    assert "max-h-[calc(100dvh-2rem)]" in dashboard
    assert "flex-col overflow-hidden rounded-md" in dashboard
    assert "min-h-0 flex-1 overflow-y-auto px-5 py-4" in dashboard
    assert "overflow-x-auto rounded-md border border-border-subtle" in dashboard
    assert "min-w-[640px]" in dashboard
    assert 'role="dialog"' in dashboard
    assert 'aria-modal="true"' in dashboard
    assert 'aria-labelledby="gpu-detail-title"' in dashboard
    assert 'id="gpu-detail-title"' in dashboard
    assert "Close GPU details" in dashboard


def test_react_dashboard_supports_manual_refresh_and_richer_gpu_details():
    dashboard = FRONTEND_DASHBOARD.read_text(encoding="utf-8")

    assert "RefreshCw" in dashboard
    assert "manualRefreshing" in dashboard
    assert "const handleManualRefresh = useCallback(async () => {" in dashboard
    assert "Dashboard refreshed" in dashboard
    assert "Task summary and system metrics are up to date." in dashboard
    assert "Refresh dashboard now" in dashboard
    assert "Free VRAM" in dashboard
    assert "Proc VRAM" in dashboard
    assert "Avg/proc" in dashboard
    assert "formatPercent" in dashboard


def test_react_monitor_uses_readable_sidebar_on_narrow_viewports():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "function readCompactMonitorLayout()" in source
    assert "const COMPACT_MONITOR_SIDEBAR_HEIGHT = 'clamp(18rem, 45vh, 24rem)'" in source
    assert "window.matchMedia('(max-width: 700px)')" in source
    assert "compactMonitorLayout ? 'flex-col' : 'flex-row'" in source
    assert "compactMonitorLayout ? 'w-full max-w-full border-b border-border-subtle' : 'border-r border-border-subtle'" in source
    assert "? { height: COMPACT_MONITOR_SIDEBAR_HEIGHT }" in source
    assert ": { width: `max(${monitorSidebarWidthPct}%, ${MIN_MONITOR_SIDEBAR_WIDTH_PX}px)` }}" in source
    assert 'className="flex-none border-b border-border-subtle px-2.5 py-2"' in source
    assert 'className="min-h-0 flex-1 overflow-y-auto px-2 py-2"' in source
    assert 'className="flex-none border-t border-border-subtle px-2.5 py-2"' in source
    assert "{!compactMonitorLayout && (" in source


def test_react_monitor_pages_and_searches_task_list_without_limit_zero():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "monitorTasks: Task[]" in store
    assert "const MONITOR_TASK_PAGE_SIZE = 200" in store
    assert "loadMore?: boolean" in store
    assert "monitorHasMore: boolean" in store
    assert "upsertMonitorTask: (task: Task) => void" in store
    assert "limit: nextLimit" in store
    assert "compact: true" in store
    assert "limit: 0" not in store[store.index("async fetchMonitorTasks"):store.index("upsertMonitorTask(task)")]
    assert "refresh: !sidebarQuery.trim()" in monitor
    assert "fetchMonitorTasks({ query: sidebarQuery, loadMore: true, refresh: false, workspaceKey })" in monitor
    assert "monitorTasks.find(task => task.name === selectedTaskName)" in monitor
    assert "useTaskEvents({" in monitor
    assert "TASK_EVENT_FALLBACK_POLL_MS = 60_000" in monitor
    assert "Load 200 more" in monitor
    assert 'title="Pinned Tasks"' in monitor
    assert "count={pinnedTasks.length}" in monitor
    assert 'className="mb-3 rounded-md border border-accent/20 bg-accent/5 p-2"' in monitor
    assert 'title="Search Results"' in monitor
    assert "<SearchResultGroup" in monitor
    assert "task.search_match_count" in monitor
    assert "searchResultSummary" in monitor
    assert "TASK_SEARCH_FIELD_LABELS" in monitor
    assert 'ariaKeyShortcuts="Control+Shift+F Meta+Shift+F"' in monitor
    assert "!event.shiftKey && key === 'f'" in monitor
    assert "MIN_MONITOR_SIDEBAR_WIDTH_PX = 240" in monitor


def test_react_monitor_preserves_selection_and_clears_only_after_confirmed_deletion():
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "selectedTaskSnapshot" in monitor
    assert "selectedTaskFromList" in monitor
    assert "api.getTask(selectedTaskName, false)" in monitor
    assert 'title="Current Task"' in monitor
    assert "!sidebarSearchActive" in monitor
    assert "if (!selectedTaskName || selectedTaskFromList)" in monitor
    assert "/not found/i.test(errorMessage(error))" in monitor
    assert "selectedTaskName: null" in monitor


def test_react_monitor_merges_run_action_response_before_next_poll():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "upsertMonitorTask(task)" in monitor
    assert "task = (await api.runTask(currentTaskName)).task" in monitor
    assert "task = (await api.cancelTask(currentTaskName)).task" in monitor
    assert "monitorTasks: exists" in store
    assert "? state.monitorTasks.map(item => item.name === task.name ? task : item)" in store
    assert ": [task, ...state.monitorTasks]" in store


def test_react_components_avoid_excessive_rounding_and_shadows():
    forbidden = (
        "rounded-xl",
        "rounded-2xl",
        "rounded-3xl",
        "rounded-full border",
        "shadow-lg",
        "shadow-2xl",
        "linear-gradient",
    )
    offenders = []

    for path in FRONTEND_COMPONENTS_DIR.rglob("*.tsx"):
        source = path.read_text(encoding="utf-8")
        for token in forbidden:
            if path.name == "ToggleSwitch.tsx" and token == "rounded-full border":
                continue
            if token in source:
                offenders.append(f"{path.relative_to(FRONTEND_COMPONENTS_DIR)}:{token}")

    assert offenders == []


def test_react_app_sidebar_can_be_resized_and_persisted():
    shell = FRONTEND_APP_SHELL.read_text(encoding="utf-8")
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "SIDEBAR_WIDTH_STORAGE_KEY" in shell
    assert "clampSidebarWidth" in shell
    assert "startSidebarResize" in shell
    assert "pointermove" in shell
    assert "window.addEventListener('pointercancel', stopResize)" in shell
    assert "window.removeEventListener('pointercancel', stopResize)" in shell
    assert "pendingSidebarWidthRef" in shell
    assert "sidebarResizeFrameRef" in shell
    assert "window.requestAnimationFrame(applyPendingSidebarWidth)" in shell
    assert "window.cancelAnimationFrame(sidebarResizeFrameRef.current)" in shell
    assert "localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY" in shell
    assert "aria-label=\"Resize navigation sidebar\"" in shell
    assert "cursor-col-resize" in shell
    assert "<Sidebar width={effectiveSidebarWidth}" in shell
    assert "width?: number" in sidebar
    assert "style={{ width }}" in sidebar


def test_react_app_shell_uses_compact_sidebar_on_narrow_viewports():
    shell = FRONTEND_APP_SHELL.read_text(encoding="utf-8")
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "const COMPACT_SIDEBAR_WIDTH = 64" in shell
    assert "window.matchMedia('(max-width: 700px)')" in shell
    assert "const effectiveSidebarWidth = compactSidebar ? COMPACT_SIDEBAR_WIDTH : sidebarWidth" in shell
    assert "<Sidebar width={effectiveSidebarWidth} compact={compactSidebar}" in shell
    assert "compact?: boolean" in sidebar
    assert "aria-label={label}" in sidebar
    assert "title={label}" in sidebar
    assert "compact && 'justify-center px-0'" in sidebar
    assert "{!compact && <span>{label}</span>}" in sidebar


def test_react_app_shell_allows_pages_to_scroll_without_horizontal_growth():
    shell = FRONTEND_APP_SHELL.read_text(encoding="utf-8")

    assert "w-screen max-w-full" in shell
    assert 'className="min-w-0 flex-1 overflow-x-hidden overflow-y-auto focus:outline-none"' in shell


def test_react_mobile_pages_constrain_empty_states_and_header_actions():
    dashboard = FRONTEND_DASHBOARD.read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")
    empty_state = (FRONTEND_COMPONENTS_DIR / "shared" / "EmptyState.tsx").read_text(encoding="utf-8")

    assert "'flex h-full w-full max-w-full min-w-0 overflow-hidden'" in monitor
    assert "compactMonitorLayout ? 'w-full max-w-full border-b border-border-subtle' : 'border-r border-border-subtle'" in monitor
    assert 'className="flex min-h-0 min-w-0 max-w-full flex-1 flex-col"' in monitor
    assert 'className="flex h-full min-w-0 items-center justify-center px-4"' in monitor
    assert (
        "grid w-full min-w-0 grid-cols-[44px_minmax(0,1fr)] gap-2 "
        "sm:flex sm:w-auto sm:flex-wrap sm:items-center"
    ) in dashboard
    assert "touch-target inline-flex min-h-11 min-w-0" in dashboard
    assert "max-w-full flex-col items-center justify-center gap-3 px-4 py-20 text-center" in empty_state
    assert "max-w-full break-words" in empty_state


def test_react_manager_responsively_caps_card_columns_without_viewport_state():
    manager = FRONTEND_MANAGER.read_text(encoding="utf-8")

    assert "repeat(auto-fill, minmax(min(100%, max(15rem" in manager
    assert "const renderedColumnCount = grid" in manager
    assert "window.getComputedStyle(grid).gridTemplateColumns" in manager
    assert "columns={columns}" in manager
    assert "const TaskCard = memo(function TaskCard" in manager
    assert "[content-visibility:auto]" in manager


def test_react_manager_uses_global_counts_page_scoped_selection_and_pending_locks():
    manager = FRONTEND_MANAGER.read_text(encoding="utf-8")
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "status_counts?: TaskStatusCounts" in types
    assert "statusCounts: TaskStatusCounts | null" in store
    assert "statusCounts: page.status_counts ?? null" in store
    assert "selectedIds: new Set()" in store
    assert "Selection is page-scoped" in manager
    assert "tasks.filter(task => selectedIds.has(task.name))" in manager
    assert "pendingTaskActionsRef.current.has(taskName)" in manager
    assert "const [bulkAction, setBulkAction]" in manager
    assert "api.runTask(task.name)" in manager
    assert "api.runTask(task.name, bulkExecutionMode)" not in manager
    assert "Waiting for GPU capacity" in manager
    assert "const deleted = new Set(result.deleted || [])" in manager
    assert "Some tasks could not be deleted" in manager
    assert "Stop ${activeSelectedCount} active task" in manager
    assert "hasReorderChanges(allTasks.items, items)" in manager
    assert "is already in this position" in manager
    assert "Move ${task.name} earlier" in manager
    assert "Move ${task.name} later" in manager
    assert 'role="button"' in manager
    assert "tabIndex={0}" in manager
    assert "event.key === 'Enter' || event.key === ' '" in manager
    assert "aria-pressed={selectMode ? selected : undefined}" in manager
    assert "ManagerLoadingState" in manager
    assert 'role="alert"' in manager


def test_react_manager_batch_run_sends_only_worker_count():
    manager = FRONTEND_MANAGER.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "bulkExecutionMode" not in manager
    assert "const [maxWorkersInput, setMaxWorkersInput] = useState('2')" in manager
    assert "const normalizeWorkerInput = useCallback((value: string) => {" in manager
    assert "return Math.min(32, Math.max(1, parsed))" in manager
    assert "const maxWorkers = normalizeWorkerInput(maxWorkersInput)" in manager
    assert "setMaxWorkersInput(String(maxWorkers))" in manager
    assert "await api.batchRunTasks(names, maxWorkers)" in manager
    assert "Workers" in manager
    assert "value={maxWorkersInput}" in manager
    assert "body: JSON.stringify({ task_names: taskNames, max_workers: maxWorkers })" in api
    assert "execution_mode" not in api


def test_react_generator_stacks_editor_and_settings_on_narrow_viewports():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "function readCompactGeneratorLayout()" in generator
    assert "window.matchMedia('(max-width: 700px)')" in generator
    assert "const generatorBodyClassName = clsx(" in generator
    assert "compactGeneratorLayout ? 'flex-col overflow-y-auto' : 'overflow-hidden'" in generator
    assert "compactGeneratorLayout ? 'min-h-[20rem] flex-none' : 'flex-1'" in generator
    assert "compactGeneratorLayout ? 'w-full flex-none border-t border-border-subtle' : 'flex-none border-l border-border-subtle'" in generator
    assert "style={compactGeneratorLayout ? undefined : { width: generatorSettingsWidth }}" in generator


def test_react_generator_keeps_compact_toolbar_and_tree_content_usable():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "flex flex-col gap-2 border-b" in generator
    assert "min-[701px]:flex-row" in generator
    assert "w-full min-w-0 min-[701px]:w-auto min-[701px]:min-w-[280px]" in generator
    assert "const [outlineCollapsed, setOutlineCollapsed] = useState(readCompactGeneratorLayout)" in generator
    assert "if (query.matches)" in generator
    assert "setOutlineCollapsed(true)" in generator


def test_react_generator_batch_confirmation_uses_the_previewed_payload():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "interface PreviewSnapshot" in generator
    assert "const previewRequestSeqRef = useRef(0)" in generator
    assert "const snapshot: PreviewSnapshot" in generator
    assert "snapshot.inputKey !== generationInputKeyRef.current" in generator
    assert "setPreviewSnapshot(snapshot)" in generator
    assert "doCreate(previewSnapshot?.payload || currentGenerationPayload)" in generator
    assert "setPreviewSnapshot(current => current?.inputKey === generationInputKey ? current : null)" in generator


def test_react_generator_settings_panel_can_be_resized():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "GENERATOR_SETTINGS_WIDTH_STORAGE_KEY" in generator
    assert "clampGeneratorSettingsWidth" in generator
    assert "readStoredGeneratorSettingsWidth" in generator
    assert "startGeneratorSettingsResize" in generator
    assert "generatorBodyRef" in generator
    assert "pendingGeneratorSettingsWidthRef" in generator
    assert "generatorSettingsResizeFrameRef" in generator
    assert "window.requestAnimationFrame(applyPendingGeneratorSettingsWidth)" in generator
    assert "window.cancelAnimationFrame(generatorSettingsResizeFrameRef.current)" in generator
    assert "window.addEventListener('pointercancel', stopResize, { once: true })" in generator
    assert "window.removeEventListener('pointercancel', stopResize)" in generator
    assert "localStorage.setItem(GENERATOR_SETTINGS_WIDTH_STORAGE_KEY" in generator
    assert 'aria-label="Resize generator settings panel"' in generator
    assert "cursor-col-resize" in generator


def test_react_icon_only_buttons_have_accessible_names():
    task_detail = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")
    confirm_dialog = FRONTEND_CONFIRM_DIALOG.read_text(encoding="utf-8")
    manager = FRONTEND_MANAGER.read_text(encoding="utf-8")
    pagination = FRONTEND_PAGINATION.read_text(encoding="utf-8")

    assert 'aria-label="Save task name"' in task_detail
    assert 'aria-label="Cancel task rename"' in task_detail
    assert 'aria-label="Rename task"' in task_detail
    assert 'aria-label="Close task details"' in task_detail
    assert 'aria-label="Close dialog"' in confirm_dialog
    assert "aria-label={task.pinned ? `Unpin ${task.name}` : `Pin ${task.name}`}" in manager
    assert "label={`${actionBtn.label} ${task.name}`}" in manager
    assert "label={`View logs for ${task.name}`}" in manager
    assert "label={`Delete ${task.name}`}" in manager
    assert "aria-label={label}" in manager
    assert 'aria-label="Previous page"' in pagination
    assert 'aria-label="Next page"' in pagination


def test_react_task_detail_panel_can_be_resized_from_left_edge():
    source = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")

    assert "TASK_DETAIL_WIDTH_STORAGE_KEY" in source
    assert "clampPanelWidth" in source
    assert "Math.max(0, window.innerWidth - 8)" in source
    assert "min-w-0 max-w-[calc(100vw-8px)]" in source
    assert "startPanelResize" in source
    assert "pointermove" in source
    assert "window.addEventListener('pointercancel', stopResize, { once: true })" in source
    assert "window.removeEventListener('pointercancel', stopResize)" in source
    assert "pendingPanelWidthRef" in source
    assert "panelResizeFrameRef" in source
    assert "window.requestAnimationFrame(applyPendingPanelWidth)" in source
    assert "window.cancelAnimationFrame(panelResizeFrameRef.current)" in source
    assert "localStorage.setItem(TASK_DETAIL_WIDTH_STORAGE_KEY" in source
    assert "aria-label=\"Resize task detail panel\"" in source
    assert "cursor-col-resize" in source
    assert "style={{ width: panelWidth }}" in source
    assert "suppressNextCloseRef" in source
    assert "backdropPointerStartedRef" in source
    assert "function handlePanelBackdropClick" in source
    assert "backdropPointerStartedRef.current && event.target === event.currentTarget" in source
    assert "w-5 -translate-x-2.5" in source
    assert "group-hover:bg-accent/45" in source
    assert "window.innerWidth - 8" in source
    assert "max-w-[calc(100vw-8px)]" in source
    assert "const MAX_PANEL_WIDTH = 2400" in source
    assert "useState(() => buildEnvPairs(task))" in source


def test_react_task_detail_guards_async_updates_and_traps_modal_focus():
    source = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")

    assert "const taskRequestSeqRef = useRef(0)" in source
    assert "const currentTaskNameRef = useRef(task.name)" in source
    assert "requestId !== taskRequestSeqRef.current || currentTaskNameRef.current !== taskName" in source
    assert 'role="dialog"' in source
    assert 'aria-labelledby="task-detail-title"' in source
    assert "window.addEventListener('keydown', handleKeyDown)" in source
    assert "previousFocusRef" in source
    assert 'role="tablist"' in source
    assert 'role="tab"' in source
    assert 'role="tabpanel"' in source


def test_react_confirm_dialog_is_labelled_and_scrolls_on_short_viewports():
    source = FRONTEND_CONFIRM_DIALOG.read_text(encoding="utf-8")

    assert "const titleId = useId()" in source
    assert "aria-labelledby={titleId}" in source
    assert "aria-describedby={description ? descriptionId : undefined}" in source
    assert "max-h-[calc(100dvh-1.5rem)]" in source
    assert "w-[calc(100vw-1.5rem)]" in source
    assert "w-full max-w-[calc(100vw-1.5rem)]" not in source
    assert 'className="min-h-0 overflow-y-auto px-5 sm:px-6"' in source


def test_react_task_detail_env_editor_handles_edits_feedback_and_errors():
    source = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")

    assert "type EnvPair" in source
    assert "id: string" in source
    assert "key={pair.id}" in source
    assert 'key={`${key}-${index}`}' not in source
    assert "type EnvSaveStatus" in source
    assert "function buildEnvPairsFromEnv" in source
    assert "const envBaseRef = useRef(copyEnv(task.env || {}))" in source
    assert "if (envDirty || previousTaskNameRef.current !== task.name)" in source
    assert "envBaseRef.current = incomingEnv" in source
    assert "const expectedEnv = envBaseRef.current" in source
    assert "const response = await api.updateEnv(taskName, env, expectedEnv)" in source
    assert "const savedEnv = copyEnv(response.task?.env || env)" in source
    assert "setEnvPairs(buildEnvPairsFromEnv(savedEnv))" in source
    assert "getEnvValidationMessage(envPairs)" in source
    assert "ENV_NAME_PATTERN" in source
    assert "Invalid environment variable name" in source
    assert "const envSaveDisabled = saving || !envDirty || Boolean(envValidationMessage)" in source
    assert "? 'Replace Env'" in source
    assert "envSaveStatus === 'error'" in source
    assert "err instanceof api.ApiError && err.status === 409" in source
    assert "Your draft was kept. Saving it again will replace the newer environment." in source
    assert "aria-label=\"Add environment variable\"" in source
    assert "setPendingEnvFocusId(pair.id)" in source
    assert "aria-label={`Remove ${pair.key.trim() || 'environment variable'}`}" in source
    assert "function requestClose" in source
    assert "const [discardConfirmOpen, setDiscardConfirmOpen]" in source
    assert "const renameDirty" in source
    assert "const hasUnsavedChanges = notesDirty || envDirty || renameDirty" in source
    assert "setDiscardConfirmOpen(true)" in source
    assert 'title="Discard changes?"' in source
    assert "window.confirm('Discard unsaved changes?')" not in source
    assert "onClick={requestClose}" in source
    assert "} catch (err) {" in source
    assert "setEnvSaveError(errorMessage(err))" in source


def test_react_manager_keeps_open_task_detail_synced_after_list_refresh():
    manager = FRONTEND_MANAGER.read_text(encoding="utf-8")

    assert "setDetailTask(current => {" in manager
    assert "const refreshed = tasks.find(task => task.name === current.name)" in manager
    assert "...refreshed," in manager
    assert "config: current.config" in manager
    assert "config_text: current.config_text" in manager
    assert "}, [tasks])" in manager


def test_react_generator_shows_creation_progress_and_result_actions():
    source = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "type GenerationStatus" in source
    assert "generationStatus === 'creating'" in source
    assert "Writing task folders..." in source
    assert "function CreatedTaskSummary" in source
    assert "Open in Manager" in source
    assert "Loader2" in source


def test_react_manager_cards_support_drag_pin_and_search_match_labels():
    source = (FRONTEND_COMPONENTS_DIR / "manager" / "ManagerPage.tsx").read_text(encoding="utf-8")

    assert "type DragTarget = 'pinned' | 'tasks'" in source
    assert "const DRAG_START_DISTANCE" in source
    assert "dragCandidateRef" in source
    assert "suppressCardClickRef" not in source
    assert "function isInteractiveDragTarget" not in source
    assert 'data-task-drag-handle="true"' in source
    assert "onPointerDown={event => onPointerDown(task, event)}" in source
    article_open = source[source.index("<article"):source.index(">\n      {dropIndicator}")]
    assert "onPointerDown" not in article_open
    assert "window.addEventListener('pointermove', handleGlobalPointerMove)" in source
    assert "data-task-drop-target=\"pinned\"" in source
    assert "data-task-drop-target=\"tasks\"" in source
    assert "function getPointerDropIntent" in source
    assert "type DragPlacement = 'before' | 'after'" in source
    assert "api.reorderTasks" in source
    assert "const REORDER_TASK_LIMIT = 10_000" in source
    assert "limit: REORDER_TASK_LIMIT" in source
    assert "sort: sortMode" in source
    assert "setSortMode('manual')" in source
    assert "const visibleSectionTasks = tasks.filter" in source
    assert "already first among the visible tasks" in source
    assert "buildReorderedItems" in source
    assert "dragFrameRef" in source
    assert "pendingDragPointRef" in source
    assert "window.requestAnimationFrame(flushDragFrame)" in source
    assert "sameDropIntent" in source
    assert "data-task-grid-columns={columns}" in source
    assert "Number.parseInt(grid?.dataset.taskGridColumns || '1', 10)" in source
    assert "function DropIndicator" in source


def test_react_task_lists_use_summaries_and_fetch_full_details_on_open():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    manager = (FRONTEND_COMPONENTS_DIR / "manager" / "ManagerPage.tsx").read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "summary?: boolean" in api
    assert "sp.set('summary', String(params.summary))" in api
    assert "sp.set('compact', String(params.compact))" in api
    assert "api.getTasks({ query, status: statusFilter, sort: sortMode, offset, limit, summary: true })" in store
    assert "page.items.length === 0" in store
    assert "Math.floor((page.total - 1) / limit) * limit" in store
    assert "retryPage = await api.getTasks" in store
    monitor_fetch = store[store.index("async fetchMonitorTasks"):store.index("upsertMonitorTask(task)")]
    assert "limit: nextLimit" in monitor_fetch
    assert "summary: true" in monitor_fetch
    assert "compact: true" in monitor_fetch
    assert "limit: 0" not in monitor_fetch
    assert "api.getTask(task.name).then(fullTask" in manager
    assert "api.getTask(task.name).then(fullTask" in monitor
    assert "task.search_text || task.preview_text || ''" in manager
    assert "dropIndicator" in manager
    assert "shadow-[0_0_0_3px_rgba(20,184,166,0.16)]" in manager
    assert "scale-[0.985]" in manager
    assert "transition-[border-color,box-shadow,background-color,opacity,transform]" in manager
    assert "data-task-card={task.name}" in manager
    assert "data-task-card-pinned={task.pinned ? 'true' : 'false'}" in manager
    assert "getTaskSearchMatches(task, query)" in manager
    assert "Matched in" in manager
    assert "Drop here to pin" in manager
    assert 'title="Pinned Tasks"' in manager
    assert "count={pinnedTasks.length}" in manager
    assert 'className="rounded-md border border-accent/20 bg-accent/5 p-2"' in manager
    assert "const taskKindLabel = task.task_kind === 'shell' ? 'shell' : 'python'" in manager


def test_react_task_detail_uses_python_shell_task_mode_labels():
    source = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")

    assert "function isShellTask(task: Task)" in source
    assert "return task.task_kind === 'shell'" in source
    assert "return isShellTask(task) ? 'shell' : 'python'" in source


def test_react_theme_uses_more_readable_base_type_and_muted_text():
    css = FRONTEND_THEME_CSS.read_text(encoding="utf-8")
    tailwind = FRONTEND_TAILWIND.read_text(encoding="utf-8")

    assert "--text-secondary: #4b5563;" in css
    assert "--text-tertiary: #6b7280;" in css
    assert "--text-tertiary: #929aa8;" in css
    assert "font-size: 14px;" in css
    assert "'2xs': ['12px', '16px']" in tailwind
    assert "xs: ['13px', '18px']" in tailwind


def test_react_monitor_sidebar_can_be_resized_from_split_handle():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "Math.min(35, Math.max(10, sidebarWidthRaw))" in source
    assert "MONITOR_SIDEBAR_WIDTH_STORAGE_KEY" in source
    assert "clampMonitorSidebarWidth" in source
    assert "startMonitorSidebarResize" in source
    assert "pointermove" in source
    assert "window.addEventListener('pointercancel', stopResize, { once: true })" in source
    assert "window.removeEventListener('pointercancel', stopResize)" in source
    assert "pendingMonitorSidebarWidthRef" in source
    assert "monitorResizeFrameRef" in source
    assert "window.requestAnimationFrame(applyPendingMonitorSidebarWidth)" in source
    assert "window.cancelAnimationFrame(monitorResizeFrameRef.current)" in source
    assert "localStorage.setItem(MONITOR_SIDEBAR_WIDTH_STORAGE_KEY" in source
    assert "aria-label=\"Resize monitor sidebar\"" in source
    assert "resizeMonitorSidebarByKeyboard" in source
    assert "aria-valuenow={Math.round(monitorSidebarWidthPct)}" in source
    assert "event.key === 'Home'" in source
    assert "event.key === 'End'" in source
    assert "cursor-col-resize" in source
    assert ": { width: `max(${monitorSidebarWidthPct}%, ${MIN_MONITOR_SIDEBAR_WIDTH_PX}px)` }}" in source


def test_react_monitor_batches_live_log_chunks_for_stable_progress_rendering():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "LOG_STREAM_FLUSH_MS" in source
    assert "type PendingLiveLogChunk" in source
    assert "pendingLiveLogChunkRef" in source
    assert "flushLiveLogChunkBuffer" in source
    assert "window.setTimeout(flushLiveLogChunkBuffer, LOG_STREAM_FLUSH_MS)" in source
    assert "chunks: [] as PendingLiveLogChunk[]" in source
    assert "const chunkOffset = typeof chunk.offset === 'number' && Number.isFinite(chunk.offset)" in source
    assert "chunkOffset <= nextOffset" in source
    assert "nextContent = appendMonitorLogContent(nextContent, chunk.content)" in source
    assert "return { logContent: nextContent, logOffset: nextOffset, logIdentity: nextIdentity }" in source
    assert "buffer.chunks.push(chunk)" in source
    assert "offset?: number" in types
    assert "log_file_name?: string" in types
    assert "message.log_file_name || liveLog" in source


def test_react_mobile_task_controls_keep_usable_touch_targets():
    action_button = (FRONTEND_COMPONENTS_DIR / "shared" / "ActionButton.tsx").read_text(encoding="utf-8")
    dashboard = (FRONTEND_COMPONENTS_DIR / "dashboard" / "DashboardPage.tsx").read_text(encoding="utf-8")
    sidebar = (FRONTEND_COMPONENTS_DIR / "layout" / "Sidebar.tsx").read_text(encoding="utf-8")
    manager = FRONTEND_MANAGER.read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")
    confirm_dialog = FRONTEND_CONFIRM_DIALOG.read_text(encoding="utf-8")
    pagination = FRONTEND_PAGINATION.read_text(encoding="utf-8")
    toast_host = FRONTEND_TOAST_HOST.read_text(encoding="utf-8")

    assert "min-h-11 gap-1.5 rounded-md px-3 py-1.5 text-xs sm:min-h-9" in action_button
    assert "touch-target inline-flex min-h-11 min-w-0 items-center justify-center" in dashboard
    assert "touch-target inline-flex min-h-11 items-center gap-1" in dashboard
    assert "flex min-h-11 w-full items-center gap-3" in dashboard
    assert "touch-target inline-flex h-11 w-11 flex-none items-center justify-center" in dashboard
    assert "min-h-10" in sidebar
    assert "basis-[12rem]" in monitor
    assert "flex min-h-9 w-full flex-col items-stretch rounded-md border px-2 py-1.5 text-left transition-colors" in monitor
    assert "'touch-target absolute right-0.5 top-0.5 z-10 inline-flex h-11 w-11 items-center justify-center rounded-md p-1 transition-colors" in manager
    assert "sm:right-2 sm:top-1.5 sm:h-9 sm:w-9" in manager
    assert "'touch-target inline-flex h-11 w-11 items-center justify-center rounded-md transition-colors" in manager
    assert "sm:h-9 sm:w-9" in manager
    assert "inline-flex h-11 w-11 items-center justify-center rounded-md" in confirm_dialog
    assert "min-h-11" in confirm_dialog
    assert "inline-flex h-11 w-11 items-center justify-center rounded-md" in pagination
    assert "inline-flex h-11 w-11 flex-none items-center justify-center rounded-md" in toast_host


def test_react_monitor_caps_live_log_state_by_scrollback_rows_for_long_tasks():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "export function trimMonitorLogContent(content: string, maxLines = currentMonitorScrollback())" in store
    assert "const lineLimit = Math.max(0, Math.trunc(maxLines))" in store
    assert "content.charCodeAt(index) !== 10" in store
    assert "if (keptLines > lineLimit)" in store
    assert "const MAX_MONITOR_LOG_CHARS = 4 * 1024 * 1024" in store
    assert "lineTrimmed.slice(-MAX_MONITOR_LOG_CHARS)" in store
    assert "content.slice(-MAX_MONITOR_LOG_CHARS)" in store
    assert "charCodeAt(index) !== 13" not in store
    assert "function isPyrunsLifecycleChunk" in store
    assert "function comparableLogText" in store
    assert "export function appendMonitorLogContent" in store
    assert "comparableLogText(contentTail).endsWith(comparableLogText(text))" in store
    assert "appendMonitorLogContent(s.logContent, text)" in store
    assert "appendMonitorLogContent(state.logContent, logs.content)" in monitor


def test_react_monitor_streams_queued_gpu_log_with_incremental_fallback_and_reconnect():
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")
    log_stream = FRONTEND_LOG_STREAM.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "const QUEUE_LOG_NAME = 'queue.log'" in monitor
    assert "selectedTask?.status === 'queued' ? QUEUE_LOG_NAME : runLogName" in monitor
    assert "selectedTask.status === 'queued' && (!selectedLog || selectedLog === QUEUE_LOG_NAME)" in monitor
    assert "const canUseLogStream = isLive" in monitor
    assert "onDisconnect?: () => void" in log_stream
    assert "onStatusChange?: (status: LogStreamStatus) => void" in log_stream
    assert "createLogStream(taskName: string, options:" in api
    assert "sp.set('log_file_name', options.logFileName)" in api
    assert "sp.set('offset', String(options.offset))" in api
    assert "offsetRef.current = offset" in log_stream
    assert "const ws = createLogStream(taskName, {" in log_stream
    assert "logFileName," in log_stream
    assert "offset: offsetRef.current," in log_stream
    assert "logIdentity: logIdentityRef.current" in log_stream
    assert "[taskName, enabled, disconnect, generationKey, logFileName]" in log_stream
    assert "const onDisconnectRef = useRef(onDisconnect)" in log_stream
    assert "ws.onclose = () => {" in log_stream
    assert "onDisconnectRef.current?.()" in log_stream
    assert "ws.onclose = null" in log_stream
    assert "LOG_STREAM_RECONNECT_BASE_MS" in log_stream
    assert "window.setTimeout(connect, retryDelay)" in log_stream
    assert "const handleLogStreamDisconnect = useCallback(() => {" in monitor
    assert "flushLiveLogChunkBuffer()" in monitor
    assert "wsStreamActiveRef.current = false" in monitor
    assert "onDisconnect: handleLogStreamDisconnect" in monitor
    assert "onStatusChange: handleLogStreamStatus" in monitor
    assert "enabled: !loading && isLive && canUseLogStream" in monitor
    assert "logFileName: selectedLog || liveLogName || undefined" in monitor
    assert "offset: logOffsetRef.current" in monitor
    assert "(canUseLogStream && wsStreamActiveRef.current)" in monitor
    assert "offset: currentOffset" in monitor
    assert "tailLines: monitorScrollback" not in monitor[monitor.index("const pollLiveLog"):monitor.index("const filteredTasks")]
    assert "usePolling(pollLiveLog, 1500, Boolean(isLive), false)" in monitor
    assert "queuedLiveLogTaskRef" in monitor
    assert "manualHistoricalLogRef" in monitor
    assert "const viewingQueueOrLiveLog = !selectedLog || selectedLog === QUEUE_LOG_NAME" in monitor
    assert "taskStatus === 'queued' && viewingQueueOrLiveLog" in monitor
    assert "taskStatus !== 'running'" in monitor
    assert "selectedLog && selectedLog !== QUEUE_LOG_NAME && selectedLog !== runLogName" in monitor
    assert "selectLogFile(runLogName)" in monitor
    assert "queueToRunTransition" in monitor
    assert "RUN_LOG_PATTERN.test(messageLog)" in monitor


def test_react_monitor_isolates_workspace_and_resets_replaced_log_streams():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")
    log_stream = FRONTEND_LOG_STREAM.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "function resetWorkspaceScopedState(nextWorkspaceKey: string)" in store
    assert "monitorWorkspaceKey: nextWorkspaceKey" in store
    assert "workspaceKey: nextWorkspaceKey" in store
    assert "monitorTasks: []" in store
    assert "selectedTaskName: null" in store
    assert "logIdentity: ''" in store
    assert "exportIds: new Set()" in store
    assert "workspaceKey !== currentWorkspaceKey()" in store
    assert "get().monitorWorkspaceKey !== workspaceKey" in store
    assert "currentWorkspaceKey() !== workspaceKey" in store

    assert "generationKey?: string" in log_stream
    assert "generationKeyRef.current !== connectedGenerationKey" in log_stream
    assert "msg.type === 'reset'" in log_stream
    assert "type: 'chunk' | 'reset'" in types
    assert "log_identity?: string" in types
    assert "sp.set('log_identity', options.logIdentity)" in api
    assert "message.type === 'reset'" in monitor
    assert "pendingLiveLogChunkRef.current = { key: '', chunks: [] }" in monitor
    assert "logContent: message.content || ''" in monitor
    assert "Boolean(logs.reset)" in monitor
    assert "generationKey: workspaceKey" in monitor
    assert "detailWorkspaceKeyRef.current === workspaceKey" in monitor


def test_react_monitor_uses_realtime_task_events_and_preserves_current_selection_after_actions():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")
    log_stream = FRONTEND_LOG_STREAM.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "monitorStatusCounts: TaskStatusCounts | null" in store
    assert "monitorStatusCounts: page.status_counts ?? null" in store
    assert "background?: boolean" in store
    assert "const background = Boolean(options.background)" in store
    assert "createTaskEventStream" in api
    assert "/api/tasks/events" in api
    assert "export interface TaskEventMessage" in types
    assert "type: 'ready' | 'changed' | 'heartbeat'" in types
    assert "export function useTaskEvents" in log_stream
    assert "message.type === 'ready'" in log_stream
    assert "message.type === 'changed'" in log_stream
    assert "generationKey: workspaceKey" in monitor
    assert "TASK_EVENT_DEGRADED_POLL_MS = 5_000" in monitor
    assert "TASK_EVENT_FALLBACK_POLL_MS = 60_000" in monitor
    assert "document.addEventListener('visibilitychange', handleVisibilityChange)" in monitor
    assert "Task list updates live" in monitor
    assert "Task changes appear automatically" in monitor
    assert "3s sync" not in monitor
    assert "10s sync" not in monitor
    assert "const stillSelected = workspaceKeyRef.current === requestedWorkspaceKey" in monitor
    assert "&& selectedTaskNameRef.current === currentTaskName" in monitor
    assert "if (stillSelected) {" in monitor
    assert "await selectTask(currentTaskName)" in monitor


def test_react_monitor_restores_terminal_focus_and_announces_search_count():
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "window.requestAnimationFrame(() => xtermRef.current?.focus())" in monitor
    assert "closeTerminalSearch(false)" in monitor
    assert 'aria-live="polite"' in monitor
    assert 'aria-atomic="true"' in monitor
    assert "Passes current thresholds" in monitor

def test_react_monitor_memoizes_task_list_derivations_and_coalesces_task_events():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "useMemo" in source
    assert "const selectedTaskFromList = useMemo(" in source
    assert "monitorTasks.find(task => task.name === selectedTaskName)" in source
    assert "taskRefreshInFlightRef" in source
    assert "taskRefreshQueuedRef" in source
    assert "TASK_EVENT_REFRESH_DEBOUNCE_MS" in source
    assert "refreshMonitorSnapshotRef.current()" in source
    assert "const filteredTasks = monitorTasks" in source
    assert "const pinnedTasks = useMemo(" in source
    assert "const otherTasks = useMemo(" in source
    assert "const allExportSelected = useMemo(" in source


def test_react_monitor_writes_terminal_deltas_without_full_screen_repaint():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "renderedLogRef" in source
    assert "appendedMonitorLogDelta" in source
    assert "return previous ? null : ''" in source
    assert "if (previous.endsWith(next))" in source
    assert "next.startsWith(previous.slice(candidate))" in source
    assert "const nextChunk = logOffset < previous.offset" in source
    assert ": appendedMonitorLogDelta(previous.content, logContent)" in source
    assert "term.write(nextChunk)" in source
    assert "normalize_log_newlines" not in source


def test_react_monitor_supports_terminal_search_shortcut_and_controls():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")
    search_policy = FRONTEND_MONITOR_SEARCH.read_text(encoding="utf-8")

    assert "SearchAddon, type ISearchOptions" in source
    assert "TERMINAL_SEARCH_HIGHLIGHT_LIMIT = 250" in search_policy
    assert "TERMINAL_SEARCH_DECORATION_ROW_LIMIT = 10_000" in search_policy
    assert "TERMINAL_SEARCH_DEBOUNCE_MS = 150" in search_policy
    assert "const TERMINAL_SEARCH_OPTIONS: ISearchOptions" in source
    assert "searchAddonRef" in source
    assert "new SearchAddon({ highlightLimit: TERMINAL_SEARCH_HIGHLIGHT_LIMIT })" in source
    assert "term.loadAddon(searchAddon)" in source
    assert "shouldDecorateTerminalSearch(term.buffer.active.length)" in source
    assert "? { ...TERMINAL_SEARCH_OPTIONS, incremental }" in source
    assert ": { incremental }" in source
    assert "terminalSearchTimerRef" in source
    assert "setTerminalSearchStatus('Searching...')" in source
    assert "}, TERMINAL_SEARCH_DEBOUNCE_MS)" in source
    assert 'defaultValue=""' in source
    assert "value={terminalSearchQuery}" not in source
    assert "window.addEventListener('keydown', handleTerminalSearchShortcut, true)" in source
    assert "terminalSearchShortcutScopeRef" in source
    assert "shortcutTargetsTerminal" in source
    assert "key === 'f'" in source
    assert "setTerminalSearchOpen(true)" in source
    assert 'aria-label="Search terminal logs"' in source
    assert "bg-[#252526]" in source
    assert "text-[#cccccc]" in source
    assert "text-[#f48771]" in source
    assert "runPendingTerminalSearchNow(event.shiftKey ? 'previous' : 'next')" in source
    assert 'aria-label="Previous match"' in source
    assert 'aria-label="Next match"' in source
    assert 'aria-label="Close terminal search"' in source


def test_react_monitor_supports_configurable_terminal_line_height():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")
    settings_source = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "utils" / "monitorSettings.ts").read_text(encoding="utf-8")

    assert "resolveMonitorLineHeight" in source
    assert "const monitorLineHeight = resolveMonitorLineHeight(workspace?.settings)" in source
    assert "lineHeight: DEFAULT_MONITOR_LINE_HEIGHT" in source
    assert "xtermRef.current.options.lineHeight = monitorLineHeight" in source
    assert "fitAddonRef.current?.fit()" in source
    assert "monitor_line_height" in settings_source
    assert "DEFAULT_MONITOR_LINE_HEIGHT" in settings_source


def test_react_search_input_clear_button_is_accessible():
    source = FRONTEND_SEARCH_INPUT.read_text(encoding="utf-8")

    assert "ariaLabel = 'Search'" in source
    assert "aria-label={ariaLabel}" in source
    assert 'aria-label="Clear search"' in source
    assert 'title="Clear search"' in source
    assert "inline-flex h-11 w-11" in source
    assert "items-center justify-center" in source
    assert "sm:h-7 sm:w-7" in source
    assert "focus:ring-2 focus:ring-accent/25" in source


def test_react_code_editor_focuses_from_blank_editor_area():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")
    runtime_panel = (FRONTEND_COMPONENTS_DIR / "layout" / "RuntimePanel.tsx").read_text(encoding="utf-8")
    editor = FRONTEND_CODE_EDITOR.read_text(encoding="utf-8")
    css = FRONTEND_THEME_CSS.read_text(encoding="utf-8")

    assert "CodeTextEditor" in generator
    assert "CodeTextEditor" in runtime_panel
    assert "function CodeTextEditor" in editor
    assert "editorViewRef" in editor
    assert "focusEditorFromBlankArea" in editor
    assert "target.closest('.cm-content')" in editor
    assert "target.closest('.cm-gutters')" in editor
    assert "target.closest('button')" in editor
    assert "onCreateEditor={view => { editorViewRef.current = view }}" in editor
    assert "view.dispatch({ selection: { anchor: view.state.doc.length }, scrollIntoView: true })" in editor
    assert "onMouseDown={focusEditorFromBlankArea}" in editor
    assert "cursor-text" in editor
    assert ".code-text-editor .cm-content" in css
    assert "min-width: 100%;" not in css


def test_react_code_editor_has_no_horizontal_scrollbar():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")
    runtime_panel = (FRONTEND_COMPONENTS_DIR / "layout" / "RuntimePanel.tsx").read_text(encoding="utf-8")
    editor = FRONTEND_CODE_EDITOR.read_text(encoding="utf-8")
    css = FRONTEND_THEME_CSS.read_text(encoding="utf-8")

    assert "EditorView.lineWrapping" in editor
    assert "aria-pressed={wrap}" in editor
    assert "WrapText" in editor
    assert "wrapStorageKey" in editor
    assert 'wrapStorageKey="pyruns.generator.shell.wrap"' in generator
    assert 'wrapStorageKey="pyruns.generator.yaml.wrap"' in generator
    assert 'wrapStorageKey="pyruns.runtime.env.wrap"' in runtime_panel
    assert "overflow: auto;" in css
    assert ".cm-editor.cm-lineWrapping .cm-scroller" in css
    assert "white-space: pre-wrap;" not in css


def test_react_shell_editor_uses_a_base_tag_before_definition_modifier():
    editor = FRONTEND_CODE_EDITOR.read_text(encoding="utf-8")

    assert "return 'variableName.definition'" in editor
    assert "return 'definition'" not in editor


def test_react_runtime_panel_stays_compact_and_low_chrome():
    runtime_panel = (FRONTEND_COMPONENTS_DIR / "layout" / "RuntimePanel.tsx").read_text(encoding="utf-8")
    editor = FRONTEND_CODE_EDITOR.read_text(encoding="utf-8")

    assert "w-[620px]" in runtime_panel
    assert "inline-flex rounded-md bg-surface-overlay p-0.5" in runtime_panel
    assert "compactToolbar" in runtime_panel
    assert "compactToolbar?: boolean" in editor
    assert "{!compactToolbar &&" in editor
    assert "absolute right-1.5 top-1.5" in editor
    assert "aria-label={wrap ? 'Disable line wrapping' : 'Enable line wrapping'}" in editor


def test_monitor_surfaces_structured_gpu_wait_and_bounded_log_tail_state():
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "GPUWaitPanel" in monitor
    assert "Waiting for GPU capacity" in monitor
    assert "requested_gpu_count" in monitor
    assert "eligible_gpu_count" in monitor
    assert "Why each GPU is waiting" in monitor
    assert "logTailTruncated" in store
    assert "tail_truncated?: boolean" in types
    assert "Showing the latest" in monitor
    assert "New output continues live." in monitor


def test_monitor_prevents_duplicate_task_actions_and_exposes_stream_state():
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "taskActionPending" in monitor
    assert "if (!selectedTaskName || !selectedTask || taskActionPending) return" in monitor
    assert "disabled={taskActionPending !== null}" in monitor
    assert "setTaskActionPending(null)" in monitor
    assert 'aria-live="polite"' in monitor
    assert "streamStatus === 'reconnecting'" in monitor
    assert "incremental polling remains active while reconnecting" in monitor


def test_react_runtime_panel_loads_and_saves_conda_runtime_choices():
    runtime_panel = (FRONTEND_COMPONENTS_DIR / "layout" / "RuntimePanel.tsx").read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")
    store = FRONTEND_STORE.read_text(encoding="utf-8")

    assert "export const getRuntimeInfo = () => request<RuntimeInfo>('/api/runtime')" in api
    assert "export const updateRuntimeInfo" in api
    assert "export const useRuntimeStore = create<RuntimeState>" in store
    assert "const next = await fetchRuntime()" in runtime_panel
    assert "applyRuntimeState(next)" in runtime_panel
    assert "refresh_providers=${refreshProviders}" in api
    assert "const next = await updateRuntime(payload, false)" in runtime_panel
    assert "applyRuntimePageState(next, page)" in runtime_panel
    assert "pageRevision === runtimePageRevisionsRef.current[page]" in runtime_panel
    assert "clearRuntimeDirtyPage(page)" in runtime_panel
    assert "runtimeLoadSeqRef" in runtime_panel
    assert "refreshWorkspaceInBackground" in runtime_panel
    assert "void refreshWorkspace().catch" in runtime_panel
    assert "Runtime saved, workspace refresh failed" in runtime_panel
    assert "await refreshWorkspace()" not in runtime_panel
    assert "setCondaEnv(next.conda_env)" in runtime_panel
    assert "setCondaExecutable(next.conda_executable || 'conda')" in runtime_panel
    assert "setRuntimeMode(modeFromRuntime(next))" in runtime_panel
    assert "runtime?.conda.envs.map(env =>" in runtime_panel
    assert "runtime?.process.conda_env && !runtime.conda.envs.some(env => env.name === runtime.process.conda_env)" in runtime_panel
    assert "setCondaEnv(runtime?.conda_env || runtime?.process.conda_env || activeConda || runtime?.conda.envs[0]?.name || '')" in runtime_panel
    assert "conda_env: condaEnv" in runtime_panel
    assert "conda_executable: condaExecutable" in runtime_panel
    assert "python_executable: ''" in runtime_panel
    assert "selectedConda?.python_executable || 'Choose a conda environment to preview Python path'" in runtime_panel


def test_react_runtime_panel_exposes_gpu_scheduler_settings():
    runtime_panel = (FRONTEND_COMPONENTS_DIR / "layout" / "RuntimePanel.tsx").read_text(encoding="utf-8")
    toggle_switch = (FRONTEND_COMPONENTS_DIR / "shared" / "ToggleSwitch.tsx").read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "type RuntimePage = 'python' | 'env' | 'gpu'" in runtime_panel
    assert "GPU scheduling" in runtime_panel
    assert "<ToggleSwitch" in runtime_panel
    assert "checked={gpuSchedulerEnabled}" in runtime_panel
    assert 'role="switch"' in toggle_switch
    assert "aria-checked={checked}" in toggle_switch
    assert "absolute left-[3px] top-[3px]" in toggle_switch
    assert "checked ? 'translate-x-5' : 'translate-x-0'" in toggle_switch
    assert "Advanced scheduling rules" in runtime_panel
    assert "Auto pick" in runtime_panel
    assert "Specific indices" in runtime_panel
    assert "GPU indices" in runtime_panel
    assert "Maximum memory use" in runtime_panel
    assert "Minimum free memory" in runtime_panel
    assert "Maximum compute use" in runtime_panel
    assert "Maximum wait" in runtime_panel
    assert "useState('40')" in runtime_panel
    assert "useState('15')" in runtime_panel
    assert "useState(48)" in runtime_panel
    assert "max_wait_seconds ?? 172800" in runtime_panel
    assert "setGpuSchedulerEnabled(next.gpu_scheduler?.enabled ?? false)" in runtime_panel
    assert "setGpuSelectionMode(next.gpu_scheduler?.selection_mode === 'specified' ? 'specified' : 'auto')" in runtime_panel
    assert "setGpuRequireSameModel(next.gpu_scheduler?.require_same_gpu_model ?? false)" in runtime_panel
    assert "function boundedNumberInputValue(value: string, fallback: number, minimum: number, maximum: number)" in runtime_panel
    assert "return Array.from(new Set(" in runtime_panel
    assert "const selectedGpuIds = useMemo(() => parseDeviceIds(gpuDeviceIds)" in runtime_panel
    assert "gpuValidationIssues" in runtime_panel
    assert "free is not possible" in runtime_panel
    assert "loadGpuMetrics" in runtime_panel
    assert "passingGpuCount" in runtime_panel
    assert "GPU UUIDs and MIG IDs are validated" in runtime_panel
    assert "aria-pressed={gpuSelectionMode === item.id}" in runtime_panel
    assert "min={1}" in runtime_panel
    assert "selection_mode: gpuSelectionMode" in runtime_panel
    assert "gpus_per_task: requestedGpuCount" in runtime_panel
    assert "memory_used_pct: boundedNumberInputValue(gpuMemoryUsedPct, 40, 0, 100)" in runtime_panel
    assert "compute_used_pct: boundedNumberInputValue(gpuComputeUsedPct, 30, 0, 100)" in runtime_panel
    assert "stable_seconds: numberInputValue(gpuStableSeconds, 15, 1)" in runtime_panel
    assert "max_wait_seconds: gpuMaxWaitHours * 3600" in runtime_panel
    assert "require_same_gpu_model: gpuRequireSameModel" in runtime_panel
    assert "Require the same model for multi-GPU tasks" in runtime_panel
    assert "sample_interval_seconds" not in runtime_panel
    assert "gpu_scheduler_sample_interval_seconds" not in runtime_panel
    assert "disabled={saving || !runtimeDirtyPages.gpu || gpuValidationIssues.length > 0}" in runtime_panel
    assert "applyWorkspaceRuntimeSettings(workspaceSettings)" in runtime_panel
    assert "gpu_scheduler:" in runtime_panel
    assert "GpuSchedulerSettings" in types
    assert "selection_mode: 'auto' | 'specified'" in types
    assert "require_same_gpu_model: boolean" in types
    assert "sample_interval_seconds" not in types
    assert "gpu_scheduler?: Partial<GpuSchedulerSettings>" in api


def test_react_launcher_supports_manual_shell_folder_paths():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "manualShellRootPath" in launcher
    assert "handleManualShellRoot" in launcher
    assert "openLauncherShellRoot(shellPath)" in launcher
    assert "openLauncherShellRoot" in api
    assert "/api/launcher/open-shell-root" in api


def test_react_launcher_modal_and_path_controls_fit_narrow_viewports():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "p-3 sm:p-4" in launcher
    assert "max-w-[calc(100vw-1.5rem)] sm:max-w-2xl" in launcher
    assert launcher.count("flex flex-col gap-2 sm:flex-row sm:items-center") >= 2
    assert launcher.count("w-full min-w-0 flex-1") >= 2
    assert launcher.count("sm:w-auto sm:flex-none") >= 2


def test_react_launcher_modal_owns_focus_and_restores_the_trigger():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "const modalRef = useRef<HTMLDivElement>(null)" in launcher
    assert "const previousFocusRef = useRef<HTMLElement | null>(null)" in launcher
    assert "modalRef.current?.querySelectorAll<HTMLElement>" in launcher
    assert "window.requestAnimationFrame" in launcher
    assert "previousFocus?.isConnected" in launcher
    assert "previousFocus !== document.body" in launcher
    assert "[data-launcher-trigger=\"true\"]" in launcher
    assert 'data-launcher-trigger="true"' in sidebar
    assert 'aria-labelledby="launcher-dialog-title"' in launcher
    assert 'aria-describedby="launcher-dialog-description"' in launcher
    assert "event.key !== 'Tab'" in launcher


def test_react_launcher_disables_browse_when_native_picker_unavailable():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "native_file_picker?: boolean" in types
    assert "const workspace = useWorkspaceStore(state => state.workspace)" in launcher
    assert "const nativePickerAvailable = workspace?.native_file_picker === true" in launcher
    assert "pickerAvailable={nativePickerAvailable}" in launcher
    assert "pickerAvailable: boolean" in launcher
    assert "disabled={!pickerAvailable}" in launcher
    assert "Browse Unavailable" in launcher
    assert "Native picker unavailable on this server; enter the path manually." in launcher


def test_react_launcher_browse_script_enters_config_selection_before_opening():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "pickLauncherScriptPath" in api
    assert "/api/launcher/pick-script-path" in api
    assert "const selection = await api.pickLauncherScriptPath()" in launcher
    assert "setManualScriptPath(selection.script_path)" in launcher
    assert "await openPythonPath(selection.script_path)" in launcher
    assert "Browse Script" in launcher


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


def test_react_launcher_route_parameters_prefill_without_opening_workspace():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "const initialLaunchMode = scriptParam ? 'python' : modeParam === 'shell' ? 'shell' : 'python'" in launcher
    assert "const configParam = searchParams.get('config')" in launcher
    assert "setManualScriptPath(scriptParam)" in launcher
    assert "setManualConfigPath(configParam || '')" in launcher
    assert "openSelectedWorkspace(scriptParam, configParam)" not in launcher
    assert "const handleLaunchModeChange = useCallback((mode: 'python' | 'shell')" in launcher
    assert "<LaunchChoiceTabs launchMode={launchMode} busy={loading} onChange={handleLaunchModeChange}" in launcher


def test_react_launcher_fetch_does_not_clobber_newer_selection():
    store = FRONTEND_STORE.read_text(encoding="utf-8")

    assert "let launcherRequestSeq = 0" in store
    assert "const requestId = ++launcherRequestSeq" in store
    assert "if (requestId !== launcherRequestSeq)" in store


def test_react_shared_stores_ignore_stale_async_responses():
    store = FRONTEND_STORE.read_text(encoding="utf-8")

    assert "let runtimeRequestSeq = 0" in store
    assert "let dashboardRequestSeq = 0" in store
    assert "let generatorTemplateRequestSeq = 0" in store
    assert "const requestId = ++runtimeRequestSeq" in store
    assert "requestId === runtimeRequestSeq && workspaceKey === currentWorkspaceKey()" in store
    assert "const requestId = ++dashboardRequestSeq" in store
    assert "requestId === dashboardRequestSeq && workspaceKey === currentWorkspaceKey()" in store
    assert "const requestId = ++generatorTemplateRequestSeq" in store
    assert "requestId !== generatorTemplateRequestSeq" in store
    assert "draftVersion !== generatorDraftVersion" in store
    assert "|| workspaceKey !== currentWorkspaceKey()" in store


def test_react_launcher_deduplicates_open_and_only_closes_after_success():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "let launcherOpenPromise: Promise<boolean> | null = null" in store
    assert "if (launcherOpenPromise)" in store
    assert "return launcherOpenPromise" in store
    assert "return false" in store
    assert "return true" in store
    assert "const opened = await useLauncherStore.getState().openWorkspace()" in launcher
    assert "if (!opened || !launcherMountedRef.current)" in launcher
    assert "aria-busy={loading || undefined}" in launcher
    assert "busy={loading}" in launcher


def test_react_launcher_prompts_for_yaml_when_load_script_has_workspace_default():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "const workspaceDefault = res.items.find(item => item.kind === 'workspace_default')" in store
    assert "selectedConfig: shouldPromptForConfig ? '' : workspaceDefault?.path || ''" in store
    assert "const shouldPromptForConfig = (res.config_source || '') === 'pyruns_load'" in store
    assert "step: workspaceDefault && !shouldPromptForConfig ? 2 : 1" in store
    assert "const mustChooseConfig = requiresConfigTemplate || configSource === 'pyruns_load'" in launcher
    assert "pyruns.load() reads the selected YAML for this workspace." in launcher


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

    assert "const openPythonPath = useCallback(async (path: string)" in launcher
    assert "setError('')" in launcher
    assert "await selectScript(scriptPath)" in launcher
    assert "const handleSelectConfig = useCallback(async (configPath: string)" in launcher
    assert "selectConfig(configPath)" in launcher
    assert "onClick={() => void handleSelectConfig(config.path)}" in launcher


def test_react_launcher_browses_and_opens_yaml_config():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "pickLauncherConfigPath" in api
    assert "/api/launcher/pick-config-path" in api
    assert "'manual'" in types
    assert "const openSelectedConfig = useCallback(async (configPath: string)" in launcher
    assert "const handlePickConfig = useCallback(async ()" in launcher
    assert "api.pickLauncherConfigPath(selectedScript)" in launcher
    assert "Open Config Path" in launcher
    assert "Browse Config" in launcher
    assert "Path to YAML config" in launcher


def test_react_launcher_config_step_uses_path_picker_panel():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "function ConfigActionPanel" in launcher
    assert "configPathReady" in launcher
    assert "api.validateLauncherPath('config', debouncedConfigPath, selectedScript)" in launcher
    assert "validation={configValidation}" in launcher
    assert "PathValidationHint id={validationId} validation={validation} pathValue={pathValue}" in launcher


def test_react_launcher_validation_is_bound_to_the_current_labelled_path():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "validatedPath: string" in launcher
    assert "scriptValidation.validatedPath === manualScriptPath.trim()" in launcher
    assert "configValidation.validatedPath === manualConfigPath.trim()" in launcher
    assert "shellValidation.validatedPath === manualShellRootPath.trim()" in launcher
    assert "validation.validatedPath !== currentPath" in launcher
    assert 'role="status"' in launcher
    assert 'aria-live="polite"' in launcher
    assert 'aria-pressed={launchMode === \'python\'}' in launcher
    assert 'aria-pressed={launchMode === \'shell\'}' in launcher
    assert "Python script path" in launcher
    assert "Shell workspace folder path" in launcher
    assert "YAML config path" in launcher


def test_react_generator_nested_form_uses_tree_depth_guides():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "depth={depth + 1}" in generator
    assert "treeSection" in generator
    assert "treeConnector" in generator
    assert "border-l border-dashed border-border-strong/60" in generator
    assert "'ml-4 border-l border-dashed border-border-strong/60 pb-1 pl-4 pt-1'" in generator
    assert "!treeSection && depth > 0 && 'border-l-2 border-border pl-3'" in generator
    assert "aria-expanded={open}" in generator
    assert "title={`${prefix} (${Object.keys(data).length} fields)`}" in generator


def test_react_generator_has_tree_layout_and_expand_controls():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "type FormLayoutMode = 'grid' | 'tree'" in generator
    assert "type GeneratorDisplayMode = FormLayoutMode | 'yaml' | 'shell'" in generator
    assert "['grid', 'tree', 'yaml'] as GeneratorDisplayMode[]" in generator
    assert "handleDisplayModeChange" in generator
    assert "formLayoutMode" in generator
    assert "setFormLayoutMode" in generator
    assert "Grid" in generator
    assert "Tree" in generator
    assert "YAML" in generator
    assert "Expand all" in generator
    assert "Collapse all" in generator
    assert "treeOpenSignal" in generator
    assert "setOpen(openSignalValue)" in generator
    assert "const effectiveColumns = Math.max(1, columns)" in generator
    assert "buildColumnGridStyle(effectiveColumns)" in generator
    assert "repeat(${columns}, minmax(20rem, 1fr))" in generator
    assert "const contentClassName = layoutMode === 'tree' ? 'space-y-1.5' : 'grid gap-x-3 gap-y-1.5 overflow-x-auto pb-0.5'" in generator
    assert "const childSectionClassName = layoutMode === 'tree' ? 'w-full' : 'col-span-full'" in generator
    assert "onSetAllSections={setAllTreeSections}" in generator
    assert "hasNestedSections && (" in generator
    assert "outlineSections.length > 1 && (" in generator
    assert "<SectionExpandControls onSetAllSections={onSetAllSections} />" in generator
    assert "layoutMode={formLayoutMode}" in generator
    assert "min-w-[280px]" in generator
    assert "flex w-full flex-wrap items-center justify-end gap-2 min-[701px]:ml-auto min-[701px]:w-auto" in generator


def test_react_generator_grid_mode_keeps_sibling_fields_on_one_row():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "function buildColumnGridStyle(columns: number)" in generator
    assert "const contentClassName = 'grid gap-x-3 gap-y-1.5 overflow-x-auto pb-0.5'" in generator


def test_react_generator_grid_param_rows_keep_label_type_and_input_inline():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "grid min-h-7 grid-cols-[minmax(9.5rem,0.68fr)_minmax(10rem,1.32fr)]" in generator
    assert "flex min-w-0 items-center gap-1.5" in generator
    assert '<div className="min-w-0 w-full">' in generator
    grid_row_start = generator.index("if (!treeParamRow)")
    name_position = generator.index("title={name}", grid_row_start)
    type_position = generator.index("PARAM_TYPE_STYLES[originalType]", grid_row_start)
    input_position = generator.index('<div className="min-w-0 w-full">', grid_row_start)
    assert name_position < type_position < input_position


def test_react_generator_tree_mode_uses_outline_explorer():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "function TreeParameterExplorer" in generator
    assert "function RootSectionOverview" in generator
    assert "function SearchResultRows" in generator
    assert "collectTreeSections(data)" in generator
    assert "collectParamRows(data, declaredTypeMap, batchParams)" in generator
    assert "Outline" in generator
    assert "outlineCollapsed" in generator
    assert "setOutlineCollapsed(true)" in generator
    assert "setOutlineCollapsed(false)" in generator
    assert "Search path or value" in generator
    assert "Search results" in generator
    assert "No matching parameters." in generator
    assert "TREE_OUTLINE_WIDTH_STORAGE_KEY" in generator
    assert "clampTreeOutlineWidth" in generator
    assert "readStoredTreeOutlineWidth" in generator
    assert "startOutlineResize" in generator
    assert "pendingOutlineWidthRef" in generator
    assert "outlineResizeFrameRef" in generator
    assert "gridTemplateColumns: `${outlineWidth}px 4px minmax(0,1fr)`" in generator
    assert 'aria-label="Resize parameter outline"' in generator
    assert "grid-cols-[minmax(0,1fr)]" in generator
    assert "columns={1}" in generator
    assert "onClick={() => onSelectPath(section.path)}" in generator


def test_react_generator_tree_param_rows_keep_value_inputs_aligned():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")
    toggle_switch = (FRONTEND_COMPONENTS_DIR / "shared" / "ToggleSwitch.tsx").read_text(encoding="utf-8")

    assert "layoutMode?: FormLayoutMode" in generator
    assert "const treeParamRow = layoutMode === 'tree'" in generator
    assert "treeParamRow" in generator
    assert "grid min-h-10 grid-cols-[24px_minmax(0,1fr)]" in generator
    assert "sm:grid-cols-[24px_minmax(150px,0.95fr)_minmax(150px,1.05fr)]" in generator
    assert "border-border bg-surface-raised" in generator
    assert "treeParamRow ? 'min-w-0' : 'flex-1'" in generator
    assert "treeParamRow ? 'min-w-0 justify-start' : 'flex-none justify-end'" in generator
    assert "treeParamRow ? 'col-start-2 min-w-0 w-full sm:col-start-auto' : 'ml-auto min-w-0 flex-1'" in generator
    assert 'aria-label={`${name} parameter value`}' in generator
    assert "checked={Boolean(value)}" in generator
    assert 'role="switch"' in toggle_switch
    assert "aria-checked={checked}" in toggle_switch
    assert "if (!treeParamRow)" in generator
    assert "group grid min-h-7 grid-cols-[minmax(9.5rem,0.68fr)_minmax(10rem,1.32fr)] items-center gap-2 rounded-md border border-border bg-surface-raised px-1.5 py-0.5 shadow-sm transition-all hover:border-border-strong hover:bg-surface-hover focus-within:border-accent/60 focus-within:bg-surface-raised focus-within:ring-2 focus-within:ring-accent/15" in generator
    assert "pinned ? 'border-l-2 border-l-accent border-y-accent/20 border-r-accent/20 bg-accent/[0.03] ring-1 ring-accent/20' : ''" in generator
    assert "focus-within:border-accent/60 focus-within:bg-surface-raised focus-within:ring-2 focus-within:ring-accent/15" in generator
    assert "h-6 w-full rounded-md border bg-[var(--input-bg)]" in generator
    assert "focus:border-accent focus:bg-surface-raised focus:ring-2 focus:ring-accent/15" in generator
    assert "focus-within:border-accent/60 focus-within:bg-surface-raised focus-within:ring-2 focus-within:ring-accent/20" in generator
    assert "hover:border-border-strong focus:border-accent focus:bg-surface-raised focus:ring-2 focus:ring-accent/15" in generator
    assert "focus-visible:ring-2 focus-visible:ring-accent/30" in toggle_switch


def test_react_generator_shell_mode_loads_existing_shell_tasks():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "handlePickShellFile" in generator
    assert "api.pickGeneratorShellFile()" in generator
    assert "Load task" in generator
    assert "Search tasks" in generator
    assert "No matching tasks" in generator
    assert "Browse Shell" in generator
    assert "templates.some(template => template.value === selectedTemplate)" in generator
    assert "/api/generator/pick-shell-file" in api


def test_react_generator_template_picker_supports_search():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "function TemplatePicker" in generator
    assert "templateFilter" in generator
    assert "filteredOptions" in generator
    assert "Search templates" in generator
    assert "No matching templates" in generator
    assert 'role="listbox"' in generator
    assert 'aria-expanded={open}' in generator


def test_react_generator_shows_imported_default_config_source():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")
    types = FRONTEND_TYPES.read_text(encoding="utf-8")

    assert "config_default_source_name" in types
    assert "configDefaultSourceName" in generator
    assert "Loaded from" in generator
    assert "pathLeaf(selectedTemplate) === 'config_default.yaml'" in generator
    assert "max-w-full select-text items-start" in generator
    assert "whitespace-normal break-all font-mono" in generator


def test_react_generator_reloads_default_when_imported_yaml_changes():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "lastWorkspaceDefaultKeyRef" in generator
    assert "workspaceDefaultKey" in generator
    assert "workspaceDefaultChanged" in generator
    assert "defaultTemplate = templates.find(template => pathLeaf(template.value) === 'config_default.yaml')" in generator
    assert "workspace?.config_default_source" in generator
    assert "workspace?.config_default_source_name" in generator
    assert "loadTemplateWithFeedback(defaultTemplateValue)" in generator


def test_react_generator_keeps_workspace_default_selected_after_create():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "loadTemplate(generatedTemplateValue)" not in generator
    assert "buildGeneratedTemplateValue" not in generator
    assert "await fetchTemplates()" in generator
    assert "firstTaskName: result.items[0]?.name || ''" in generator


def test_react_batch_preview_uses_readable_summary_and_structured_rows():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")
    dialog = FRONTEND_CONFIRM_DIALOG.read_text(encoding="utf-8")

    assert "function BatchPreviewContent" in generator
    assert "function BatchPreviewList" in generator
    assert "Tasks to create" in generator
    assert "Task samples" in generator
    assert "formatFullTaskTooltip" in generator
    assert "title={formatFullTaskTooltip(item)}" in generator
    assert "grid-cols-[56px_minmax(0,1fr)]" in generator
    assert "size=\"lg\"" in generator
    assert "size?: 'md' | 'lg'" in dialog


def test_react_shell_mode_has_runtime_status_panel():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "function ShellRuntimePanel" in generator
    assert "Shell Runtime" in generator
    assert "Resolved file" in generator
    assert "Workspace folder" in generator
    assert "getShellConfigFilename" in generator


def test_react_launcher_exposes_direct_mode_actions():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "function LaunchChoiceTabs" in launcher
    assert "function ModeActionPanel" in launcher
    assert "launchMode === 'python'" in launcher
    assert "launchMode === 'shell'" in launcher
    assert "Browse Script" in launcher
    assert "Browse & Open Folder" in launcher


def test_react_sidebar_workspace_card_opens_launcher_with_mode():
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")
    app = (Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "const openWorkspaceLauncher = (mode: 'python' | 'shell')" in sidebar
    assert "nextParams.set('launcher', '1')" in sidebar
    assert "nextParams.set('mode', mode)" in sidebar
    assert "nextParams.delete('script')" in sidebar
    assert "onClick={() => openWorkspaceLauncher(shellWorkspaceActive ? 'shell' : 'python')}" in sidebar
    assert "nextParams.delete('mode')" in app
    assert "const modeParam = searchParams.get('mode')" in launcher
    assert "const initialLaunchMode = scriptParam ? 'python' : modeParam === 'shell' ? 'shell' : 'python'" in launcher
    assert "setLaunchMode(initialLaunchMode)" in launcher


def test_react_launcher_config_step_explains_required_yaml_selection():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "requiresConfigTemplate" in launcher
    assert "Choose a YAML config" in launcher
    assert "This script needs a YAML config before first launch." in launcher
    assert "pyruns will save it as config_default.yaml" in launcher
    assert "Choose or enter a YAML config path first." in launcher
    assert "Path to YAML config" in launcher


def test_react_workspace_switch_invalidates_all_scoped_state():
    store = FRONTEND_STORE.read_text(encoding="utf-8")

    assert "workspaceEpoch" in store
    assert "function resetWorkspaceScopedState" in store
    assert "dashboardRequestSeq += 1" in store
    assert "generatorTemplateRequestSeq += 1" in store
    assert "tasks: []" in store
    assert "data: null" in store
    assert "selectedTemplate: ''" in store
    assert "workspaceKey === currentWorkspaceKey()" in store


def test_react_workspace_refresh_failures_preserve_the_last_successful_snapshot():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    dashboard = FRONTEND_DASHBOARD.read_text(encoding="utf-8")

    assert "error: string | null" in store
    assert "set({ error: err instanceof Error ? err.message : 'Could not load dashboard' })" in store
    assert "set({ error: err instanceof Error ? err.message : 'Could not load tasks' })" in store
    assert "loading && !data ? '--'" in dashboard
    assert "dashboardError && !data" in dashboard
    assert "Task summary refresh failed" in dashboard
    assert "Task summary unavailable" in dashboard
    assert 'role="alert"' in dashboard


def test_react_launcher_url_paths_require_explicit_open():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "setManualScriptPath(scriptParam)" in launcher
    assert "setManualConfigPath(configParam || '')" in launcher
    assert "openSelectedWorkspace(scriptParam" not in launcher
    assert "selectScript(scriptParam)" not in launcher


def test_react_runtime_guards_unsaved_changes_and_generator_editor_is_named():
    runtime_panel = (FRONTEND_COMPONENTS_DIR / "layout" / "RuntimePanel.tsx").read_text(encoding="utf-8")
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "runtimeDirty" in runtime_panel
    assert "Discard unsaved runtime changes?" in runtime_panel
    assert "beforeunload" in runtime_panel
    assert 'ariaLabel="Task YAML editor"' in generator
    assert 'ariaLabel="Task shell editor"' in generator


def test_react_generator_keeps_edits_made_while_tasks_are_created():
    source = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "const generationDraftRevisionRef = useRef(0)" in source
    assert "const requestDraftRevision = generationDraftRevisionRef.current" in source
    assert "generationDraftRevisionRef.current === requestDraftRevision" in source
    assert "const markDraftEdited = useContext(GeneratorDraftEditContext)" in source
    assert source.count("onChange={event => handleLocalValueChange(event.target.value)}") == 2

    dialog_start = source.index("<ConfirmDialog\n        open={previewOpen}")
    dialog_end = source.index("</ConfirmDialog>", dialog_start)
    batch_dialog = source[dialog_start:dialog_end]
    assert 'role="alert"' in batch_dialog
    assert "{error}" in batch_dialog


def test_react_task_detail_save_responses_only_confirm_the_submitted_draft():
    source = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")
    notes_start = source.index("const handleSaveNotes")
    env_start = source.index("const handleSaveEnv", notes_start)
    close_start = source.index("function requestClose", env_start)
    notes_save = source[notes_start:env_start]
    env_save = source[env_start:close_start]

    assert "const notesDraftRevisionRef = useRef(0)" in source
    assert "const envDraftRevisionRef = useRef(0)" in source
    assert "const draftRevision = notesDraftRevisionRef.current" in notes_save
    assert "notesDraftRevisionRef.current === draftRevision" in notes_save
    assert "notesDraftRevisionRef.current += 1" in source
    assert "const draftRevision = envDraftRevisionRef.current" in env_save
    assert "if (envDraftRevisionRef.current === draftRevision)" in env_save
    assert "setEnvPairs(buildEnvPairsFromEnv(savedEnv))" in env_save
    assert "if (envDraftRevisionRef.current !== draftRevision) return" in env_save


def test_react_runtime_reload_preserves_dirty_draft_until_success():
    source = (FRONTEND_COMPONENTS_DIR / "layout" / "RuntimePanel.tsx").read_text(encoding="utf-8")
    refresh_start = source.index("const refreshPanel")
    refresh_end = source.index("if (!open)", refresh_start)
    refresh_panel = source[refresh_start:refresh_end]

    assert "clearAllRuntimeDirty()" not in refresh_panel
    assert "void loadRuntime(true)" in refresh_panel
    assert "runtimeLoadSeqRef.current += 1" in source
    assert "if (loadSeq === runtimeLoadSeqRef.current)" in source


def test_react_terminal_log_enables_screen_reader_mode():
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "screenReaderMode: true" in monitor
    assert 'aria-label={`Read-only logs for ${selectedTaskName}`}' in monitor


def test_react_routes_have_titles_focus_and_not_found_page():
    app = FRONTEND_APP.read_text(encoding="utf-8")

    assert "document.title" in app
    assert "route-heading" in app
    assert 'path="*"' in app
    assert "Page not found" in app
