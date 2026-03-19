import { create } from 'zustand'
import type {
  ConfigCandidate,
  Dashboard,
  GeneratorMode,
  ScriptCandidate,
  Task,
  TemplateContent,
  WorkspaceInfo,
} from './types'
import * as api from './api'

let monitorRequestSeq = 0
const THEME_STORAGE_KEY = 'pyruns_theme'
const MANAGER_COLS_STORAGE_KEY = 'pyruns_manager_cols'
const PINNED_PARAMS_STORAGE_KEY = 'pyruns_pinned_params'

interface ThemeState {
  theme: 'dark' | 'light'
  toggle: () => void
}

function resolveInitialTheme(): 'dark' | 'light' {
  if (typeof window === 'undefined') {
    return 'light'
  }
  return window.localStorage.getItem(THEME_STORAGE_KEY) === 'dark' ? 'dark' : 'light'
}

function readStoredNumber(key: string, fallback: number) {
  if (typeof window === 'undefined') {
    return fallback
  }
  const raw = window.localStorage.getItem(key)
  if (!raw) {
    return fallback
  }
  const parsed = Number.parseInt(raw, 10)
  return Number.isFinite(parsed) ? parsed : fallback
}

function readStoredStringArray(key: string) {
  if (typeof window === 'undefined') {
    return [] as string[]
  }
  try {
    const raw = window.localStorage.getItem(key)
    if (!raw) {
      return []
    }
    const parsed = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : []
  } catch {
    return []
  }
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
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(THEME_STORAGE_KEY, next)
    }
    applyThemeClass(next)
    set({ theme: next })
  },
}))

interface WorkspaceState {
  workspace: WorkspaceInfo | null
  lastScriptWorkspace: WorkspaceInfo | null
  loading: boolean
  fetch: () => Promise<void>
  setWorkspace: (workspace: WorkspaceInfo | null) => void
  setRunRoot: (path: string) => Promise<void>
  openShellWorkspace: () => Promise<void>
  exitShellWorkspace: () => Promise<WorkspaceInfo | null>
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  workspace: null,
  lastScriptWorkspace: null,
  loading: false,
  async fetch() {
    set({ loading: true })
    try {
      const ws = await api.getWorkspace()
      set(state => ({
        workspace: ws,
        lastScriptWorkspace: ws?.workspace_kind === 'script' ? ws : state.lastScriptWorkspace,
      }))
    } finally {
      set({ loading: false })
    }
  },
  setWorkspace(workspace) {
    set(state => ({
      workspace,
      lastScriptWorkspace: workspace?.workspace_kind === 'script' ? workspace : state.lastScriptWorkspace,
    }))
  },
  async setRunRoot(path: string) {
    const ws = await api.setRunRoot(path)
    set(state => ({
      workspace: ws,
      lastScriptWorkspace: ws?.workspace_kind === 'script' ? ws : state.lastScriptWorkspace,
    }))
  },
  async openShellWorkspace() {
    const ws = await api.openShellWorkspace()
    set(state => ({ workspace: ws, lastScriptWorkspace: state.lastScriptWorkspace }))
  },
  async exitShellWorkspace() {
    const nextWorkspace = get().lastScriptWorkspace
    if (!nextWorkspace?.run_root) {
      return null
    }

    const ws = await api.setRunRoot(nextWorkspace.run_root)
    set({ workspace: ws, lastScriptWorkspace: ws })
    return ws
  },
}))

interface TaskState {
  tasks: Task[]
  total: number
  offset: number
  limit: number
  hasMore: boolean
  query: string
  statusFilter: string
  selectedIds: Set<string>
  loading: boolean
  columns: number
  setQuery: (q: string) => void
  setStatusFilter: (s: string) => void
  setOffset: (o: number) => void
  setColumns: (n: number) => void
  fetchTasks: () => Promise<void>
  toggleSelect: (name: string) => void
  selectAll: () => void
  clearSelection: () => void
  setSelectedIds: (ids: Set<string>) => void
}

export const useTaskStore = create<TaskState>((set, get) => ({
  tasks: [],
  total: 0,
  offset: 0,
  limit: 50,
  hasMore: false,
  query: '',
  statusFilter: 'All',
  selectedIds: new Set(),
  loading: false,
  columns: readStoredNumber(MANAGER_COLS_STORAGE_KEY, 5),
  setQuery(q) { set({ query: q, offset: 0 }) },
  setStatusFilter(s) { set({ statusFilter: s, offset: 0 }) },
  setOffset(o) { set({ offset: o }) },
  setColumns(n) {
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(MANAGER_COLS_STORAGE_KEY, String(n))
    }
    set({ columns: n })
  },
  async fetchTasks() {
    const { query, statusFilter, offset, limit } = get()
    set({ loading: true })
    try {
      const page = await api.getTasks({ query, status: statusFilter, offset, limit })
      set({ tasks: page.items, total: page.total, hasMore: page.has_more })
    } finally {
      set({ loading: false })
    }
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
  fetch: () => Promise<void>
}

export const useDashboardStore = create<DashboardState>((set) => ({
  data: null,
  loading: false,
  async fetch() {
    set({ loading: true })
    try {
      const d = await api.getDashboard()
      set({ data: d })
    } finally {
      set({ loading: false })
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
  pinnedParams: string[]
  loading: boolean
  fetchTemplates: () => Promise<void>
  loadTemplate: (value: string) => Promise<void>
  clearTemplate: () => void
  setViewMode: (m: GeneratorMode) => void
  setYamlText: (t: string) => void
  setShellText: (t: string) => void
  setNamePrefix: (n: string) => void
  setAppendTimestamp: (b: boolean) => void
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
  pinnedParams: readStoredStringArray(PINNED_PARAMS_STORAGE_KEY),
  loading: false,
  async fetchTemplates() {
    const res = await api.getTemplates()
    set({ templates: res.items })
  },
  async loadTemplate(value: string) {
    if (!value) {
      set({ selectedTemplate: '', templateContent: null, yamlText: '' })
      return
    }
    set({ loading: true, selectedTemplate: value })
    try {
      const content = await api.getTemplateContent(value)
      set({
        templateContent: content,
        yamlText: content.content,
        shellText: content.mode_hint === 'shell' ? content.content : get().shellText,
        viewMode: content.mode_hint === 'shell' ? 'shell' : get().viewMode === 'shell' ? 'yaml' : get().viewMode,
      })
    } finally {
      set({ loading: false })
    }
  },
  clearTemplate() {
    set({ selectedTemplate: '', templateContent: null })
  },
  setViewMode(m) { set({ viewMode: m }) },
  setYamlText(t) { set({ yamlText: t }) },
  setShellText(t) { set({ shellText: t }) },
  setNamePrefix(n) { set({ namePrefix: n }) },
  setAppendTimestamp(b) { set({ appendTimestamp: b }) },
  togglePin(key) {
    const pins = [...get().pinnedParams]
    const idx = pins.indexOf(key)
    if (idx >= 0) pins.splice(idx, 1); else pins.push(key)
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(PINNED_PARAMS_STORAGE_KEY, JSON.stringify(pins))
    }
    set({ pinnedParams: pins })
  },
}))

interface MonitorState {
  selectedTaskName: string | null
  logContent: string
  logOffset: number
  availableLogs: string[]
  selectedLog: string
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
  selectedTaskName: null,
  logContent: '',
  logOffset: 0,
  availableLogs: [],
  selectedLog: '',
  loading: false,
  exportIds: new Set(),
  async selectTask(name: string) {
    const requestId = ++monitorRequestSeq
    set({
      selectedTaskName: name,
      logContent: '',
      logOffset: 0,
      availableLogs: [],
      selectedLog: '',
      loading: true,
    })
    try {
      const logs = await api.getTaskLogs(name, undefined, undefined, 50000)
      if (requestId !== monitorRequestSeq || get().selectedTaskName !== name) {
        return
      }
      set({
        logContent: logs.content,
        logOffset: logs.offset,
        availableLogs: logs.available_logs,
        selectedLog: logs.selected_log,
      })
    } finally {
      if (requestId === monitorRequestSeq) {
        set({ loading: false })
      }
    }
  },
  async selectLogFile(logName: string) {
    const { selectedTaskName } = get()
    if (!selectedTaskName) return
    const requestId = ++monitorRequestSeq
    set({ selectedLog: logName, logContent: '', logOffset: 0, loading: true })
    try {
      const logs = await api.getTaskLogs(selectedTaskName, logName, undefined, 50000)
      if (
        requestId !== monitorRequestSeq
        || get().selectedTaskName !== selectedTaskName
        || get().selectedLog !== logName
      ) {
        return
      }
      set({
        logContent: logs.content,
        logOffset: logs.offset,
        availableLogs: logs.available_logs,
        selectedLog: logs.selected_log,
      })
    } finally {
      if (requestId === monitorRequestSeq) {
        set({ loading: false })
      }
    }
  },
  appendLog(text: string) {
    set(s => ({ logContent: s.logContent + text }))
  },
  clearLog() { set({ logContent: '', logOffset: 0 }) },
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
  step: number
  loading: boolean
  fetchScripts: () => Promise<void>
  selectScript: (path: string) => Promise<void>
  selectConfig: (path: string) => void
  openWorkspace: () => Promise<void>
  reset: () => void
}

export const useLauncherStore = create<LauncherState>((set, get) => ({
  scripts: [],
  configs: [],
  selectedScript: '',
  selectedConfig: '',
  step: 0,
  loading: false,
  async fetchScripts() {
    set({ loading: true })
    try {
      const res = await api.getLauncherScripts()
      set({ scripts: res.items, step: 0 })
    } finally {
      set({ loading: false })
    }
  },
  async selectScript(path: string) {
    set({ selectedScript: path, loading: true })
    try {
      const res = await api.getLauncherConfigs(path)
      set({ configs: res.items, step: 1 })
    } finally {
      set({ loading: false })
    }
  },
  selectConfig(path) {
    set({ selectedConfig: path, step: 2 })
  },
  async openWorkspace() {
    const { selectedScript, selectedConfig } = get()
    set({ loading: true })
    try {
      const workspace = await api.openLauncherWorkspace(selectedScript, selectedConfig || undefined)
      useWorkspaceStore.getState().setWorkspace(workspace)
    } finally {
      set({ loading: false })
    }
  },
  reset() {
    set({ scripts: [], configs: [], selectedScript: '', selectedConfig: '', step: 0 })
  },
}))
