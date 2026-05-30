import type {
  Dashboard,
  GeneratorPreview,
  GeneratorResult,
  Task,
  TaskLogs,
  TaskPage,
  TemplateContent,
  WorkspaceInfo,
  SystemMetrics,
  ScriptCandidate,
  LauncherConfigsResponse,
  WorkspaceCandidate,
  GeneratorMode,
  PathValidationResult,
} from './types'

const BASE = ''

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

export const getWorkspace = () => request<WorkspaceInfo>('/api/workspace')
export const setRunRoot = (path: string) =>
  request<WorkspaceInfo>('/api/workspace/run-root', { method: 'POST', body: JSON.stringify({ path }) })
export const openShellWorkspace = () =>
  request<WorkspaceInfo>('/api/workspace/shell', { method: 'POST' })

export const getTemplates = () => request<{ items: { value: string; label: string }[] }>('/api/templates')
export const getTemplateContent = (value: string) =>
  request<TemplateContent>(`/api/templates/content?value=${encodeURIComponent(value)}`)

export const createTasks = (payload: {
  name_prefix: string
  mode: GeneratorMode
  yaml_text?: string
  shell_text?: string
  template_value?: string
  append_timestamp?: boolean
}) => request<GeneratorResult>('/api/generator/create', { method: 'POST', body: JSON.stringify(payload) })

export const previewTasks = (payload: {
  mode: GeneratorMode
  yaml_text?: string
  shell_text?: string
  template_value?: string
}) => request<GeneratorPreview>('/api/generator/preview', { method: 'POST', body: JSON.stringify(payload) })

export const getDashboard = (refresh = true, recentLimit = 6) =>
  request<Dashboard>(`/api/dashboard?refresh=${refresh}&recent_limit=${recentLimit}`)

export const getTasks = (params: {
  query?: string
  status?: string
  offset?: number
  limit?: number
  refresh?: boolean
} = {}) => {
  const sp = new URLSearchParams()
  if (params.query) sp.set('query', params.query)
  if (params.status && params.status !== 'All') sp.set('status', params.status)
  if (params.offset != null) sp.set('offset', String(params.offset))
  if (params.limit != null) sp.set('limit', String(params.limit))
  if (params.refresh != null) sp.set('refresh', String(params.refresh))
  return request<TaskPage>(`/api/tasks?${sp}`)
}

export const getTask = (name: string, refresh = true) =>
  request<Task>(`/api/tasks/${encodeURIComponent(name)}?refresh=${refresh}`)

export const batchRunTasks = (taskNames: string[], executionMode?: string, maxWorkers?: number) =>
  request<{ count: number; items: Task[] }>('/api/tasks/batch/run', {
    method: 'POST',
    body: JSON.stringify({ task_names: taskNames, execution_mode: executionMode, max_workers: maxWorkers }),
  })

export const batchDeleteTasks = (taskNames: string[]) =>
  request<{ count: number; deleted: string[] }>('/api/tasks/batch/delete', {
    method: 'POST',
    body: JSON.stringify({ task_names: taskNames }),
  })

export async function exportTasksCsv(taskNames: string[]): Promise<Blob> {
  const res = await fetch(`${BASE}/api/tasks/export/csv`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_names: taskNames }),
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
  return res.blob()
}

export const runTask = (name: string, executionMode?: string) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/run`, {
    method: 'POST',
    body: JSON.stringify({ execution_mode: executionMode }),
  })

export const cancelTask = (name: string) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/cancel`, { method: 'POST' })

export const pinTask = (name: string, pinned?: boolean) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/pin`, {
    method: 'POST',
    body: JSON.stringify({ pinned }),
  })

export const updateNotes = (name: string, notes: string) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/notes`, {
    method: 'PATCH',
    body: JSON.stringify({ notes }),
  })

export const updateEnv = (name: string, env: Record<string, any>) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/env`, {
    method: 'PATCH',
    body: JSON.stringify({ env }),
  })

export const renameTask = (name: string, newName: string) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/rename`, {
    method: 'POST',
    body: JSON.stringify({ new_name: newName }),
  })

export const getTaskLogs = (name: string, logFileName?: string, offset?: number, tailBytes = 12000) => {
  const sp = new URLSearchParams()
  if (logFileName) sp.set('log_file_name', logFileName)
  if (offset != null) sp.set('offset', String(offset))
  sp.set('tail_bytes', String(tailBytes))
  return request<TaskLogs>(`/api/tasks/${encodeURIComponent(name)}/logs?${sp}`)
}

export function createLogStream(taskName: string): WebSocket {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return new WebSocket(`${proto}//${location.host}/api/tasks/${encodeURIComponent(taskName)}/logs/stream`)
}

export const getMetrics = () => request<SystemMetrics>('/api/system/metrics')

export const getLauncherScripts = () => request<{ items: ScriptCandidate[] }>('/api/launcher/scripts')
export const getLauncherConfigs = (script: string) =>
  request<LauncherConfigsResponse>(`/api/launcher/configs?script=${encodeURIComponent(script)}`)
export const getLauncherWorkspaces = (script: string, config?: string) => {
  const sp = new URLSearchParams({ script })
  if (config) sp.set('config', config)
  return request<{ items: WorkspaceCandidate[] }>(`/api/launcher/workspaces?${sp}`)
}

export const validateLauncherPath = (kind: 'python' | 'shell' | 'config', path: string) =>
  request<PathValidationResult>(
    `/api/launcher/validate-path?kind=${encodeURIComponent(kind)}&path=${encodeURIComponent(path)}`,
  )

export const openLauncherWorkspace = (scriptPath: string, configPath?: string) =>
  request<WorkspaceInfo>('/api/launcher/open', {
    method: 'POST',
    body: JSON.stringify({ script_path: scriptPath, config_path: configPath }),
  })

export const pickLauncherScriptPath = () =>
  request<WorkspaceCandidate>('/api/launcher/pick-script-path', { method: 'POST' })

export const pickLauncherScript = () =>
  request<WorkspaceInfo>('/api/launcher/pick-script', { method: 'POST' })

export const pickLauncherShellRoot = () =>
  request<WorkspaceInfo>('/api/launcher/pick-shell-root', { method: 'POST' })

export const openLauncherShellRoot = (path: string) =>
  request<WorkspaceInfo>('/api/launcher/open-shell-root', {
    method: 'POST',
    body: JSON.stringify({ path }),
  })
