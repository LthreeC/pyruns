import { create } from 'zustand'
import type {
  ConfigCandidate,
  Dashboard,
  GeneratorMode,
  RuntimeInfo,
  ScriptCandidate,
  Task,
  TaskSortMode,
  TaskStatusCounts,
  TemplateContent,
  WorkspaceInfo,
} from './types'
import * as api from './api'
import { resolveMonitorScrollback } from './utils/monitorSettings'

let taskRequestSeq = 0
let monitorTaskRequestSeq = 0
let monitorRequestSeq = 0
let launcherRequestSeq = 0
let runtimeRequestSeq = 0
let dashboardRequestSeq = 0
let generatorTemplateRequestSeq = 0
let generatorDraftVersion = 0
let workspaceRequestSeq = 0
let launcherOpenPromise: Promise<boolean> | null = null
let launcherOpenToken: symbol | null = null
let toastIdSeq = 0
let confirmationIdSeq = 0
let confirmationResolver: ((confirmed: boolean) => void) | null = null
const THEME_STORAGE_KEY = 'pyruns_theme'
const MANAGER_COLS_STORAGE_KEY = 'pyruns_manager_cols'
const MANAGER_SORT_STORAGE_KEY = 'pyruns_manager_sort'
const GENERATOR_COLS_STORAGE_KEY = 'pyruns_generator_cols'
const PINNED_PARAMS_STORAGE_KEY = 'pyruns_pinned_params'
const MONITOR_TASK_PAGE_SIZE = 200
const MAX_MONITOR_LOG_CHARS = 4 * 1024 * 1024
const TASK_SORT_MODES = new Set<TaskSortMode>([
  'priority',
  'manual',
  'activity_desc',
  'activity_asc',
  'name_asc',
  'name_desc',
])

function readLocalStorage(key: string) {
  if (typeof window === 'undefined') {
    return null
  }
  try {
    return window.localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeLocalStorage(key: string, value: string) {
  if (typeof window === 'undefined') {
    return
  }
  try {
    window.localStorage.setItem(key, value)
  } catch {
    // Preferences remain active for this session when browser storage is unavailable.
  }
}

interface TaskDetailDraftState {
  dirty: boolean
  taskName: string
  setDirty: (taskName: string, dirty: boolean) => void
  clear: (taskName?: string) => void
}

export const useTaskDetailDraftStore = create<TaskDetailDraftState>((set, get) => ({
  dirty: false,
  taskName: '',
  setDirty(taskName, dirty) {
    if (dirty) {
      set({ dirty: true, taskName })
      return
    }
    if (!get().taskName || get().taskName === taskName) {
      set({ dirty: false, taskName: '' })
    }
  },
  clear(taskName) {
    if (!taskName || !get().taskName || get().taskName === taskName) {
      set({ dirty: false, taskName: '' })
    }
  },
}))

function currentWorkspaceKey() {
  return String(useWorkspaceStore.getState().workspace?.run_root || '')
}

function resetWorkspaceScopedState(nextWorkspaceKey: string) {
  taskRequestSeq += 1
  monitorTaskRequestSeq += 1
  monitorRequestSeq += 1
  runtimeRequestSeq += 1
  dashboardRequestSeq += 1
  generatorTemplateRequestSeq += 1
  generatorDraftVersion += 1
  useTaskStore.setState({
    tasks: [],
    total: 0,
    statusCounts: null,
    offset: 0,
    hasMore: false,
    query: '',
    statusFilter: 'All',
    selectedIds: new Set(),
    loading: false,
    error: null,
    monitorWorkspaceKey: nextWorkspaceKey,
    monitorTasks: [],
    monitorTotal: 0,
    monitorHasMore: false,
    monitorLoadedLimit: MONITOR_TASK_PAGE_SIZE,
    monitorQuery: '',
    monitorLoading: false,
    monitorError: '',
    monitorStatusCounts: null,
  })
  useMonitorStore.setState({
    workspaceKey: nextWorkspaceKey,
    selectedTaskName: null,
    logContent: '',
    logOffset: 0,
    logIdentity: '',
    availableLogs: [],
    selectedLog: '',
    logTailTruncated: false,
    logTailLimitBytes: 0,
    loading: false,
    exportIds: new Set(),
  })
  useDashboardStore.setState({ data: null, loading: false, error: null })
  useGeneratorStore.setState({
    templates: [],
    selectedTemplate: '',
    templateContent: null,
    viewMode: 'form',
    yamlText: '',
    shellText: '',
    namePrefix: 'task',
    appendTimestamp: true,
    dirty: false,
    loading: false,
  })
  useTaskDetailDraftStore.setState({ dirty: false, taskName: '' })
  useRuntimeStore.setState({ runtime: null, loading: false, dirty: false })
}

interface ThemeState {
  theme: 'dark' | 'light'
  toggle: () => void
}

function resolveInitialTheme(): 'dark' | 'light' {
  return readLocalStorage(THEME_STORAGE_KEY) === 'dark' ? 'dark' : 'light'
}

function clampInteger(value: number, fallback: number, min: number, max: number) {
  const normalized = Number.isFinite(value) ? Math.trunc(value) : fallback
  return Math.min(max, Math.max(min, normalized))
}

function readStoredNumber(key: string, fallback: number, min: number, max: number) {
  const raw = readLocalStorage(key)
  if (!raw) {
    return fallback
  }
  const parsed = Number.parseInt(raw, 10)
  return clampInteger(parsed, fallback, min, max)
}

function readStoredTaskSortMode(): TaskSortMode {
  const value = readLocalStorage(MANAGER_SORT_STORAGE_KEY) as TaskSortMode | null
  return value && TASK_SORT_MODES.has(value) ? value : 'priority'
}

function readStoredStringArray(key: string) {
  if (typeof window === 'undefined') {
    return [] as string[]
  }
  try {
    const raw = readLocalStorage(key)
    if (!raw) {
      return []
    }
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : []
  } catch {
    return []
  }
}

export function trimMonitorLogContent(content: string, maxLines = currentMonitorScrollback()) {
  const lineLimit = Math.max(0, Math.trunc(maxLines))
  if (!content || lineLimit === 0) {
    return ''
  }

  let keptLines = content.endsWith('\n') ? 0 : 1
  for (let index = content.length - 1; index >= 0; index -= 1) {
    if (content.charCodeAt(index) !== 10) {
      continue
    }
    keptLines += 1
    if (keptLines > lineLimit) {
      const lineTrimmed = content.slice(index + 1)
      return lineTrimmed.length > MAX_MONITOR_LOG_CHARS
        ? lineTrimmed.slice(-MAX_MONITOR_LOG_CHARS)
        : lineTrimmed
    }
  }

  return content.length > MAX_MONITOR_LOG_CHARS
    ? content.slice(-MAX_MONITOR_LOG_CHARS)
    : content
}

function comparableLogText(text: string) {
  return text.replace(/\r\n/g, '\n')
}

function isPyrunsLifecycleChunk(text: string) {
  return text.includes('[PYRUNS]')
    && (text.includes(' START ') || text.includes(' FINISH ') || text.includes('[PYRUNS] Source '))
}

export function appendMonitorLogContent(content: string, text: string) {
  if (!text) {
    return content
  }
  if (isPyrunsLifecycleChunk(text)) {
    const contentTail = content.slice(Math.max(0, content.length - text.length - 32))
    if (comparableLogText(contentTail).endsWith(comparableLogText(text))) {
      return content
    }
  }
  return trimMonitorLogContent(content + text)
}

export function applyThemeClass(theme: 'dark' | 'light') {
  if (typeof document === 'undefined') {
    return
  }
  document.documentElement.classList.remove('light', 'dark')
  document.documentElement.classList.add(theme)
}

const initialTheme = resolveInitialTheme()
applyThemeClass(initialTheme)

export const useThemeStore = create<ThemeState>((set, get) => ({
  theme: initialTheme,
  toggle() {
    const next = get().theme === 'dark' ? 'light' : 'dark'
    writeLocalStorage(THEME_STORAGE_KEY, next)
    applyThemeClass(next)
    set({ theme: next })
  },
}))

export class WorkspaceChangeRequiresDiscardError extends Error {
  readonly unsavedChanges: string[]

  constructor(unsavedChanges: string[]) {
    super('The server selected another workspace while local changes are unsaved.')
    this.name = 'WorkspaceChangeRequiresDiscardError'
    this.unsavedChanges = [...unsavedChanges]
  }
}

interface WorkspaceState {
  workspace: WorkspaceInfo | null
  workspaceEpoch: number
  lastScriptWorkspace: WorkspaceInfo | null
  loading: boolean
  fetch: (options?: { discardUnsavedChanges?: boolean }) => Promise<void>
  setWorkspace: (workspace: WorkspaceInfo | null) => void
  setRunRoot: (path: string) => Promise<void>
  openShellWorkspace: () => Promise<void>
  exitShellWorkspace: () => Promise<WorkspaceInfo | null>
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  workspace: null,
  workspaceEpoch: 0,
  lastScriptWorkspace: null,
  loading: false,
  async fetch(options = {}) {
    const requestId = ++workspaceRequestSeq
    set({ loading: true })
    try {
      const ws = await api.getWorkspace()
      if (requestId !== workspaceRequestSeq) {
        return
      }
      const previousRunRoot = get().workspace?.run_root
      const nextRunRoot = ws?.run_root
      const unsavedChanges = getUnsavedWorkspaceChangeLabels()
      if (
        previousRunRoot !== nextRunRoot
        && unsavedChanges.length > 0
        && !options.discardUnsavedChanges
      ) {
        throw new WorkspaceChangeRequiresDiscardError(unsavedChanges)
      }
      set(state => ({
        workspace: ws,
        workspaceEpoch: state.workspaceEpoch + (previousRunRoot !== nextRunRoot ? 1 : 0),
        lastScriptWorkspace: ws?.workspace_kind === 'script' ? ws : state.lastScriptWorkspace,
      }))
      if (previousRunRoot !== nextRunRoot) {
        resetWorkspaceScopedState(String(nextRunRoot || ''))
      }
    } finally {
      if (requestId === workspaceRequestSeq) {
        set({ loading: false })
      }
    }
  },
  setWorkspace(workspace) {
    workspaceRequestSeq += 1
    const previousRunRoot = get().workspace?.run_root
    set(state => ({
      workspace,
      loading: false,
      workspaceEpoch: state.workspaceEpoch + (previousRunRoot !== workspace?.run_root ? 1 : 0),
      lastScriptWorkspace: workspace?.workspace_kind === 'script' ? workspace : state.lastScriptWorkspace,
    }))
    if (previousRunRoot !== workspace?.run_root) {
      resetWorkspaceScopedState(String(workspace?.run_root || ''))
    }
  },
  async setRunRoot(path: string) {
    const requestId = ++workspaceRequestSeq
    const ws = await api.setRunRoot(path)
    if (requestId !== workspaceRequestSeq) {
      return
    }
    const previousRunRoot = get().workspace?.run_root
    set(state => ({
      workspace: ws,
      workspaceEpoch: state.workspaceEpoch + (previousRunRoot !== ws.run_root ? 1 : 0),
      lastScriptWorkspace: ws?.workspace_kind === 'script' ? ws : state.lastScriptWorkspace,
    }))
    if (previousRunRoot !== ws.run_root) {
      resetWorkspaceScopedState(String(ws.run_root || ''))
    }
  },
  async openShellWorkspace() {
    const requestId = ++workspaceRequestSeq
    const ws = await api.openShellWorkspace()
    if (requestId !== workspaceRequestSeq) {
      return
    }
    const previousRunRoot = get().workspace?.run_root
    set(state => ({
      workspace: ws,
      workspaceEpoch: state.workspaceEpoch + (previousRunRoot !== ws.run_root ? 1 : 0),
      lastScriptWorkspace: state.lastScriptWorkspace,
    }))
    if (previousRunRoot !== ws.run_root) {
      resetWorkspaceScopedState(String(ws.run_root || ''))
    }
  },
  async exitShellWorkspace() {
    const nextWorkspace = get().lastScriptWorkspace
    if (!nextWorkspace?.run_root) {
      return null
    }

    const requestId = ++workspaceRequestSeq
    const ws = await api.setRunRoot(nextWorkspace.run_root)
    if (requestId !== workspaceRequestSeq) {
      return null
    }
    const previousRunRoot = get().workspace?.run_root
    set(state => ({
      workspace: ws,
      workspaceEpoch: state.workspaceEpoch + (previousRunRoot !== ws.run_root ? 1 : 0),
      lastScriptWorkspace: ws,
    }))
    if (previousRunRoot !== ws.run_root) {
      resetWorkspaceScopedState(String(ws.run_root || ''))
    }
    return ws
  },
}))

interface RuntimeState {
  runtime: RuntimeInfo | null
  loading: boolean
  dirty: boolean
  setRuntime: (runtime: RuntimeInfo | null) => void
  setDirty: (dirty: boolean) => void
  fetchRuntime: () => Promise<RuntimeInfo>
  updateRuntime: (
    payload: Parameters<typeof api.updateRuntimeInfo>[0],
    refreshProviders?: boolean,
  ) => Promise<RuntimeInfo>
}

export const useRuntimeStore = create<RuntimeState>((set) => ({
  runtime: null,
  loading: false,
  dirty: false,
  setRuntime(runtime) {
    runtimeRequestSeq += 1
    set({ runtime, loading: false, dirty: false })
  },
  setDirty(dirty) { set({ dirty }) },
  async fetchRuntime() {
    const requestId = ++runtimeRequestSeq
    const workspaceKey = currentWorkspaceKey()
    set({ loading: true })
    try {
      const runtime = await api.getRuntimeInfo()
      if (requestId === runtimeRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ runtime })
      }
      return runtime
    } finally {
      if (requestId === runtimeRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ loading: false })
      }
    }
  },
  async updateRuntime(payload, refreshProviders = false) {
    const requestId = ++runtimeRequestSeq
    const workspaceKey = currentWorkspaceKey()
    set({ loading: true })
    try {
      const runtime = await api.updateRuntimeInfo(payload, refreshProviders)
      if (requestId === runtimeRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ runtime })
      }
      return runtime
    } finally {
      if (requestId === runtimeRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ loading: false })
      }
    }
  },
}))

export type ToastTone = 'success' | 'error' | 'info'

export interface ToastItem {
  id: number
  tone: ToastTone
  title: string
  detail?: string
}

interface ToastState {
  toasts: ToastItem[]
  notify: (toast: Omit<ToastItem, 'id'>) => number
  dismiss: (id: number) => void
  clear: () => void
}

export const useToastStore = create<ToastState>((set) => ({
  toasts: [],
  notify(toast) {
    toastIdSeq += 1
    const id = toastIdSeq
    set(state => ({
      toasts: [
        { id, ...toast },
        ...state.toasts.filter(item => item.title !== toast.title || item.detail !== toast.detail),
      ].slice(0, 4),
    }))
    return id
  },
  dismiss(id) {
    set(state => ({ toasts: state.toasts.filter(toast => toast.id !== id) }))
  },
  clear() {
    set({ toasts: [] })
  },
}))

export interface ConfirmationOptions {
  title: string
  description?: string
  confirmLabel?: string
  confirmVariant?: 'danger' | 'primary'
}

export interface ConfirmationRequest extends ConfirmationOptions {
  id: number
}

interface ConfirmationState {
  request: ConfirmationRequest | null
  ask: (options: ConfirmationOptions) => Promise<boolean>
  respond: (confirmed: boolean) => void
}

export const useConfirmationStore = create<ConfirmationState>((set) => ({
  request: null,
  ask(options) {
    confirmationResolver?.(false)
    confirmationIdSeq += 1
    return new Promise<boolean>(resolve => {
      confirmationResolver = resolve
      set({ request: { id: confirmationIdSeq, ...options } })
    })
  },
  respond(confirmed) {
    const resolve = confirmationResolver
    confirmationResolver = null
    set({ request: null })
    resolve?.(confirmed)
  },
}))

export function requestConfirmation(options: ConfirmationOptions) {
  return useConfirmationStore.getState().ask(options)
}

function currentMonitorScrollback() {
  return resolveMonitorScrollback(useWorkspaceStore.getState().workspace?.settings)
}

interface TaskState {
  tasks: Task[]
  monitorWorkspaceKey: string
  monitorTasks: Task[]
  monitorTotal: number
  monitorHasMore: boolean
  monitorLoadedLimit: number
  monitorQuery: string
  monitorLoading: boolean
  monitorError: string
  monitorStatusCounts: TaskStatusCounts | null
  total: number
  statusCounts: TaskStatusCounts | null
  offset: number
  limit: number
  hasMore: boolean
  query: string
  statusFilter: string
  sortMode: TaskSortMode
  selectedIds: Set<string>
  loading: boolean
  error: string | null
  columns: number
  setQuery: (q: string) => void
  setStatusFilter: (s: string) => void
  setSortMode: (mode: TaskSortMode) => void
  setOffset: (o: number) => void
  setColumns: (n: number) => void
  fetchTasks: () => Promise<void>
  fetchMonitorTasks: (options?: {
    query?: string
    loadMore?: boolean
    refresh?: boolean
    background?: boolean
    workspaceKey?: string
  }) => Promise<void>
  upsertMonitorTask: (task: Task) => void
  toggleSelect: (name: string) => void
  selectAll: () => void
  clearSelection: () => void
  setSelectedIds: (ids: Set<string>) => void
}

export const useTaskStore = create<TaskState>((set, get) => ({
  tasks: [],
  monitorWorkspaceKey: '',
  monitorTasks: [],
  monitorTotal: 0,
  monitorHasMore: false,
  monitorLoadedLimit: MONITOR_TASK_PAGE_SIZE,
  monitorQuery: '',
  monitorLoading: false,
  monitorError: '',
  monitorStatusCounts: null,
  total: 0,
  statusCounts: null,
  offset: 0,
  limit: 50,
  hasMore: false,
  query: '',
  statusFilter: 'All',
  sortMode: readStoredTaskSortMode(),
  selectedIds: new Set(),
  loading: false,
  error: null,
  columns: readStoredNumber(MANAGER_COLS_STORAGE_KEY, 5, 1, 8),
  setQuery(q) {
    if (q !== get().query) {
      taskRequestSeq += 1
      set({ query: q, offset: 0, selectedIds: new Set() })
    }
  },
  setStatusFilter(s) {
    if (s !== get().statusFilter) {
      taskRequestSeq += 1
      set({ statusFilter: s, offset: 0, selectedIds: new Set() })
    }
  },
  setSortMode(sortMode) {
    if (sortMode !== get().sortMode) {
      taskRequestSeq += 1
      writeLocalStorage(MANAGER_SORT_STORAGE_KEY, sortMode)
      set({ sortMode, offset: 0, selectedIds: new Set() })
    }
  },
  setOffset(o) {
    if (o !== get().offset) {
      taskRequestSeq += 1
      set({ offset: o, selectedIds: new Set() })
    }
  },
  setColumns(n) {
    const next = clampInteger(n, 5, 1, 8)
    writeLocalStorage(MANAGER_COLS_STORAGE_KEY, String(next))
    set({ columns: next })
  },
  async fetchTasks() {
    const requestId = ++taskRequestSeq
    const workspaceKey = currentWorkspaceKey()
    const { query, statusFilter, sortMode, offset, limit } = get()
    let requestedOffset = offset
    const isCurrentRequest = () => {
      const current = get()
      return requestId === taskRequestSeq
        && workspaceKey === currentWorkspaceKey()
        && current.query === query
        && current.statusFilter === statusFilter
        && current.sortMode === sortMode
        && current.offset === requestedOffset
        && current.limit === limit
    }
    set({ loading: true, error: null })
    try {
      const page = await api.getTasks({ query, status: statusFilter, sort: sortMode, offset, limit, summary: true })
      if (!isCurrentRequest()) {
        return
      }
      if (limit > 0 && offset > 0 && page.items.length === 0) {
        const nextOffset = page.total > 0
          ? Math.max(0, Math.floor((page.total - 1) / limit) * limit)
          : 0
        if (nextOffset !== offset && offset >= page.total) {
          requestedOffset = nextOffset
          set({ offset: nextOffset })
          const retryPage = await api.getTasks({
            query,
            status: statusFilter,
            sort: sortMode,
            offset: nextOffset,
            limit,
            summary: true,
          })
          if (!isCurrentRequest()) {
            return
          }
          set({
            tasks: retryPage.items,
            total: retryPage.total,
            statusCounts: retryPage.status_counts ?? null,
            hasMore: retryPage.has_more,
            offset: nextOffset,
            selectedIds: new Set(),
          })
          return
        }
      }
      if (page.total === 0 && offset !== 0) {
        requestedOffset = 0
        set({
          tasks: page.items,
          total: page.total,
          statusCounts: page.status_counts ?? null,
          hasMore: page.has_more,
          offset: 0,
          selectedIds: new Set(),
        })
        return
      }
      const visibleNames = new Set(page.items.map(task => task.name))
      const visibleSelection = new Set(
        [...get().selectedIds].filter(name => visibleNames.has(name)),
      )
      set({
        tasks: page.items,
        total: page.total,
        statusCounts: page.status_counts ?? null,
        hasMore: page.has_more,
        selectedIds: visibleSelection,
      })
    } catch (err) {
      if (isCurrentRequest()) {
        set({ error: err instanceof Error ? err.message : 'Could not load tasks' })
      }
    } finally {
      if (isCurrentRequest()) {
        set({ loading: false })
      }
    }
  },
  async fetchMonitorTasks(options = {}) {
    const requestId = ++monitorTaskRequestSeq
    const current = get()
    const workspaceKey = String(options.workspaceKey ?? currentWorkspaceKey())
    if (workspaceKey !== currentWorkspaceKey()) {
      return
    }
    const query = String(options.query ?? current.monitorQuery).trim()
    const queryChanged = query !== current.monitorQuery
    const background = Boolean(options.background)
    const baseLimit = queryChanged ? MONITOR_TASK_PAGE_SIZE : current.monitorLoadedLimit
    const nextLimit = options.loadMore && !queryChanged
      ? baseLimit + MONITOR_TASK_PAGE_SIZE
      : baseLimit
    set(background
      ? { monitorQuery: query }
      : { monitorLoading: true, monitorError: '', monitorQuery: query })
    try {
      const page = await api.getTasks({
        query,
        limit: nextLimit,
        refresh: options.refresh ?? true,
        summary: true,
        compact: true,
      })
      if (
        requestId !== monitorTaskRequestSeq
        || workspaceKey !== currentWorkspaceKey()
        || get().monitorWorkspaceKey !== workspaceKey
      ) {
        return
      }
      set({
        monitorTasks: page.items,
        monitorTotal: page.total,
        monitorHasMore: page.has_more,
        monitorLoadedLimit: nextLimit,
        monitorStatusCounts: page.status_counts ?? null,
        monitorError: '',
      })
    } catch (error) {
      if (
        requestId !== monitorTaskRequestSeq
        || workspaceKey !== currentWorkspaceKey()
        || get().monitorWorkspaceKey !== workspaceKey
      ) {
        return
      }
      set({ monitorError: error instanceof Error ? error.message : String(error) })
      throw error
    } finally {
      if (!background && (
        requestId === monitorTaskRequestSeq
        && workspaceKey === currentWorkspaceKey()
        && get().monitorWorkspaceKey === workspaceKey
      )) {
        set({ monitorLoading: false })
      }
    }
  },
  upsertMonitorTask(task) {
    if (!task?.name) {
      return
    }
    set(state => {
      const exists = state.monitorTasks.some(item => item.name === task.name)
      return {
        monitorTasks: exists
          ? state.monitorTasks.map(item => item.name === task.name ? task : item)
          : state.monitorQuery
            ? state.monitorTasks
            : [task, ...state.monitorTasks],
      }
    })
  },
  toggleSelect(name) {
    const ids = new Set(get().selectedIds)
    if (ids.has(name)) ids.delete(name); else ids.add(name)
    set({ selectedIds: ids })
  },
  selectAll() {
    set({ selectedIds: new Set(get().tasks.map(t => t.name)) })
  },
  clearSelection() { set({ selectedIds: new Set() }) },
  setSelectedIds(ids) { set({ selectedIds: ids }) },
}))

interface DashboardState {
  data: Dashboard | null
  loading: boolean
  error: string | null
  fetch: () => Promise<void>
}

export const useDashboardStore = create<DashboardState>((set) => ({
  data: null,
  loading: false,
  error: null,
  async fetch() {
    const requestId = ++dashboardRequestSeq
    const workspaceKey = currentWorkspaceKey()
    set({ loading: true, error: null })
    try {
      const d = await api.getDashboard()
      if (requestId === dashboardRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ data: d, error: null })
      }
    } catch (err) {
      if (requestId === dashboardRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ error: err instanceof Error ? err.message : 'Could not load dashboard' })
      }
      throw err
    } finally {
      if (requestId === dashboardRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ loading: false })
      }
    }
  },
}))

interface GeneratorState {
  templates: { value: string; label: string }[]
  selectedTemplate: string
  templateContent: TemplateContent | null
  viewMode: GeneratorMode
  yamlText: string
  shellText: string
  namePrefix: string
  appendTimestamp: boolean
  columns: number
  pinnedParams: string[]
  dirty: boolean
  loading: boolean
  fetchTemplates: () => Promise<void>
  loadTemplate: (value: string) => Promise<void>
  clearTemplate: () => void
  setViewMode: (m: GeneratorMode) => void
  setYamlText: (t: string) => void
  setShellText: (t: string) => void
  setNamePrefix: (n: string) => void
  setAppendTimestamp: (b: boolean) => void
  setDirty: (dirty: boolean) => void
  setColumns: (n: number) => void
  togglePin: (key: string) => void
}

export const useGeneratorStore = create<GeneratorState>((set, get) => ({
  templates: [],
  selectedTemplate: '',
  templateContent: null,
  viewMode: 'form',
  yamlText: '',
  shellText: '',
  namePrefix: 'task',
  appendTimestamp: true,
  columns: readStoredNumber(GENERATOR_COLS_STORAGE_KEY, 5, 2, 8),
  pinnedParams: readStoredStringArray(PINNED_PARAMS_STORAGE_KEY),
  dirty: false,
  loading: false,
  async fetchTemplates() {
    const requestId = ++generatorTemplateRequestSeq
    const workspaceKey = currentWorkspaceKey()
    const res = await api.getTemplates()
    if (requestId === generatorTemplateRequestSeq && workspaceKey === currentWorkspaceKey()) {
      set({ templates: res.items })
    }
  },
  async loadTemplate(value: string) {
    const requestId = ++generatorTemplateRequestSeq
    const draftVersion = generatorDraftVersion
    const workspaceKey = currentWorkspaceKey()
    if (!value) {
      set({ selectedTemplate: '', templateContent: null, yamlText: '', shellText: '', dirty: false, loading: false })
      return
    }
    set({ loading: true })
    try {
      const content = await api.getTemplateContent(value)
      if (
        requestId !== generatorTemplateRequestSeq
        || draftVersion !== generatorDraftVersion
        || workspaceKey !== currentWorkspaceKey()
      ) {
        return
      }
      set({
        selectedTemplate: value,
        templateContent: content,
        yamlText: content.content,
        shellText: content.mode_hint === 'shell' ? content.content : get().shellText,
        viewMode: content.mode_hint === 'shell' ? 'shell' : get().viewMode === 'shell' ? 'yaml' : get().viewMode,
        dirty: false,
      })
    } finally {
      if (requestId === generatorTemplateRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ loading: false })
      }
    }
  },
  clearTemplate() {
    generatorTemplateRequestSeq += 1
    generatorDraftVersion += 1
    set({ selectedTemplate: '', templateContent: null, yamlText: '', shellText: '', dirty: false, loading: false })
  },
  setViewMode(m) { set({ viewMode: m }) },
  setYamlText(t) {
    if (t === get().yamlText) return
    generatorDraftVersion += 1
    set({ yamlText: t, dirty: true })
  },
  setShellText(t) {
    if (t === get().shellText) return
    generatorDraftVersion += 1
    set({ shellText: t, dirty: true })
  },
  setNamePrefix(n) {
    if (n === get().namePrefix) return
    generatorDraftVersion += 1
    set({ namePrefix: n, dirty: true })
  },
  setAppendTimestamp(b) {
    if (b === get().appendTimestamp) return
    generatorDraftVersion += 1
    set({ appendTimestamp: b, dirty: true })
  },
  setDirty(dirty) { set({ dirty }) },
  setColumns(n) {
    const next = clampInteger(n, 5, 2, 8)
    writeLocalStorage(GENERATOR_COLS_STORAGE_KEY, String(next))
    set({ columns: next })
  },
  togglePin(key) {
    const pins = [...get().pinnedParams]
    const idx = pins.indexOf(key)
    if (idx >= 0) pins.splice(idx, 1); else pins.push(key)
    writeLocalStorage(PINNED_PARAMS_STORAGE_KEY, JSON.stringify(pins))
    set({ pinnedParams: pins })
  },
}))

export function getUnsavedWorkspaceChangeLabels() {
  const labels: string[] = []
  if (useRuntimeStore.getState().dirty) labels.push('runtime settings')
  if (useGeneratorStore.getState().dirty) labels.push('generator draft')
  if (useTaskDetailDraftStore.getState().dirty) labels.push('task details')
  return labels
}

export async function confirmDiscardWorkspaceChanges() {
  const labels = getUnsavedWorkspaceChangeLabels()
  if (labels.length === 0) {
    return true
  }
  const description = labels.length === 1
    ? labels[0]
    : `${labels.slice(0, -1).join(', ')} and ${labels[labels.length - 1]}`
  return requestConfirmation({
    title: 'Discard unsaved changes?',
    description: `Discard unsaved ${description} before switching workspaces?`,
    confirmLabel: 'Discard and Switch',
    confirmVariant: 'danger',
  })
}

interface MonitorState {
  workspaceKey: string
  selectedTaskName: string | null
  logContent: string
  logOffset: number
  logIdentity: string
  availableLogs: string[]
  selectedLog: string
  logTailTruncated: boolean
  logTailLimitBytes: number
  loading: boolean
  exportIds: Set<string>
  selectTask: (name: string) => Promise<void>
  selectLogFile: (name: string) => Promise<void>
  appendLog: (text: string) => void
  clearLog: () => void
  toggleExport: (name: string) => void
  selectAllExport: (names: string[]) => void
  clearExport: () => void
}

export const useMonitorStore = create<MonitorState>((set, get) => ({
  workspaceKey: '',
  selectedTaskName: null,
  logContent: '',
  logOffset: 0,
  logIdentity: '',
  availableLogs: [],
  selectedLog: '',
  logTailTruncated: false,
  logTailLimitBytes: 0,
  loading: false,
  exportIds: new Set(),
  async selectTask(name: string) {
    const requestId = ++monitorRequestSeq
    const workspaceKey = currentWorkspaceKey()
    if (get().workspaceKey !== workspaceKey) {
      return
    }
    set({
      selectedTaskName: name,
      logContent: '',
      logOffset: 0,
      logIdentity: '',
      availableLogs: [],
      selectedLog: '',
      logTailTruncated: false,
      logTailLimitBytes: 0,
      loading: true,
    })
    try {
      const logs = await api.getTaskLogs(name, { tailLines: currentMonitorScrollback() })
      if (
        requestId !== monitorRequestSeq
        || get().selectedTaskName !== name
        || get().workspaceKey !== workspaceKey
        || currentWorkspaceKey() !== workspaceKey
      ) {
        return
      }
      set({
        logContent: logs.content,
        logOffset: logs.offset,
        logIdentity: String(logs.log_identity || ''),
        availableLogs: logs.available_logs,
        selectedLog: logs.selected_log,
        logTailTruncated: Boolean(logs.tail_truncated),
        logTailLimitBytes: Number(logs.tail_limit_bytes || 0),
      })
    } finally {
      if (
        requestId === monitorRequestSeq
        && get().workspaceKey === workspaceKey
        && currentWorkspaceKey() === workspaceKey
      ) {
        set({ loading: false })
      }
    }
  },
  async selectLogFile(logName: string) {
    const { selectedTaskName } = get()
    if (!selectedTaskName) return
    const requestId = ++monitorRequestSeq
    const workspaceKey = currentWorkspaceKey()
    if (get().workspaceKey !== workspaceKey) {
      return
    }
    set({
      selectedLog: logName,
      logContent: '',
      logOffset: 0,
      logIdentity: '',
      logTailTruncated: false,
      logTailLimitBytes: 0,
      loading: true,
    })
    try {
      const logs = await api.getTaskLogs(selectedTaskName, {
        logFileName: logName,
        tailLines: currentMonitorScrollback(),
      })
      if (
        requestId !== monitorRequestSeq
        || get().selectedTaskName !== selectedTaskName
        || get().selectedLog !== logName
        || get().workspaceKey !== workspaceKey
        || currentWorkspaceKey() !== workspaceKey
      ) {
        return
      }
      set({
        logContent: logs.content,
        logOffset: logs.offset,
        logIdentity: String(logs.log_identity || ''),
        availableLogs: logs.available_logs,
        selectedLog: logs.selected_log,
        logTailTruncated: Boolean(logs.tail_truncated),
        logTailLimitBytes: Number(logs.tail_limit_bytes || 0),
      })
    } finally {
      if (
        requestId === monitorRequestSeq
        && get().workspaceKey === workspaceKey
        && currentWorkspaceKey() === workspaceKey
      ) {
        set({ loading: false })
      }
    }
  },
  appendLog(text: string) {
    set(s => ({ logContent: appendMonitorLogContent(s.logContent, text) }))
  },
  clearLog() {
    set({ logContent: '', logOffset: 0, logIdentity: '', logTailTruncated: false, logTailLimitBytes: 0 })
  },
  toggleExport(name) {
    const ids = new Set(get().exportIds)
    if (ids.has(name)) ids.delete(name); else ids.add(name)
    set({ exportIds: ids })
  },
  selectAllExport(names) {
    set({ exportIds: new Set(names) })
  },
  clearExport() { set({ exportIds: new Set() }) },
}))

interface LauncherState {
  scripts: ScriptCandidate[]
  configs: ConfigCandidate[]
  selectedScript: string
  selectedConfig: string
  requiresConfigTemplate: boolean
  configSource: string
  step: number
  loading: boolean
  fetchScripts: () => Promise<void>
  selectScript: (path: string) => Promise<void>
  selectConfig: (path: string) => void
  openWorkspace: () => Promise<boolean>
  reset: () => void
}

export const useLauncherStore = create<LauncherState>((set, get) => ({
  scripts: [],
  configs: [],
  selectedScript: '',
  selectedConfig: '',
  requiresConfigTemplate: false,
  configSource: '',
  step: 0,
  loading: false,
  async fetchScripts() {
    const requestId = ++launcherRequestSeq
    set({ loading: true })
    try {
      const res = await api.getLauncherScripts()
      if (requestId !== launcherRequestSeq) {
        return
      }
      set(state => ({ scripts: res.items, step: state.selectedScript ? state.step : 0 }))
    } finally {
      if (requestId === launcherRequestSeq) {
        set({ loading: false })
      }
    }
  },
  async selectScript(path: string) {
    const requestId = ++launcherRequestSeq
    set({
      selectedScript: path,
      loading: true,
      requiresConfigTemplate: false,
      configSource: '',
    })
    try {
      const res = await api.getLauncherConfigs(path)
      if (requestId !== launcherRequestSeq) {
        return
      }
      const workspaceDefault = res.items.find(item => item.kind === 'workspace_default')
      const shouldPromptForConfig = (res.config_source || '') === 'pyruns_load'
      set({
        configs: res.items,
        selectedConfig: shouldPromptForConfig ? '' : workspaceDefault?.path || '',
        requiresConfigTemplate: Boolean(res.requires_config_template),
        configSource: res.config_source || '',
        step: workspaceDefault && !shouldPromptForConfig ? 2 : 1,
      })
    } finally {
      if (requestId === launcherRequestSeq) {
        set({ loading: false })
      }
    }
  },
  selectConfig(path) {
    launcherRequestSeq += 1
    set({ selectedConfig: path, step: 1 })
  },
  openWorkspace() {
    if (launcherOpenPromise) {
      return launcherOpenPromise
    }
    const requestId = ++launcherRequestSeq
    const openToken = Symbol('launcher-open')
    launcherOpenToken = openToken
    const { selectedScript, selectedConfig } = get()
    set({ loading: true })
    const pending = (async () => {
      try {
        const workspace = await api.openLauncherWorkspace(selectedScript, selectedConfig || undefined)
        if (requestId !== launcherRequestSeq) {
          return false
        }
        useWorkspaceStore.getState().setWorkspace(workspace)
        return true
      } finally {
        if (requestId === launcherRequestSeq) {
          set({ loading: false })
        }
        if (launcherOpenToken === openToken) {
          launcherOpenPromise = null
          launcherOpenToken = null
        }
      }
    })()
    launcherOpenPromise = pending
    return pending
  },
  reset() {
    launcherRequestSeq += 1
    launcherOpenPromise = null
    launcherOpenToken = null
    set({
      scripts: [],
      configs: [],
      selectedScript: '',
      selectedConfig: '',
      requiresConfigTemplate: false,
      configSource: '',
      step: 0,
      loading: false,
    })
  },
}))
