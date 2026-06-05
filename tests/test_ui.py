from pathlib import Path


FRONTEND_GENERATOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "generator" / "GeneratorPage.tsx"
FRONTEND_APP = Path(__file__).resolve().parents[1] / "frontend" / "src" / "App.tsx"
FRONTEND_COMPONENTS_DIR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components"
FRONTEND_POLLING = Path(__file__).resolve().parents[1] / "frontend" / "src" / "hooks" / "usePolling.ts"
FRONTEND_STORE = Path(__file__).resolve().parents[1] / "frontend" / "src" / "store.ts"
FRONTEND_DASHBOARD = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "dashboard" / "DashboardPage.tsx"
FRONTEND_MONITOR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "monitor" / "MonitorPage.tsx"
FRONTEND_MANAGER = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "manager" / "ManagerPage.tsx"
FRONTEND_LAUNCHER = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "launcher" / "LauncherPage.tsx"
FRONTEND_APP_SHELL = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "layout" / "AppShell.tsx"
FRONTEND_SIDEBAR = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "layout" / "Sidebar.tsx"
FRONTEND_TASK_DETAIL = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "manager" / "TaskDetailPanel.tsx"
FRONTEND_API = Path(__file__).resolve().parents[1] / "frontend" / "src" / "api.ts"
FRONTEND_TYPES = Path(__file__).resolve().parents[1] / "frontend" / "src" / "types.ts"
FRONTEND_CONFIRM_DIALOG = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "shared" / "ConfirmDialog.tsx"
FRONTEND_COMPACT_SECTION = Path(__file__).resolve().parents[1] / "frontend" / "src" / "components" / "shared" / "CompactSection.tsx"
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
    assert "if (requestId !== taskRequestSeq) {" in source


def test_react_dashboard_polling_waits_for_network_work():
    source = FRONTEND_DASHBOARD.read_text(encoding="utf-8")

    assert "const refreshDashboard = useCallback(async () => {" in source
    assert "await Promise.all([" in source
    assert "api.getMetrics().then(setMetrics)" in source


def test_react_app_lazy_loads_route_pages_for_smaller_initial_bundle():
    source = FRONTEND_APP.read_text(encoding="utf-8")

    assert "lazy," in source
    assert "Suspense," in source
    assert "const DashboardPage = lazy(() => import('@/components/dashboard/DashboardPage'))" in source
    assert "const GeneratorPage = lazy(() => import('@/components/generator/GeneratorPage'))" in source
    assert "const ManagerPage = lazy(() => import('@/components/manager/ManagerPage'))" in source
    assert "const MonitorPage = lazy(() => import('@/components/monitor/MonitorPage'))" in source
    assert "const LauncherPage = lazy(() => import('@/components/launcher/LauncherPage'))" in source
    assert "<Suspense fallback={<RouteLoadingFallback />}>" in source
    assert "function RouteLoadingFallback()" in source


def test_react_runtime_panel_is_lazy_loaded_to_keep_editor_out_of_initial_bundle():
    source = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "const RuntimePanel = lazy(() => import('./RuntimePanel'))" in source
    assert "import RuntimePanel from './RuntimePanel'" not in source
    assert "<Suspense fallback={null}>" in source
    assert "{runtimeOpen && (" in source


def test_frontend_index_avoids_external_font_dependencies():
    indexes = [
        FRONTEND_INDEX.read_text(encoding="utf-8"),
        STATIC_INDEX.read_text(encoding="utf-8"),
    ]

    for index in indexes:
        assert "fonts.googleapis.com" not in index
        assert "fonts.gstatic.com" not in index


def test_frontend_html_uses_only_local_runtime_assets():
    indexes = [
        FRONTEND_INDEX.read_text(encoding="utf-8"),
        STATIC_INDEX.read_text(encoding="utf-8"),
    ]

    for index in indexes:
        assert 'href="http' not in index
        assert "href='http" not in index
        assert 'src="http' not in index
        assert "src='http" not in index


def test_built_index_does_not_preload_codemirror_for_initial_shell():
    index = STATIC_INDEX.read_text(encoding="utf-8")

    assert "vendor-codemirror" not in index


def test_react_app_supports_direct_launcher_route():
    source = FRONTEND_APP.read_text(encoding="utf-8")

    assert "useLocation" in source
    assert "useNavigate" in source
    assert "location.pathname === '/launcher'" in source
    assert '<Route path="launcher" element={<DashboardPage />} />' in source
    assert "navigate('/', { replace: true })" in source


def test_frontend_index_serves_branded_favicon_without_404():
    index = FRONTEND_INDEX.read_text(encoding="utf-8")
    icon = FRONTEND_INDEX.parent / "public" / "pyruns.svg"

    assert '<link rel="icon" type="image/svg+xml" href="/pyruns.svg" />' in index
    assert icon.exists()
    assert "<svg" in icon.read_text(encoding="utf-8")


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
    assert "{workspaceWorkingPath || 'Choose a workspace to start'}" in source
    assert 'InfoRow label="Working" value={workspaceWorkingPath || \'--\'} mono' in source
    assert 'InfoRow label="Storage" value={workspaceStoragePath || \'--\'} mono' in source
    assert "splitPathSegments(workspace?.run_root)" not in source


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
    assert "Shell mode active" not in source


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


def test_react_monitor_uses_readable_sidebar_on_narrow_viewports():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "function readCompactMonitorLayout()" in source
    assert "const COMPACT_MONITOR_SIDEBAR_HEIGHT = 260" in source
    assert "window.matchMedia('(max-width: 700px)')" in source
    assert "compactMonitorLayout ? 'flex-col' : 'flex-row'" in source
    assert "compactMonitorLayout ? 'border-b border-border-subtle' : 'border-r border-border-subtle'" in source
    assert "style={compactMonitorLayout ? { height: COMPACT_MONITOR_SIDEBAR_HEIGHT } : { width: `${monitorSidebarWidthPct}%` }}" in source
    assert "{!compactMonitorLayout && (" in source


def test_react_monitor_uses_unfiltered_full_task_list():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "monitorTasks: Task[]" in store
    assert "fetchMonitorTasks: () => Promise<void>" in store
    assert "api.getTasks({ limit: 0, refresh: true, summary: true })" in store
    assert "const { monitorTasks, fetchMonitorTasks } = useTaskStore()" in monitor
    assert "monitorTasks.find(task => task.name === selectedTaskName)" in monitor
    assert "usePolling(fetchMonitorTasks" in monitor
    assert 'title="Pinned Tasks"' in monitor
    assert "count={pinnedTasks.length}" in monitor
    assert 'className="mb-3 rounded-md border border-accent/20 bg-accent/5 p-2"' in monitor


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


def test_react_dashboard_and_generator_avoid_gradient_glow_status_accents():
    surfaces = {
        "DashboardPage.tsx": FRONTEND_DASHBOARD.read_text(encoding="utf-8"),
        "GeneratorPage.tsx": FRONTEND_GENERATOR.read_text(encoding="utf-8"),
    }

    for label, source in surfaces.items():
        assert "bg-gradient" not in source, label
        assert "shadow-[0_0_" not in source, label


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
    assert "window.addEventListener('pointercancel', stopResize, { once: true })" in shell
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
    assert "w-sidebar" not in sidebar


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
    assert '<main className="min-w-0 flex-1 overflow-x-hidden overflow-y-auto">' in shell


def test_react_manager_uses_single_column_cards_on_narrow_viewports():
    manager = FRONTEND_MANAGER.read_text(encoding="utf-8")

    assert "function readCompactTaskGrid()" in manager
    assert "window.matchMedia('(max-width: 700px)')" in manager
    assert "const effectiveTaskColumns = compactTaskGrid ? 1 : columns" in manager
    assert "columns={effectiveTaskColumns}" in manager


def test_react_generator_stacks_editor_and_settings_on_narrow_viewports():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "function readCompactGeneratorLayout()" in generator
    assert "window.matchMedia('(max-width: 700px)')" in generator
    assert "const generatorBodyClassName = clsx(" in generator
    assert "compactGeneratorLayout ? 'flex-col overflow-y-auto' : 'overflow-hidden'" in generator
    assert "compactGeneratorLayout ? 'min-h-[20rem] flex-none' : 'flex-1'" in generator
    assert "compactGeneratorLayout ? 'w-full flex-none border-t border-border-subtle' : 'w-[286px] border-l border-border-subtle'" in generator
    assert "style={compactGeneratorLayout ? undefined : { minWidth: 268, maxWidth: 296 }}" in generator


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
    assert "aria-label={`${actionBtn.label} ${task.name}`}" in manager
    assert "aria-label={`View logs for ${task.name}`}" in manager
    assert "aria-label={`Delete ${task.name}`}" in manager
    assert 'aria-label="Previous page"' in pagination
    assert 'aria-label="Next page"' in pagination


def test_react_task_detail_panel_can_be_resized_from_left_edge():
    source = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")

    assert "TASK_DETAIL_WIDTH_STORAGE_KEY" in source
    assert "clampPanelWidth" in source
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
    assert "function handlePanelBackdropClick" in source
    assert "onClick={handlePanelBackdropClick}" in source
    assert "w-5 -translate-x-2.5" in source
    assert "group-hover:bg-accent/45" in source
    assert "window.innerWidth - 8" in source
    assert "max-w-[calc(100vw-8px)]" in source
    assert "const MAX_PANEL_WIDTH = 2400" in source
    assert "useState(() => buildEnvPairs(task))" in source


def test_react_task_detail_env_rows_keep_stable_keys_while_editing():
    source = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")

    assert "type EnvPair" in source
    assert "id: string" in source
    assert "key={pair.id}" in source
    assert 'key={`${key}-${index}`}' not in source


def test_react_task_detail_env_controls_have_clear_feedback_states():
    source = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")

    assert "type EnvSaveStatus" in source
    assert "getEnvValidationMessage(envPairs)" in source
    assert "const envSaveDisabled = saving || !envDirty || Boolean(envValidationMessage)" in source
    assert "envSaveStatus === 'saved' ? 'Saved' : 'Save'" in source
    assert "envSaveStatus === 'error'" in source
    assert "aria-label=\"Add environment variable\"" in source
    assert "setPendingEnvFocusId(pair.id)" in source
    assert "aria-label={`Remove ${pair.key.trim() || 'environment variable'}`}" in source


def test_react_task_detail_warns_before_discarding_unsaved_edits():
    source = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")

    assert "function requestClose" in source
    assert "notesDirty || envDirty" in source
    assert "window.confirm('Discard unsaved changes?')" in source
    assert "onClick={requestClose}" in source


def test_react_generator_shows_creation_progress_and_result_actions():
    source = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "type GenerationStatus" in source
    assert "generationStatus === 'creating'" in source
    assert "Writing task folders..." in source
    assert "function CreatedTaskSummary" in source
    assert "Open in Manager" in source
    assert "Loader2" in source
    assert "const [success" not in source
    assert "(error || success)" not in source


def test_react_manager_cards_support_drag_pin_and_search_match_labels():
    source = (FRONTEND_COMPONENTS_DIR / "manager" / "ManagerPage.tsx").read_text(encoding="utf-8")

    assert "type DragTarget = 'pinned' | 'tasks'" in source
    assert "const DRAG_START_DISTANCE" in source
    assert "dragCandidateRef" in source
    assert "suppressCardClickRef" in source
    assert "function isInteractiveDragTarget" in source
    assert "onPointerDown" in source
    assert "window.addEventListener('pointermove', handleGlobalPointerMove)" in source
    assert "data-task-drop-target=\"pinned\"" in source
    assert "data-task-drop-target=\"tasks\"" in source
    assert "function getPointerDropIntent" in source
    assert "type DragPlacement = 'before' | 'after'" in source
    assert "api.reorderTasks" in source
    assert "api.getTasks({ limit: 0, refresh: false, summary: true })" in source
    assert "buildReorderedItems" in source
    assert "dragFrameRef" in source
    assert "pendingDragPointRef" in source
    assert "window.requestAnimationFrame(flushDragFrame)" in source
    assert "sameDropIntent" in source
    assert "data-task-grid-columns={columns}" in source
    assert "Number.parseInt(grid?.dataset.taskGridColumns || '1', 10)" in source
    assert "window.getComputedStyle(grid)" not in source
    assert "function DropIndicator" in source


def test_react_task_lists_use_summaries_and_fetch_full_details_on_open():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    manager = (FRONTEND_COMPONENTS_DIR / "manager" / "ManagerPage.tsx").read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "summary?: boolean" in api
    assert "sp.set('summary', String(params.summary))" in api
    assert "api.getTasks({ query, status: statusFilter, offset, limit, summary: true })" in store
    assert "api.getTasks({ limit: 0, refresh: true, summary: true })" in store
    assert "api.getTask(task.name).then(fullTask" in manager
    assert "api.getTask(task.name).then(fullTask" in monitor
    assert "task.search_text || task.preview_text || ''" in manager
    assert "dropIndicator" in manager
    assert "shadow-[0_0_0_3px_rgba(20,184,166,0.16)]" in manager
    assert "scale-[0.985]" in manager
    assert "transition-[border-color,box-shadow,background-color,opacity,transform]" in manager
    assert "data-task-card={task.name}" in manager
    assert "data-task-card-pinned={task.pinned ? 'true' : 'false'}" in manager
    assert "draggable={!selectMode}" not in manager
    assert "getTaskSearchMatches(task, query)" in manager
    assert "Matched in" in manager
    assert "Drop here to pin" in manager
    assert 'title="Pinned Tasks"' in manager
    assert "count={pinnedTasks.length}" in manager
    assert 'className="rounded-md border border-accent/20 bg-accent/5 p-2"' in manager
    assert "const taskKindLabel = task.task_kind === 'shell' ? 'shell' : 'python'" in manager
    assert "task.config_mode" not in manager


def test_react_task_detail_uses_python_shell_task_mode_labels():
    source = FRONTEND_TASK_DETAIL.read_text(encoding="utf-8")

    assert "function isShellTask(task: Task)" in source
    assert "return task.task_kind === 'shell'" in source
    assert "return isShellTask(task) ? 'shell' : 'python'" in source
    assert "task.config_mode" not in source


def test_react_theme_uses_more_readable_base_type_and_muted_text():
    css = FRONTEND_THEME_CSS.read_text(encoding="utf-8")
    tailwind = FRONTEND_TAILWIND.read_text(encoding="utf-8")

    assert "--text-secondary: #4b5563;" in css
    assert "--text-tertiary: #6b7280;" in css
    assert "--text-tertiary: #71717a;" in css
    assert "font-size: 14px;" in css
    assert "'2xs': ['12px', '16px']" in tailwind
    assert "xs: ['13px', '18px']" in tailwind


def test_react_monitor_sidebar_can_be_resized_from_split_handle():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")

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
    assert "cursor-col-resize" in source
    assert "style={compactMonitorLayout ? { height: COMPACT_MONITOR_SIDEBAR_HEIGHT } : { width: `${monitorSidebarWidthPct}%` }}" in source


def test_react_monitor_batches_live_log_chunks_for_stable_progress_rendering():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "LOG_STREAM_FLUSH_MS" in source
    assert "pendingLiveLogChunkRef" in source
    assert "flushLiveLogChunkBuffer" in source
    assert "window.setTimeout(flushLiveLogChunkBuffer, LOG_STREAM_FLUSH_MS)" in source
    assert "appendLog(buffer.content)" in source
    assert "pendingLiveLogChunkRef.current = { key, content: buffer.content + message.content }" in source


def test_react_monitor_caps_live_log_state_for_long_tasks():
    store = FRONTEND_STORE.read_text(encoding="utf-8")
    monitor = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "MONITOR_LOG_STATE_MAX_CHARS" in store
    assert "MONITOR_LOG_STATE_TRIM_THRESHOLD" in store
    assert "export function appendMonitorLogContent" in store
    assert "appendMonitorLogContent(s.logContent, text)" in store
    assert "appendMonitorLogContent(state.logContent, logs.content)" in monitor


def test_react_monitor_memoizes_task_list_derivations_during_log_streaming():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "useMemo" in source
    assert "const selectedTask = useMemo(" in source
    assert "monitorTasks.find(task => task.name === selectedTaskName)" in source
    assert "const hasActive = useMemo(" in source
    assert "const filteredTasks = useMemo(" in source
    assert "const pinnedTasks = useMemo(" in source
    assert "const otherTasks = useMemo(" in source
    assert "const allExportSelected = useMemo(" in source


def test_react_monitor_writes_terminal_deltas_without_full_screen_repaint():
    source = FRONTEND_MONITOR.read_text(encoding="utf-8")

    assert "renderedLogRef" in source
    assert "logContent.startsWith(previous.content)" in source
    assert "const nextChunk = logContent.slice(previous.content.length)" in source
    assert "term.write(nextChunk)" in source
    assert "normalize_log_newlines" not in source


def test_react_search_input_clear_button_is_accessible():
    source = FRONTEND_SEARCH_INPUT.read_text(encoding="utf-8")

    assert "ariaLabel = 'Search'" in source
    assert "aria-label={ariaLabel}" in source
    assert 'aria-label="Clear search"' in source
    assert 'title="Clear search"' in source
    assert "inline-flex h-7 w-7 items-center justify-center" in source
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


def test_react_runtime_panel_stays_compact_and_low_chrome():
    runtime_panel = (FRONTEND_COMPONENTS_DIR / "layout" / "RuntimePanel.tsx").read_text(encoding="utf-8")
    editor = FRONTEND_CODE_EDITOR.read_text(encoding="utf-8")

    assert "w-[620px]" in runtime_panel
    assert "grid-cols-[128px_minmax(0,1fr)]" not in runtime_panel
    assert "border-r border-border-subtle" not in runtime_panel
    assert "inline-flex rounded-md bg-surface-overlay p-0.5" in runtime_panel
    assert "compactToolbar" in runtime_panel
    assert "Save Python Runtime" not in runtime_panel
    assert "Save Workspace Env" not in runtime_panel
    assert "Workspace Env</h3>" not in runtime_panel
    assert "terminal &lt; workspace &lt; task" not in runtime_panel
    assert "Safe .bashrc-style lines" not in runtime_panel
    assert "Saved to this workspace" not in runtime_panel
    assert "Only change this when env discovery fails" not in runtime_panel
    assert "grid grid-cols-3 gap-2" not in runtime_panel
    assert "rounded-md border px-3 py-2.5" not in runtime_panel
    assert "compactToolbar?: boolean" in editor
    assert "{!compactToolbar &&" in editor
    assert "absolute right-1.5 top-1.5" in editor
    assert "<span>{wrap ? 'Wrap' : 'No wrap'}</span>" not in editor
    assert "aria-label={wrap ? 'Disable line wrapping' : 'Enable line wrapping'}" in editor
    assert "lineCount" not in editor
    assert "value.split" not in editor


def test_react_runtime_panel_loads_and_saves_conda_runtime_choices():
    runtime_panel = (FRONTEND_COMPONENTS_DIR / "layout" / "RuntimePanel.tsx").read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "export const getRuntimeInfo = () => request<RuntimeInfo>('/api/runtime')" in api
    assert "export const updateRuntimeInfo" in api
    assert "applyRuntimeState(await api.getRuntimeInfo())" in runtime_panel
    assert "applyRuntimeState(await api.updateRuntimeInfo(payload))" in runtime_panel
    assert "await refreshWorkspace()" in runtime_panel
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


def test_react_launcher_skips_script_scan_for_initial_shell_mode():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "const initialLaunchMode = scriptParam ? 'python' : modeParam === 'shell' ? 'shell' : 'python'" in launcher
    assert "if (initialLaunchMode === 'python')" in launcher
    assert "const handleLaunchModeChange = useCallback((mode: 'python' | 'shell')" in launcher
    assert "if (mode === 'python')" in launcher
    assert "<LaunchChoiceTabs launchMode={launchMode} onChange={handleLaunchModeChange}" in launcher


def test_react_launcher_fetch_does_not_clobber_newer_selection():
    store = FRONTEND_STORE.read_text(encoding="utf-8")

    assert "let launcherRequestSeq = 0" in store
    assert "const requestId = ++launcherRequestSeq" in store
    assert "if (requestId !== launcherRequestSeq)" in store


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

    assert "const handleSelectScript = useCallback(async (scriptPath: string)" in launcher
    assert "setError('')" in launcher
    assert "await selectScript(scriptPath)" in launcher
    assert "const handleSelectConfig = useCallback(async (configPath: string)" in launcher
    assert "selectConfig(configPath)" in launcher
    assert "onClick={() => void handleSelectScript(script.script_path)}" in launcher
    assert "onClick={() => void handleSelectConfig(config.path)}" in launcher


def test_react_launcher_browses_yaml_and_skips_ready_step():
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
    assert "Use Config" not in launcher
    assert "Path to YAML config" in launcher
    assert "Ready to launch" not in launcher
    assert "Open Workspace <ArrowRight" not in launcher


def test_react_launcher_config_step_uses_path_picker_panel():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "function ConfigActionPanel" in launcher
    assert "configPathReady" in launcher
    assert "api.validateLauncherPath('config', debouncedConfigPath, selectedScript)" in launcher
    assert "validation={configValidation}" in launcher
    assert "PathValidationHint validation={validation}" in launcher
    assert "Browse Config" in launcher
    assert "Open Config Path" in launcher


def test_react_generator_nested_form_uses_tree_depth_guides():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "depth={depth + 1}" in generator
    assert "treeSection" in generator
    assert "treeConnector" in generator
    assert "border-l border-dashed border-border-strong/60" in generator
    assert "'ml-4 border-l border-dashed border-border-strong/60 pb-1 pl-4 pt-1'" in generator
    assert "!treeSection && depth > 0 && 'border-l-2 border-border-subtle pl-3'" in generator
    assert "treeSection ? undefined : { paddingLeft: `${Math.min(depth, 5) * 10}px` }" not in generator
    assert "style={undefined}" not in generator
    assert "aria-expanded={open}" in generator
    assert "title={`${prefix} (${Object.keys(data).length} fields)`}" in generator


def test_react_generator_has_tree_layout_and_expand_controls():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "type FormLayoutMode = 'grid' | 'tree'" in generator
    assert "formLayoutMode" in generator
    assert "setFormLayoutMode" in generator
    assert "Grid" in generator
    assert "Tree" in generator
    assert "Expand all" in generator
    assert "Collapse all" in generator
    assert "treeOpenSignal" in generator
    assert "setOpen(openSignalValue)" in generator
    assert "const effectiveColumns = Math.max(1, columns)" in generator
    assert "buildColumnGridStyle(effectiveColumns)" in generator
    assert "repeat(${columns}, minmax(20rem, 1fr))" in generator
    assert "treeColumns" not in generator
    assert "setTreeColumns" not in generator
    assert "TREE_COLS_STORAGE_KEY" not in generator
    assert "Sections per row" not in generator
    assert "TREE_TOP_LEVEL_COLUMN_STYLE" not in generator
    assert "columnWidth" not in generator
    assert "TREE_SECTION_GRID_STYLE" not in generator
    assert "TREE_FIELD_GRID_STYLE" not in generator
    assert "repeat(auto-fit, minmax(360px, 1fr))" not in generator
    assert "repeat(auto-fit, minmax(300px, 1fr))" not in generator
    assert "const contentClassName = layoutMode === 'tree' ? 'space-y-1.5' : 'grid gap-x-3 gap-y-2.5 overflow-x-auto pb-1'" in generator
    assert "const childSectionClassName = layoutMode === 'tree' ? 'w-full' : 'col-span-full'" in generator
    assert "editorMode === 'form' && (formLayoutMode === 'tree' || formLayoutMode === 'grid')" in generator
    assert "layoutMode={formLayoutMode}" in generator
    assert "min-w-[280px]" in generator
    assert "ml-auto flex flex-wrap items-center gap-2" in generator


def test_react_generator_grid_mode_keeps_sibling_fields_on_one_row():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "function buildColumnGridStyle(columns: number)" in generator
    assert "repeat(${columns}, minmax(20rem, 1fr))" in generator
    assert "const effectiveColumns = Math.max(1, columns)" in generator
    assert "const contentClassName = 'grid gap-x-3 gap-y-2.5 overflow-x-auto pb-1'" in generator
    assert "const contentClassName = layoutMode === 'tree' ? 'space-y-1.5' : 'grid gap-x-3 gap-y-2.5 overflow-x-auto pb-1'" in generator
    assert "columns - depth" not in generator
    assert "Math.max(1, columns, itemCount)" not in generator


def test_react_generator_grid_param_rows_keep_label_type_and_input_inline():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "grid min-h-10 grid-cols-[minmax(9rem,0.75fr)_auto_minmax(12rem,1.25fr)]" in generator
    assert "flex flex-none items-center justify-end gap-1" in generator
    assert '<div className="min-w-0 w-full">' in generator
    assert "group flex flex-col justify-between gap-1.5" not in generator
    grid_row_start = generator.index("if (!treeParamRow)")
    name_position = generator.index("title={name}", grid_row_start)
    type_position = generator.index("PARAM_TYPE_STYLES[originalType]", grid_row_start)
    input_position = generator.index('<div className="min-w-0 w-full">', grid_row_start)
    assert name_position < type_position < input_position


def test_react_generator_does_not_backfill_ui_tests_with_comments():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "Keep UI unit tests happy" not in generator


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
    assert "grid-cols-[minmax(220px,260px)_minmax(0,1fr)]" in generator
    assert "grid-cols-[minmax(0,1fr)]" in generator
    assert "columns={1}" in generator
    assert "onClick={() => onSelectPath(section.path)}" in generator
    assert "layoutMode === 'tree' ? TREE_TOP_LEVEL_COLUMN_STYLE : gridStyle" not in generator


def test_react_generator_tree_param_rows_keep_value_inputs_aligned():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "layoutMode?: FormLayoutMode" in generator
    assert "const treeParamRow = layoutMode === 'tree'" in generator
    assert "treeParamRow" in generator
    assert "grid min-h-10 grid-cols-[24px_minmax(150px,0.95fr)_minmax(150px,1.05fr)]" in generator
    assert "border-border-subtle bg-surface-raised" in generator
    assert "treeParamRow ? 'min-w-0' : 'flex-1'" in generator
    assert "treeParamRow ? 'min-w-0 justify-start' : 'flex-none justify-end'" in generator
    assert "treeParamRow ? 'min-w-0 w-full' : 'ml-auto min-w-0 flex-1'" in generator
    assert "if (!treeParamRow)" in generator
    assert "group grid min-h-10 grid-cols-[minmax(9rem,0.75fr)_auto_minmax(12rem,1.25fr)] items-center gap-2 rounded-md border border-border-subtle bg-surface-raised/40 px-2.5 py-1.5 shadow-sm transition-all hover:border-border hover:bg-surface-overlay/30 focus-within:border-accent/60 focus-within:bg-surface-raised focus-within:ring-2 focus-within:ring-accent/15" in generator
    assert "pinned ? 'border-l-2 border-l-accent border-y-accent/20 border-r-accent/20 bg-accent/[0.03] ring-1 ring-accent/20' : ''" in generator
    assert "focus-within:border-accent/60 focus-within:bg-surface-raised focus-within:ring-2 focus-within:ring-accent/15" in generator
    assert "h-7 w-full rounded-md border bg-surface-overlay/45" in generator
    assert "focus:border-accent focus:bg-surface-raised focus:ring-2 focus:ring-accent/15" in generator
    assert "focus-within:border-accent/60 focus-within:bg-surface-raised focus-within:ring-2 focus-within:ring-accent/20" in generator
    assert "focus:border-accent focus:bg-surface-overlay/45 focus:ring-2 focus:ring-accent/15" in generator
    assert "focus-visible:ring-2 focus-visible:ring-accent/30" in generator
    assert "border-border-subtle bg-surface-raised hover:border-border" not in generator
    assert "ml-auto min-w-[180px] max-w-[420px] flex-[1.2]" not in generator
    assert "grid-cols-[24px_minmax(82px,0.55fr)_auto_minmax(112px,1fr)]" not in generator
    assert "max-w-[34%]" not in generator


def test_react_generator_shell_mode_loads_existing_shell_scripts():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")
    api = FRONTEND_API.read_text(encoding="utf-8")

    assert "handlePickShellFile" in generator
    assert "api.pickGeneratorShellFile()" in generator
    assert "Load task or script" in generator
    assert "Browse Shell" in generator
    assert "Shell Workspace" not in generator
    assert "templates.some(template => template.value === selectedTemplate)" in generator
    assert "/api/generator/pick-shell-file" in api


def test_react_generator_template_picker_supports_search():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "function TemplatePicker" in generator
    assert "templateFilter" in generator
    assert "filteredOptions" in generator
    assert "Search templates" in generator
    assert "Search tasks or scripts" in generator
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


def test_react_generator_reloads_default_when_imported_yaml_changes():
    generator = FRONTEND_GENERATOR.read_text(encoding="utf-8")

    assert "lastWorkspaceDefaultKeyRef" in generator
    assert "workspaceDefaultKey" in generator
    assert "workspaceDefaultChanged" in generator
    assert "defaultTemplate = templates.find(template => pathLeaf(template.value) === 'config_default.yaml')" in generator
    assert "workspace?.config_default_source" in generator
    assert "workspace?.config_default_source_name" in generator
    assert "void loadTemplate(defaultTemplateValue)" in generator


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
    assert "nextParams.delete('mode')" in app
    assert "const modeParam = searchParams.get('mode')" in launcher
    assert "const initialLaunchMode = scriptParam ? 'python' : modeParam === 'shell' ? 'shell' : 'python'" in launcher
    assert "setLaunchMode(initialLaunchMode)" in launcher
    assert "if (initialLaunchMode === 'python')" in launcher
    assert "Open Shell Mode" not in sidebar
    assert "Exit Shell Mode" not in sidebar
    assert "openShellWorkspace" not in sidebar
    assert "exitShellWorkspace" not in sidebar


def test_react_sidebar_routes_load_scripts_to_yaml_selection_without_red_error():
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "nextParams.set('launcher', '1')" in sidebar
    assert "nextParams.set('mode', mode)" in sidebar
    assert "pyruns.load()" not in sidebar
    assert "bg-rose-500/10" not in sidebar


def test_react_sidebar_uses_launcher_instead_of_inline_picker_errors():
    sidebar = FRONTEND_SIDEBAR.read_text(encoding="utf-8")

    assert "pickerError" not in sidebar
    assert "showPickerError" not in sidebar
    assert "No script selected." not in sidebar
    assert "No directory selected." not in sidebar
    assert "title={pickerError}" not in sidebar


def test_react_launcher_config_step_explains_required_yaml_selection():
    launcher = FRONTEND_LAUNCHER.read_text(encoding="utf-8")

    assert "requiresConfigTemplate" in launcher
    assert "Choose a YAML config" in launcher
    assert "This script needs a YAML config before first launch." in launcher
    assert "pyruns will save it as config_default.yaml" in launcher
    assert "Choose or enter a YAML config path first." in launcher
    assert "Path to YAML config" in launcher
