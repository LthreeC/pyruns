import { create } from 'zustand'
import type {
  ConfigCandidate,
  Dashboard,
  GeneratorMode,
  RuntimeInfo,
  ScriptCandidate,
  SystemMetrics,
  Task,
  TaskPage,
  TaskSearchMatch,
  TaskSearchScope,
  TaskSearchOptions,
  TaskSortMode,
  TaskStatusCounts,
  TemplateContent,
  WorkspaceInfo,
} from './types'
import * as api from './api'
import {
  MAX_MONITOR_LOG_CHARS, monitorLogTailLines, monitorPreviewTailLines, resolveMonitorScrollback,
} from './utils/monitorSettings'
import { configPathId } from './utils/configPaths'
import { retainTaskSnapshots } from './utils/taskSnapshots'
import { t as translate } from './i18n'

let taskRequestSeq = 0
let monitorTaskRequestSeq = 0
let monitorRequestSeq = 0
let monitorLogController: AbortController | null = null
let launcherRequestSeq = 0
let launcherReadController: AbortController | null = null
let runtimeRequestSeq = 0
let dashboardRequestSeq = 0
let metricsRequestSeq = 0
let generatorTemplateListRequestSeq = 0
let generatorTemplateListController: AbortController | null = null
let generatorTemplateListPending: Promise<void> | null = null
let generatorTemplateRequestSeq = 0
let generatorTemplateController: AbortController | null = null
let generatorDraftVersion = 0
let workspaceRequestSeq = 0
let launcherOpenPromise: Promise<boolean> | null = null
let launcherOpenToken: symbol | null = null
let toastIdSeq = 0
let confirmationIdSeq = 0
let confirmationResolver: ((confirmed: boolean) => void) | null = null
const THEME_STORAGE_KEY = 'pyruns_theme'
const MANAGER_SORT_STORAGE_KEY = 'pyruns_manager_sort'
const GENERATOR_COLS_STORAGE_KEY = 'pyruns_generator_cols'
const GENERATOR_STORAGE_PREFERENCE_KEY = 'pyruns_generator_task_storage'
const GENERATOR_TIMESTAMP_PREFERENCE_KEY = 'pyruns_generator_append_timestamp'
const GENERATOR_TEMPLATE_CACHE_MS = 15_000
const TASK_SOURCES_PREFERENCE_KEY = 'pyruns_task_sources'
const PINNED_PARAMS_STORAGE_KEY = 'pyruns_pinned_params_v2'
export const MANAGER_TASK_PAGE_SIZE = 50
export const MONITOR_TASK_PAGE_SIZE = 200
const DEFAULT_SEARCH_OPTIONS: TaskSearchOptions = { matchCase: false, wholeWord: false, useRegex: false }
const DEFAULT_SOURCES = [{ id: 'shared', host: '' }, { id: 'local', host: '' }]
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

export const DEFAULT_SEARCH_SETTINGS = { searchOnType: true, searchOnTypeDebouncePeriod: 300, maxResults: 20_000 as number | null }
type SearchSettings = typeof DEFAULT_SEARCH_SETTINGS
const SEARCH_SETTINGS_KEY = 'pyruns_search_settings'

function normalizeSearchSettings(value: Partial<SearchSettings> | null): SearchSettings {
  const delay = value?.searchOnTypeDebouncePeriod
  const limit = value?.maxResults
  return {
    searchOnType: typeof value?.searchOnType === 'boolean' ? value.searchOnType : true,
    searchOnTypeDebouncePeriod: typeof delay === 'number' && Number.isSafeInteger(delay) && delay >= 0 ? delay : 300,
    maxResults: limit === null ? null
      : typeof limit === 'number' && Number.isSafeInteger(limit) && limit > 0 ? limit : 20_000,
  }
}

function readSearchSettings() {
  try { return normalizeSearchSettings(JSON.parse(readLocalStorage(SEARCH_SETTINGS_KEY) || 'null')) }
  catch { return { ...DEFAULT_SEARCH_SETTINGS } }
}

export const useSearchSettingsStore = create<SearchSettings & { update: (value: Partial<SearchSettings>) => void }>((set, get) => ({
  ...readSearchSettings(),
  update(value) {
    const settings = normalizeSearchSettings({ ...get(), ...value })
    writeLocalStorage(SEARCH_SETTINGS_KEY, JSON.stringify(settings))
    set(settings)
  },
}))

export const usePreferencesStore = create<{
  isOpen: boolean
  returnFocus: HTMLElement | null
  open: (returnFocus?: HTMLElement | null) => void
  close: () => void
}>((set, get) => ({
  isOpen: false,
  returnFocus: null,
  open(returnFocus) {
    if (get().isOpen) return
    set({
      isOpen: true,
      returnFocus: returnFocus ?? (typeof document !== 'undefined' && document.activeElement instanceof HTMLElement ? document.activeElement : null),
    })
  },
  close() {
    const target = get().returnFocus
    set({ isOpen: false, returnFocus: null })
    if (typeof window !== 'undefined') window.requestAnimationFrame(() => {
      if (!get().isOpen && target?.isConnected) target.focus({ preventScroll: true })
    })
  },
}))

const SEARCH_HISTORY_KEY = 'pyruns_search_history'

function readSearchHistory(): string[] {
  try {
    const value = JSON.parse(readLocalStorage(SEARCH_HISTORY_KEY) || '[]')
    return Array.isArray(value) ? [...new Set(value.filter((item): item is string => typeof item === 'string' && item.length > 0))].slice(0, 100) : []
  } catch { return [] }
}

export const useSearchHistoryStore = create<{
  items: string[]
  revision: number
  remember: (query: string) => void
  clear: () => void
}>((set, get) => ({
  items: readSearchHistory(),
  revision: 0,
  remember(query) {
    if (!query || get().items[0] === query) return
    const items = [query, ...get().items.filter(item => item !== query)].slice(0, 100)
    writeLocalStorage(SEARCH_HISTORY_KEY, JSON.stringify(items))
    set({ items })
  },
  clear() {
    writeLocalStorage(SEARCH_HISTORY_KEY, '[]')
    set({ items: [], revision: get().revision + 1 })
  },
}))

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

function readWorkspacePreference(key: string, workspaceKey: string) {
  return workspaceKey ? readLocalStorage(`${key}:${workspaceKey}`) : null
}

function saveWorkspacePreference(key: string, value: string) {
  const workspaceKey = currentWorkspaceKey()
  if (workspaceKey) writeLocalStorage(`${key}:${workspaceKey}`, value)
}

function readTaskSourcesPreference(workspaceKey: string): string[] | null {
  try {
    const sources = JSON.parse(readWorkspacePreference(TASK_SOURCES_PREFERENCE_KEY, workspaceKey) || 'null')
    return Array.isArray(sources) && sources.every(source => typeof source === 'string' && source.length > 0)
      ? sources : null
  } catch {
    return null
  }
}

function resetWorkspaceScopedState(nextWorkspaceKey: string) {
  monitorLogController?.abort()
  taskSearchController?.abort()
  monitorSearchController?.abort()
  taskRequestSeq += 1
  monitorTaskRequestSeq += 1
  monitorRequestSeq += 1
  runtimeRequestSeq += 1
  dashboardRequestSeq += 1
  metricsRequestSeq += 1
  generatorTemplateListRequestSeq += 1
  generatorTemplateListController?.abort()
  generatorTemplateListController = null
  generatorTemplateListPending = null
  generatorTemplateRequestSeq += 1
  generatorTemplateController?.abort()
  generatorTemplateController = null
  generatorDraftVersion += 1
  useTaskStore.setState({
    sources: DEFAULT_SOURCES,
    selectedSources: readTaskSourcesPreference(nextWorkspaceKey),
    tasks: [],
    total: 0,
    statusCounts: null,
    offset: 0,
    hasMore: false,
    query: '',
    searchField: 'all',
    searchOptions: { ...DEFAULT_SEARCH_OPTIONS },
    statusFilter: 'All',
    selectedIds: new Set(),
    loading: false,
    error: null,
    searchLimitHit: false,
    monitorWorkspaceKey: nextWorkspaceKey,
    monitorTasks: [],
    monitorTotal: 0,
    monitorOffset: 0,
    monitorQuery: '',
    monitorSearchField: 'all',
    monitorSearchOptions: { ...DEFAULT_SEARCH_OPTIONS },
    monitorLoading: false,
    monitorError: '',
    monitorSearchLimitHit: false,
    monitorStatusCounts: null,
  })
  useMonitorStore.setState({
    workspaceKey: nextWorkspaceKey,
    selectedTaskName: null,
    logMatch: null,
    logError: '',
    logGeneration: monitorRequestSeq,
    logContent: '',
    logTextVersion: 0,
    logTextEnd: 0,
    logOffset: 0,
    logIdentity: '',
    logRevision: '',
    availableLogs: [],
    requestedLog: '',
    selectedLog: '',
    logTailTruncated: false,
    logTailByteLimited: false,
    logTailExpanded: false,
    loading: false,
    exportIds: new Set(),
  })
  useDashboardStore.setState({
    data: null, loading: false, error: null,
    metrics: null, metricsLoading: false, metricsError: null, metricsUpdatedAt: null,
  })
  const storage = readWorkspacePreference(GENERATOR_STORAGE_PREFERENCE_KEY, nextWorkspaceKey)
  useGeneratorStore.setState({
    templates: [],
    templateListUpdatedAt: null,
    selectedTemplate: '',
    templateContent: null,
    viewMode: 'form',
    yamlText: '',
    shellText: '',
    namePrefix: 'task',
    appendTimestamp: readWorkspacePreference(GENERATOR_TIMESTAMP_PREFERENCE_KEY, nextWorkspaceKey) !== 'false',
    storageOverride: storage === 'local' || storage === 'shared' ? storage : null,
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
  const lineLimit = monitorLogTailLines(Math.max(0, Math.trunc(maxLines)))
  const tail = content.slice(-MAX_MONITOR_LOG_CHARS)
  let end = tail.length - (tail.endsWith('\n') ? 2 : 1)
  for (let lines = lineLimit; lines > 0 && end >= 0; lines -= 1) {
    const newline = tail.lastIndexOf('\n', end)
    if (newline < 0) break
    if (lines === 1) return tail.slice(newline + 1)
    end = newline - 1
  }
  return tail
}

function comparableLogText(text: string) {
  return text.replace(/\r\n/g, '\n')
}

function isPyrunsLifecycleChunk(text: string) {
  return text.includes('[PYRUNS]')
    && (text.includes(' START ') || text.includes(' FINISH ') || text.includes('[PYRUNS] Source '))
}

interface MonitorLogText {
  logContent: string
  logTextVersion: number
  // UTF-16 coordinates in this version, independent of disk byte offsets and trimming.
  logTextEnd: number
}

export function replaceMonitorLogContent(state: MonitorLogText, logContent: string): MonitorLogText {
  return { logContent, logTextVersion: state.logTextVersion + 1, logTextEnd: logContent.length }
}

export function appendMonitorLogContent(state: MonitorLogText, chunks: string | readonly string[]): MonitorLogText {
  let content = state.logContent
  let appendedChars = 0
  for (const text of typeof chunks === 'string' ? [chunks] : chunks) {
    if (!text) continue
    if (isPyrunsLifecycleChunk(text)) {
      const contentTail = content.slice(Math.max(0, content.length - text.length - 32))
      if (comparableLogText(contentTail).endsWith(comparableLogText(text))) continue
    }
    content += text
    appendedChars += text.length
  }
  return {
    logContent: appendedChars ? trimMonitorLogContent(content) : state.logContent,
    logTextVersion: state.logTextVersion,
    logTextEnd: state.logTextEnd + appendedChars,
  }
}

export function monitorLogAppendDelta(previous: Pick<MonitorLogText, 'logTextVersion' | 'logTextEnd'>, next: MonitorLogText) {
  const appendedChars = next.logTextEnd - previous.logTextEnd
  if (next.logTextVersion !== previous.logTextVersion || appendedChars < 0 || appendedChars > next.logContent.length) {
    return null
  }
  return appendedChars ? next.logContent.slice(-appendedChars) : ''
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

// Request context follows accepted workspace snapshots, including reconnects.
useWorkspaceStore.subscribe((state, previous) => {
  if (state.workspace !== previous.workspace) {
    api.setWorkspaceContext(String(state.workspace?.run_root || ''))
  }
})

interface RuntimeState {
  runtime: RuntimeInfo | null
  loading: boolean
  dirty: boolean
  setRuntime: (runtime: RuntimeInfo | null) => void
  setDirty: (dirty: boolean) => void
  fetchRuntime: (refreshProviders?: boolean) => Promise<RuntimeInfo>
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
  async fetchRuntime(refreshProviders = true) {
    const requestId = ++runtimeRequestSeq
    const workspaceKey = currentWorkspaceKey()
    set({ loading: true })
    try {
      const runtime = await api.getRuntimeInfo(refreshProviders)
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
  return monitorLogTailLines(resolveMonitorScrollback(useWorkspaceStore.getState().workspace?.settings))
}

interface TaskState {
  sources: NonNullable<TaskPage['task_sources']>
  selectedSources: string[] | null
  setSelectedSources: (ids: string[] | null) => void
  cancelTaskSearch: () => void
  cancelMonitorSearch: () => void
  tasks: Task[]
  monitorWorkspaceKey: string
  monitorTasks: Task[]
  monitorTotal: number
  monitorOffset: number
  monitorQuery: string
  monitorSearchField: TaskSearchScope
  monitorSearchOptions: TaskSearchOptions
  monitorLoading: boolean
  monitorError: string
  monitorSearchLimitHit: boolean
  monitorStatusCounts: TaskStatusCounts | null
  total: number
  statusCounts: TaskStatusCounts | null
  offset: number
  hasMore: boolean
  query: string
  searchField: TaskSearchScope
  searchOptions: TaskSearchOptions
  statusFilter: string
  sortMode: TaskSortMode
  selectedIds: Set<string>
  loading: boolean
  error: string | null
  searchLimitHit: boolean
  setQuery: (q: string) => void
  setSearchField: (field: TaskSearchScope) => void
  setMonitorSearchField: (field: TaskSearchScope) => void
  setSearchOptions: (options: TaskSearchOptions) => void
  setMonitorSearchOptions: (options: TaskSearchOptions) => void
  setStatusFilter: (s: string) => void
  setSortMode: (mode: TaskSortMode) => void
  setOffset: (o: number) => void
  fetchTasks: (options?: { refresh?: boolean; forceRefresh?: boolean; background?: boolean }) => Promise<void>
  fetchMonitorTasks: (options?: {
    query?: string
    searchField?: TaskSearchScope
    searchOptions?: TaskSearchOptions
    offset?: number
    refresh?: boolean
    forceRefresh?: boolean
    background?: boolean
    workspaceKey?: string
  }) => Promise<void>
  updateMonitorTask: (task: Task) => void
  toggleSelect: (name: string) => void
  selectAll: () => void
  clearSelection: () => void
  setSelectedIds: (ids: Set<string>) => void
}

let taskSearchController: AbortController | null = null
let monitorSearchController: AbortController | null = null

export const useTaskStore = create<TaskState>((set, get) => ({
  sources: DEFAULT_SOURCES,
  selectedSources: null,
  setSelectedSources(selectedSources) {
    saveWorkspacePreference(TASK_SOURCES_PREFERENCE_KEY, JSON.stringify(selectedSources))
    taskSearchController?.abort()
    monitorSearchController?.abort()
    taskRequestSeq += 1
    monitorTaskRequestSeq += 1
    set({ selectedSources, selectedIds: new Set(), offset: 0, monitorOffset: 0,
      tasks: [], monitorTasks: [], total: 0, monitorTotal: 0, hasMore: false,
      statusCounts: null, monitorStatusCounts: null, loading: false, monitorLoading: false })
    useMonitorStore.getState().clearExport()
  },
  cancelTaskSearch() {
    const wasSearching = Boolean(taskSearchController && get().loading && get().query)
    taskSearchController?.abort()
    taskSearchController = null
    taskRequestSeq += 1
    set({ loading: false, ...(wasSearching ? { error: 'Search cancelled. Refresh to search again.' } : {}) })
  },
  cancelMonitorSearch() {
    const wasSearching = Boolean(monitorSearchController && get().monitorLoading && get().monitorQuery)
    monitorSearchController?.abort()
    monitorSearchController = null
    monitorTaskRequestSeq += 1
    set({ monitorLoading: false, ...(wasSearching ? { monitorError: 'Search cancelled. Refresh to search again.' } : {}) })
  },
  tasks: [],
  monitorWorkspaceKey: '',
  monitorTasks: [],
  monitorTotal: 0,
  monitorOffset: 0,
  monitorQuery: '',
  monitorSearchField: 'all',
  monitorSearchOptions: { ...DEFAULT_SEARCH_OPTIONS },
  monitorLoading: false,
  monitorError: '',
  monitorSearchLimitHit: false,
  monitorStatusCounts: null,
  total: 0,
  statusCounts: null,
  offset: 0,
  hasMore: false,
  query: '',
  searchField: 'all',
  searchOptions: { ...DEFAULT_SEARCH_OPTIONS },
  statusFilter: 'All',
  sortMode: readStoredTaskSortMode(),
  selectedIds: new Set(),
  loading: false,
  error: null,
  searchLimitHit: false,
  setQuery(q) {
    if (q !== get().query) {
      taskSearchController?.abort()
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
  setSearchField(searchField) {
    if (searchField !== get().searchField) {
      taskSearchController?.abort()
      taskRequestSeq += 1
      set({ searchField, offset: 0, selectedIds: new Set() })
    }
  },
  setMonitorSearchField(monitorSearchField) {
    if (monitorSearchField !== get().monitorSearchField) {
      monitorSearchController?.abort()
      monitorTaskRequestSeq += 1
      set({ monitorSearchField, monitorOffset: 0 })
    }
  },
  setSearchOptions(searchOptions) {
    taskSearchController?.abort()
    taskRequestSeq += 1
    set({ searchOptions, offset: 0, selectedIds: new Set() })
  },
  setMonitorSearchOptions(monitorSearchOptions) {
    monitorSearchController?.abort()
    monitorTaskRequestSeq += 1
    set({ monitorSearchOptions, monitorOffset: 0 })
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
  async fetchTasks(options = {}) {
    if (options.background && (get().loading || get().query)) return
    taskSearchController?.abort()
    const controller = new AbortController()
    taskSearchController = controller
    const requestId = ++taskRequestSeq
    const workspaceKey = currentWorkspaceKey()
    const { query, searchField, searchOptions, statusFilter, sortMode, offset } = get()
    const limit = MANAGER_TASK_PAGE_SIZE
    // Searching may scan remote logs; only ordinary list reads use this budget.
    const timeout = query ? undefined : setTimeout(() => {
      controller.abort(new Error('Task list loading timed out. Refresh to retry.'))
    }, 10_000)
    const maxResults = useSearchSettingsStore.getState().maxResults
    let requestedOffset = offset
    const isCurrentRequest = () => {
      const current = get()
      return requestId === taskRequestSeq
        && workspaceKey === currentWorkspaceKey()
        && current.query === query
        && current.searchField === searchField
        && current.searchOptions === searchOptions
        && current.statusFilter === statusFilter
        && current.sortMode === sortMode
        && current.offset === requestedOffset
        && useSearchSettingsStore.getState().maxResults === maxResults
    }
    const onProgress = query ? (page: TaskPage) => {
      if (isCurrentRequest()) set({ tasks: retainTaskSnapshots(get().tasks, page.items), total: page.total, hasMore: page.has_more,
        statusCounts: page.status_counts ?? null, searchLimitHit: Boolean(page.search_limit_hit) })
    } : undefined
    set(options.background ? { error: null } : { loading: true, error: null, searchLimitHit: false })
    try {
      const page = await api.getTasks({ query, sources: get().selectedSources, searchField, searchOptions, status: statusFilter, sort: sortMode, offset, limit, summary: true, includeLogs: true, refresh: options.refresh, forceRefresh: options.forceRefresh, maxResults, onProgress }, controller.signal)
      if (!isCurrentRequest()) {
        return
      }
      if (page.task_sources) set({ sources: page.task_sources })
      if (limit > 0 && offset > 0 && page.items.length === 0) {
        const nextOffset = page.total > 0
          ? Math.max(0, Math.floor((page.total - 1) / limit) * limit)
          : 0
        if (nextOffset !== offset && offset >= page.total) {
          requestedOffset = nextOffset
          set({ offset: nextOffset })
          const retryPage = await api.getTasks({
            query,
            sources: get().selectedSources,
            searchField,
            searchOptions,
            status: statusFilter,
            sort: sortMode,
            offset: nextOffset,
            limit,
            summary: true,
            includeLogs: true,
            refresh: options.refresh,
            forceRefresh: options.forceRefresh,
            maxResults,
            onProgress,
          }, controller.signal)
          if (!isCurrentRequest()) {
            return
          }
          set({
            tasks: retainTaskSnapshots(get().tasks, retryPage.items),
            searchLimitHit: Boolean(retryPage.search_limit_hit),
            error: retryPage.search_errors?.length ? `Search incomplete: ${retryPage.search_errors.join('; ')}` : null,
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
          tasks: retainTaskSnapshots(get().tasks, page.items),
          searchLimitHit: Boolean(page.search_limit_hit),
          error: page.search_errors?.length ? `Search incomplete: ${page.search_errors.join('; ')}` : null,
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
        tasks: retainTaskSnapshots(get().tasks, page.items),
        searchLimitHit: Boolean(page.search_limit_hit),
        total: page.total,
        statusCounts: page.status_counts ?? null,
        hasMore: page.has_more,
        selectedIds: visibleSelection,
        error: page.search_errors?.length ? `Search incomplete: ${page.search_errors.join('; ')}` : null,
      })
    } catch (err) {
      if (isCurrentRequest()) {
        set({ error: err instanceof Error ? err.message : 'Could not load tasks',
          ...(err instanceof Error && 'status' in err && err.status === 422
            ? { tasks: [], total: 0, hasMore: false, selectedIds: new Set<string>() } : {}),
        })
      }
    } finally {
      clearTimeout(timeout)
      if (taskSearchController === controller) taskSearchController = null
      if (isCurrentRequest()) {
        set({ loading: false })
      }
    }
  },
  async fetchMonitorTasks(options = {}) {
    if (String(options.workspaceKey ?? currentWorkspaceKey()) !== currentWorkspaceKey()) return
    if (options.background && (get().monitorLoading || (options.query ?? get().monitorQuery))) return
    monitorSearchController?.abort()
    const controller = new AbortController()
    monitorSearchController = controller
    const requestId = ++monitorTaskRequestSeq
    const current = get()
    const workspaceKey = String(options.workspaceKey ?? currentWorkspaceKey())
    if (workspaceKey !== currentWorkspaceKey()) {
      return
    }
    const query = String(options.query ?? current.monitorQuery)
    const timeout = query ? undefined : setTimeout(() => {
      controller.abort(new Error('Task list loading timed out. Refresh to retry.'))
    }, 10_000)
    const searchField = options.searchField ?? current.monitorSearchField
    const searchOptions = options.searchOptions ?? current.monitorSearchOptions
    const maxResults = useSearchSettingsStore.getState().maxResults
    const isCurrentRequest = () => requestId === monitorTaskRequestSeq
      && workspaceKey === currentWorkspaceKey() && get().monitorWorkspaceKey === workspaceKey
      && useSearchSettingsStore.getState().maxResults === maxResults
    const queryChanged = query !== current.monitorQuery || searchField !== current.monitorSearchField || searchOptions !== current.monitorSearchOptions
    const background = Boolean(options.background)
    let requestedOffset = queryChanged ? 0 : Math.max(0, options.offset ?? current.monitorOffset)
    set(background
      ? { monitorLoading: false, monitorQuery: query, monitorSearchField: searchField, monitorSearchOptions: searchOptions }
      : { monitorLoading: true, monitorError: '', monitorSearchLimitHit: false, monitorQuery: query, monitorSearchField: searchField, monitorSearchOptions: searchOptions })
    try {
      const requestPage = () => api.getTasks({
        query,
        sources: current.selectedSources,
        searchField,
        searchOptions,
        offset: requestedOffset,
        limit: MONITOR_TASK_PAGE_SIZE,
        refresh: options.refresh ?? true,
        forceRefresh: options.forceRefresh,
        summary: true,
        compact: true,
        includeLogs: true,
        maxResults,
        onProgress: query ? page => {
          if (isCurrentRequest()) set({ monitorTasks: retainTaskSnapshots(get().monitorTasks, page.items), monitorTotal: page.total,
            monitorOffset: requestedOffset,
            monitorSearchLimitHit: Boolean(page.search_limit_hit),
            monitorStatusCounts: page.status_counts ?? null })
        } : undefined,
      }, controller.signal)
      let page = await requestPage()
      if (!isCurrentRequest()) {
        return
      }
      if (requestedOffset > 0 && page.items.length === 0 && requestedOffset >= page.total) {
        requestedOffset = page.total > 0
          ? Math.floor((page.total - 1) / MONITOR_TASK_PAGE_SIZE) * MONITOR_TASK_PAGE_SIZE
          : 0
        if (page.total > 0) {
          page = await requestPage()
          if (!isCurrentRequest()) return
        }
      }
      set({
        monitorTasks: retainTaskSnapshots(get().monitorTasks, page.items),
        sources: page.task_sources ?? get().sources,
        monitorSearchLimitHit: Boolean(page.search_limit_hit),
        monitorTotal: page.total,
        monitorOffset: requestedOffset,
        monitorStatusCounts: page.status_counts ?? null,
        monitorError: page.search_errors?.length ? `Search incomplete: ${page.search_errors.join('; ')}` : '',
      })
    } catch (error) {
      if (!isCurrentRequest()) {
        return
      }
      set({ monitorError: error instanceof Error ? error.message : String(error),
        ...(error instanceof Error && 'status' in error && error.status === 422
          ? { monitorTasks: [], monitorTotal: 0, monitorOffset: 0 } : {}),
      })
      throw error
    } finally {
      clearTimeout(timeout)
      if (monitorSearchController === controller) monitorSearchController = null
      if (!background && isCurrentRequest()) {
        set({ monitorLoading: false })
      }
    }
  },
  updateMonitorTask(task) {
    if (!task?.name) {
      return
    }
    set(state => {
      const exists = state.monitorTasks.some(item => item.name === task.name)
      return exists ? {
        monitorTasks: state.monitorTasks.map(item => item.name === task.name ? { ...item, ...task } : item),
      } : state
    })
  },
  toggleSelect(name) {
    const ids = new Set(get().selectedIds)
    if (ids.has(name)) ids.delete(name); else ids.add(name)
    set({ selectedIds: ids })
  },
  selectAll() {
    set({ selectedIds: new Set(get().tasks.map(task => task.name)) })
  },
  clearSelection() { set({ selectedIds: new Set() }) },
  setSelectedIds(ids) { set({ selectedIds: ids }) },
}))

interface DashboardState {
  data: Dashboard | null
  loading: boolean
  error: string | null
  fetch: (signal?: AbortSignal) => Promise<void>
  metrics: SystemMetrics | null
  metricsLoading: boolean
  metricsError: string | null
  metricsUpdatedAt: number | null
  fetchMetrics: (signal?: AbortSignal) => Promise<void>
}

export const useDashboardStore = create<DashboardState>((set) => ({
  data: null,
  loading: false,
  error: null,
  metrics: null,
  metricsLoading: false,
  metricsError: null,
  metricsUpdatedAt: null,
  async fetch(signal) {
    const requestId = ++dashboardRequestSeq
    const workspaceKey = currentWorkspaceKey()
    set({ loading: true, error: null })
    try {
      const d = await api.getDashboard(true, 6, signal)
      signal?.throwIfAborted()
      if (requestId === dashboardRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ data: d, error: null })
      }
    } catch (err) {
      if (requestId === dashboardRequestSeq && workspaceKey === currentWorkspaceKey()
        && (!signal?.aborted || signal.reason?.name === 'TimeoutError')) {
        set({ error: err instanceof Error ? err.message : 'Could not load dashboard' })
      }
      throw err
    } finally {
      if (requestId === dashboardRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ loading: false })
      }
    }
  },
  async fetchMetrics(signal) {
    const requestId = ++metricsRequestSeq
    const workspaceKey = currentWorkspaceKey()
    set({ metricsLoading: true })
    try {
      const metrics = await api.getMetrics({}, signal)
      signal?.throwIfAborted()
      if (requestId === metricsRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ metrics, metricsError: null, metricsUpdatedAt: Date.now() })
      }
    } catch (err) {
      if (requestId === metricsRequestSeq && workspaceKey === currentWorkspaceKey()
        && (!signal?.aborted || signal.reason?.name === 'TimeoutError')) {
        set({ metricsError: err instanceof Error ? err.message : translate('System metrics unavailable.') })
      }
      throw err
    } finally {
      if (requestId === metricsRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ metricsLoading: false })
      }
    }
  },
}))

interface GeneratorState {
  templates: { value: string; label: string }[]
  templateListUpdatedAt: number | null
  selectedTemplate: string
  templateContent: TemplateContent | null
  viewMode: GeneratorMode
  yamlText: string
  shellText: string
  namePrefix: string
  appendTimestamp: boolean
  storageOverride: 'shared' | 'local' | null
  columns: number
  pinnedParams: string[]
  dirty: boolean
  loading: boolean
  fetchTemplates: (reuse?: boolean) => Promise<void>
  invalidateTemplateList: () => void
  loadTemplate: (value: string, loadedContent?: TemplateContent) => Promise<void>
  clearTemplate: () => void
  setViewMode: (m: GeneratorMode) => void
  setYamlText: (t: string) => void
  setShellText: (t: string) => void
  setNamePrefix: (n: string) => void
  setAppendTimestamp: (b: boolean) => void
  setStorageOverride: (storage: 'shared' | 'local' | null) => void
  setDirty: (dirty: boolean) => void
  setColumns: (n: number) => void
  togglePin: (key: string) => void
}

export const useGeneratorStore = create<GeneratorState>((set, get) => ({
  templates: [],
  templateListUpdatedAt: null,
  selectedTemplate: '',
  templateContent: null,
  viewMode: 'form',
  yamlText: '',
  shellText: '',
  namePrefix: 'task',
  appendTimestamp: true,
  storageOverride: null,
  columns: readStoredNumber(GENERATOR_COLS_STORAGE_KEY, 5, 2, 8),
  pinnedParams: readLocalStorage(PINNED_PARAMS_STORAGE_KEY) !== null
    ? readStoredStringArray(PINNED_PARAMS_STORAGE_KEY)
    : readStoredStringArray('pyruns_pinned_params').map(key => configPathId(key.split('.').filter(Boolean))),
  dirty: false,
  loading: false,
  fetchTemplates(reuse = false) {
    if (reuse && generatorTemplateListPending) return generatorTemplateListPending
    const updatedAt = get().templateListUpdatedAt
    if (reuse && updatedAt !== null && Date.now() - updatedAt < GENERATOR_TEMPLATE_CACHE_MS) return Promise.resolve()
    const requestId = ++generatorTemplateListRequestSeq
    const workspaceKey = currentWorkspaceKey()
    generatorTemplateListController?.abort()
    const controller = new AbortController()
    generatorTemplateListController = controller
    const timeout = setTimeout(() => controller.abort(new Error('Template list loading timed out. Retry to refresh.')), 10_000)
    const pending = (async () => {
      try {
        const res = await api.getTemplates(controller.signal)
        controller.signal.throwIfAborted()
        if (requestId === generatorTemplateListRequestSeq && workspaceKey === currentWorkspaceKey()) {
          set({ templates: res.items, templateListUpdatedAt: Date.now() })
        }
      } catch (error) {
        if (requestId === generatorTemplateListRequestSeq && workspaceKey === currentWorkspaceKey()) throw error
      } finally {
        clearTimeout(timeout)
        if (generatorTemplateListController === controller) {
          generatorTemplateListController = null
          generatorTemplateListPending = null
        }
      }
    })()
    generatorTemplateListPending = pending
    return pending
  },
  invalidateTemplateList() {
    generatorTemplateListRequestSeq += 1
    generatorTemplateListController?.abort()
    generatorTemplateListController = null
    generatorTemplateListPending = null
    set({ templateListUpdatedAt: null })
  },
  async loadTemplate(value: string, loadedContent?: TemplateContent) {
    const requestId = ++generatorTemplateRequestSeq
    const draftVersion = generatorDraftVersion
    const workspaceKey = currentWorkspaceKey()
    generatorTemplateController?.abort()
    if (!value) {
      generatorTemplateController = null
      set({ selectedTemplate: '', templateContent: null, yamlText: '', shellText: '', dirty: false, loading: false })
      return
    }
    const controller = new AbortController()
    generatorTemplateController = controller
    const timeout = setTimeout(() => controller.abort(new Error('Template loading timed out. Try again.')), 10_000)
    set({ loading: true })
    try {
      const content = loadedContent ?? await api.getTemplateContent(value, controller.signal)
      if (
        requestId !== generatorTemplateRequestSeq
        || draftVersion !== generatorDraftVersion
        || workspaceKey !== currentWorkspaceKey()
      ) {
        return
      }
      controller.signal.throwIfAborted()
      set({
        selectedTemplate: value,
        templateContent: content,
        yamlText: content.content,
        shellText: content.mode_hint === 'shell' ? content.content : get().shellText,
        viewMode: content.mode_hint === 'shell' ? 'shell' : get().viewMode === 'shell' ? 'yaml' : get().viewMode,
        dirty: false,
      })
    } catch (error) {
      if (requestId === generatorTemplateRequestSeq && draftVersion === generatorDraftVersion && workspaceKey === currentWorkspaceKey()) throw error
    } finally {
      clearTimeout(timeout)
      if (generatorTemplateController === controller) generatorTemplateController = null
      if (requestId === generatorTemplateRequestSeq && workspaceKey === currentWorkspaceKey()) {
        set({ loading: false })
      }
    }
  },
  clearTemplate() {
    generatorTemplateRequestSeq += 1
    generatorTemplateController?.abort()
    generatorTemplateController = null
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
  setAppendTimestamp(appendTimestamp) {
    if (appendTimestamp === get().appendTimestamp) return
    saveWorkspacePreference(GENERATOR_TIMESTAMP_PREFERENCE_KEY, String(appendTimestamp))
    set({ appendTimestamp })
  },
  setStorageOverride(storageOverride) {
    if (storageOverride === get().storageOverride) return
    saveWorkspacePreference(GENERATOR_STORAGE_PREFERENCE_KEY, storageOverride ?? '')
    set({ storageOverride })
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
    ? translate(labels[0])
    : translate('{{changes}} and {{last}}', { changes: labels.slice(0, -1).map(label => translate(label)).join(', '), last: translate(labels[labels.length - 1]) })
  return requestConfirmation({
    title: translate('Discard unsaved changes?'),
    description: translate('Discard unsaved {{description}} before switching workspaces?', { description }),
    confirmLabel: translate('Discard and Switch'),
    confirmVariant: 'danger',
  })
}

interface MonitorState extends MonitorLogText {
  workspaceKey: string
  selectedTaskName: string | null
  logMatch: TaskSearchMatch | null
  logError: string
  logGeneration: number
  logOffset: number
  logIdentity: string
  logRevision: string
  availableLogs: string[]
  requestedLog: string
  selectedLog: string
  logTailTruncated: boolean
  logTailByteLimited: boolean
  logTailExpanded: boolean
  loading: boolean
  exportIds: Set<string>
  selectTask: (name: string, match?: TaskSearchMatch) => Promise<void>
  selectLogFile: (name: string, expanded?: boolean) => Promise<void>
  appendLog: (text: string) => void
  clearLog: () => void
  clearTask: () => void
  toggleExport: (name: string) => void
  setExportSelected: (names: string[], selected: boolean) => void
  clearExport: () => void
}

export const useMonitorStore = create<MonitorState>((set, get) => ({
  workspaceKey: '',
  selectedTaskName: null,
  logMatch: null,
  logError: '',
  logGeneration: 0,
  logContent: '',
  logTextVersion: 0,
  logTextEnd: 0,
  logOffset: 0,
  logIdentity: '',
  logRevision: '',
  availableLogs: [],
  requestedLog: '',
  selectedLog: '',
  logTailTruncated: false,
  logTailByteLimited: false,
  logTailExpanded: false,
  loading: false,
  exportIds: new Set(),
  async selectTask(name: string, match?: TaskSearchMatch) {
    const requestId = ++monitorRequestSeq
    const workspaceKey = currentWorkspaceKey()
    if (get().workspaceKey !== workspaceKey) {
      return
    }
    monitorLogController?.abort()
    const controller = new AbortController()
    monitorLogController = controller
    const timeout = setTimeout(() => controller.abort(new Error('Log loading timed out. Please retry.')), 10_000)
    const logMatch = match?.field === 'log' ? match : null
    set({
      selectedTaskName: name,
      logMatch,
      logError: '',
      logGeneration: requestId,
      ...replaceMonitorLogContent(get(), ''),
      logOffset: 0,
      logIdentity: '',
      logRevision: '',
      availableLogs: [],
      requestedLog: '',
      selectedLog: logMatch?.log_file || '',
      logTailTruncated: false,
      logTailByteLimited: false,
      logTailExpanded: false,
      loading: true,
    })
    try {
      const logs = await api.getTaskLogs(name, logMatch ? {
        logFileName: logMatch.log_file,
        logIdentity: logMatch.log_identity,
        offset: logMatch.offset ?? 0,
        chunkSize: 32 * 1024,
      } : { tailLines: monitorPreviewTailLines(currentMonitorScrollback()) }, controller.signal)
      if (
        requestId !== monitorRequestSeq
        || get().selectedTaskName !== name
        || get().workspaceKey !== workspaceKey
        || currentWorkspaceKey() !== workspaceKey
      ) {
        return
      }
      if (logMatch && (!logs.available_logs.includes(logMatch.log_file ?? '')
        || logs.selected_log !== logMatch.log_file || logs.reset)) {
        throw new Error('This log changed. Refresh the search to locate the match again.')
      }
      set({
        ...replaceMonitorLogContent(get(), logs.content),
        logOffset: logs.offset,
        logIdentity: String(logs.log_identity || ''),
        logRevision: logs.log_revision || '',
        availableLogs: logs.available_logs,
        selectedLog: logs.selected_log,
        logTailTruncated: Boolean(logs.tail_truncated),
        logTailByteLimited: Boolean(logs.tail_byte_limited),
      })
    } catch (err) {
      if (requestId !== monitorRequestSeq || get().workspaceKey !== workspaceKey) return
      set({ logError: err instanceof Error ? err.message : String(err) })
      throw err
    } finally {
      clearTimeout(timeout)
      if (
        requestId === monitorRequestSeq
        && get().workspaceKey === workspaceKey
        && currentWorkspaceKey() === workspaceKey
      ) {
        set({ loading: false })
      }
    }
  },
  async selectLogFile(logName: string, expanded = false) {
    const { selectedTaskName } = get()
    if (!selectedTaskName) return
    const requestId = ++monitorRequestSeq
    const workspaceKey = currentWorkspaceKey()
    if (get().workspaceKey !== workspaceKey) {
      return
    }
    monitorLogController?.abort()
    const controller = new AbortController()
    monitorLogController = controller
    const timeout = setTimeout(() => controller.abort(new Error('Log loading timed out. Please retry.')), 10_000)
    set({
      requestedLog: logName,
      selectedLog: logName,
      logMatch: null,
      logError: '',
      logGeneration: requestId,
      ...replaceMonitorLogContent(get(), ''),
      logOffset: 0,
      logIdentity: '',
      logRevision: '',
      logTailTruncated: false,
      logTailByteLimited: false,
      logTailExpanded: expanded,
      loading: true,
    })
    try {
      const logs = await api.getTaskLogs(selectedTaskName, {
        logFileName: logName,
        tailLines: monitorPreviewTailLines(currentMonitorScrollback(), expanded),
      }, controller.signal)
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
        ...replaceMonitorLogContent(get(), logs.content),
        logOffset: logs.offset,
        logIdentity: String(logs.log_identity || ''),
        logRevision: logs.log_revision || '',
        availableLogs: logs.available_logs,
        selectedLog: logs.selected_log,
        logTailTruncated: Boolean(logs.tail_truncated),
        logTailByteLimited: Boolean(logs.tail_byte_limited),
      })
    } catch (err) {
      if (requestId !== monitorRequestSeq || get().workspaceKey !== workspaceKey) return
      set({ logError: err instanceof Error ? err.message : String(err) })
      throw err
    } finally {
      clearTimeout(timeout)
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
    set(s => appendMonitorLogContent(s, text))
  },
  clearLog() {
    set({ ...replaceMonitorLogContent(get(), ''), logOffset: 0, logIdentity: '', logRevision: '',
      logTailTruncated: false, logTailByteLimited: false, logTailExpanded: false })
  },
  clearTask() {
    monitorLogController?.abort()
    set({ ...replaceMonitorLogContent(get(), ''), selectedTaskName: null, logMatch: null, logError: '',
      logGeneration: ++monitorRequestSeq, logOffset: 0, logIdentity: '', logRevision: '', availableLogs: [], requestedLog: '', selectedLog: '',
      logTailTruncated: false, logTailByteLimited: false, logTailExpanded: false, loading: false })
  },
  toggleExport(name) {
    const ids = new Set(get().exportIds)
    if (ids.has(name)) ids.delete(name); else ids.add(name)
    set({ exportIds: ids })
  },
  setExportSelected(names, selected) {
    const ids = new Set(get().exportIds)
    for (const name of names) {
      if (selected) ids.add(name)
      else ids.delete(name)
    }
    set({ exportIds: ids })
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
  opening: boolean
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
  opening: false,
  async fetchScripts() {
    const requestId = ++launcherRequestSeq
    launcherReadController?.abort()
    const controller = new AbortController()
    launcherReadController = controller
    const timeout = setTimeout(() => controller.abort(new Error('Script discovery timed out. Please retry.')), 10_000)
    set({ loading: true })
    try {
      const res = await api.getLauncherScripts(controller.signal)
      if (requestId !== launcherRequestSeq) {
        return
      }
      set(state => ({ scripts: res.items, step: state.selectedScript ? state.step : 0 }))
    } finally {
      clearTimeout(timeout)
      if (launcherReadController === controller) launcherReadController = null
      if (requestId === launcherRequestSeq) {
        set({ loading: false })
      }
    }
  },
  async selectScript(path: string) {
    const requestId = ++launcherRequestSeq
    launcherReadController?.abort()
    const controller = new AbortController()
    launcherReadController = controller
    const timeout = setTimeout(() => controller.abort(new Error('Config discovery timed out. Please retry.')), 10_000)
    set({
      selectedScript: path,
      loading: true,
      requiresConfigTemplate: false,
      configSource: '',
    })
    try {
      const res = await api.getLauncherConfigs(path, controller.signal)
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
      clearTimeout(timeout)
      if (launcherReadController === controller) launcherReadController = null
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
    set({ loading: true, opening: true })
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
          set({ loading: false, opening: false })
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
    launcherReadController?.abort()
    launcherReadController = null
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
      opening: false,
    })
  },
}))
