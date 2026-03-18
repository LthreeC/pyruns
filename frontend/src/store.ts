import { create } from 'zustand'
import type { Task, WorkspaceInfo, TemplateContent, Dashboard, ScriptCandidate, ConfigCandidate } from './types'
import * as api from './api'

/* ── Theme Store ── */
interface ThemeState {
  theme: 'dark' | 'light'
  toggle: () => void
}

export const useThemeStore = create<ThemeState>((set, get) => ({
  theme: (localStorage.getItem('pyruns_theme') as 'dark' | 'light') || 'dark',
  toggle() {
    const next = get().theme === 'dark' ? 'light' : 'dark'
    localStorage.setItem('pyruns_theme', next)
    document.documentElement.className = next === 'dark' ? 'dark' : 'light'
    set({ theme: next })
  },
}))

/* ── Workspace Store ── */
interface WorkspaceState {
  workspace: WorkspaceInfo | null
  loading: boolean
  fetch: () => Promise<void>
  setRunRoot: (path: string) => Promise<void>
}

export const useWorkspaceStore = create<WorkspaceState>((set) => ({
  workspace: null,
  loading: false,
  async fetch() {
    set({ loading: true })
    try {
      const ws = await api.getWorkspace()
      set({ workspace: ws })
    } finally {
      set({ loading: false })
    }
  },
  async setRunRoot(path: string) {
    const ws = await api.setRunRoot(path)
    set({ workspace: ws })
  },
}))

/* ── Task Store ── */
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
  columns: parseInt(localStorage.getItem('pyruns_manager_cols') || '5'),
  setQuery(q) { set({ query: q, offset: 0 }) },
  setStatusFilter(s) { set({ statusFilter: s, offset: 0 }) },
  setOffset(o) { set({ offset: o }) },
  setColumns(n) {
    localStorage.setItem('pyruns_manager_cols', String(n))
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

/* ── Dashboard Store ── */
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

/* ── Generator Store ── */
interface GeneratorState {
  templates: { value: string; label: string }[]
  selectedTemplate: string
  templateContent: TemplateContent | null
  viewMode: 'form' | 'yaml' | 'args'
  yamlText: string
  argsText: string
  runScript: string
  namePrefix: string
  appendTimestamp: boolean
  pinnedParams: string[]
  loading: boolean
  fetchTemplates: () => Promise<void>
  loadTemplate: (value: string) => Promise<void>
  setViewMode: (m: 'form' | 'yaml' | 'args') => void
  setYamlText: (t: string) => void
  setArgsText: (t: string) => void
  setRunScript: (t: string) => void
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
  argsText: '',
  runScript: '',
  namePrefix: 'task',
  appendTimestamp: true,
  pinnedParams: JSON.parse(localStorage.getItem('pyruns_pinned_params') || '[]'),
  loading: false,
  async fetchTemplates() {
    const res = await api.getTemplates()
    set({ templates: res.items })
  },
  async loadTemplate(value: string) {
    set({ loading: true, selectedTemplate: value })
    try {
      const content = await api.getTemplateContent(value)
      set({
        templateContent: content,
        yamlText: content.content,
        argsText: content.args_text,
        runScript: content.run_script,
        viewMode: content.mode_hint === 'args' ? 'args' : get().viewMode,
      })
    } finally {
      set({ loading: false })
    }
  },
  setViewMode(m) { set({ viewMode: m }) },
  setYamlText(t) { set({ yamlText: t }) },
  setArgsText(t) { set({ argsText: t }) },
  setRunScript(t) { set({ runScript: t }) },
  setNamePrefix(n) { set({ namePrefix: n }) },
  setAppendTimestamp(b) { set({ appendTimestamp: b }) },
  togglePin(key: string) {
    const pins = [...get().pinnedParams]
    const idx = pins.indexOf(key)
    if (idx >= 0) pins.splice(idx, 1); else pins.push(key)
    localStorage.setItem('pyruns_pinned_params', JSON.stringify(pins))
    set({ pinnedParams: pins })
  },
}))

/* ── Monitor Store ── */
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
    set({ selectedTaskName: name, logContent: '', logOffset: 0, loading: true })
    try {
      const logs = await api.getTaskLogs(name)
      set({
        logContent: logs.content,
        logOffset: logs.offset,
        availableLogs: logs.available_logs,
        selectedLog: logs.selected_log,
      })
    } finally {
      set({ loading: false })
    }
  },
  async selectLogFile(logName: string) {
    const { selectedTaskName } = get()
    if (!selectedTaskName) return
    set({ selectedLog: logName, loading: true })
    try {
      const logs = await api.getTaskLogs(selectedTaskName, logName)
      set({ logContent: logs.content, logOffset: logs.offset })
    } finally {
      set({ loading: false })
    }
  },
  appendLog(text: string) {
    set(s => ({ logContent: s.logContent + text }))
  },
  clearLog() { set({ logContent: '', logOffset: 0 }) },
  toggleExport(name: string) {
    const ids = new Set(get().exportIds)
    if (ids.has(name)) ids.delete(name); else ids.add(name)
    set({ exportIds: ids })
  },
  selectAllExport(names: string[]) {
    set({ exportIds: new Set(names) })
  },
  clearExport() { set({ exportIds: new Set() }) },
}))

/* ── Launcher Store ── */
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
  selectConfig(path: string) {
    set({ selectedConfig: path, step: 2 })
  },
  async openWorkspace() {
    const { selectedScript, selectedConfig } = get()
    set({ loading: true })
    try {
      await api.openLauncherWorkspace(selectedScript, selectedConfig || undefined)
      await useWorkspaceStore.getState().fetch()
    } finally {
      set({ loading: false })
    }
  },
  reset() {
    set({ scripts: [], configs: [], selectedScript: '', selectedConfig: '', step: 0 })
  },
}))
