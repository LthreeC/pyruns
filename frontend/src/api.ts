import type {
  Task, TaskPage, WorkspaceInfo, TemplateContent,
  Dashboard, GeneratorResult, GeneratorPreview,
  TaskLogs, SystemMetrics,
  ScriptCandidate, ConfigCandidate, WorkspaceCandidate,
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

/* ── Workspace ── */
export const getWorkspace = () => request<WorkspaceInfo>('/api/workspace')
export const setRunRoot = (path: string) =>
  request<WorkspaceInfo>('/api/workspace/run-root', { method: 'POST', body: JSON.stringify({ path }) })

/* ── Templates ── */
export const getTemplates = () => request<{ items: { value: string; label: string }[] }>('/api/templates')
export const getTemplateContent = (value: string) =>
  request<TemplateContent>(`/api/templates/content?value=${encodeURIComponent(value)}`)

/* ── Generator ── */
export const createTasks = (payload: {
  name_prefix: string; run_mode: string; yaml_text?: string
  args_text?: string; run_script?: string; template_value?: string; append_timestamp?: boolean
}) => request<GeneratorResult>('/api/generator/create', { method: 'POST', body: JSON.stringify(payload) })

export const previewTasks = (payload: {
  run_mode: string; yaml_text?: string; args_text?: string
  run_script?: string; template_value?: string
}) => request<GeneratorPreview>('/api/generator/preview', { method: 'POST', body: JSON.stringify(payload) })

/* ── Dashboard ── */
export const getDashboard = (refresh = true, recentLimit = 6) =>
  request<Dashboard>(`/api/dashboard?refresh=${refresh}&recent_limit=${recentLimit}`)

/* ── Tasks ── */
export const getTasks = (params: {
  query?: string; status?: string; offset?: number; limit?: number; refresh?: boolean
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
    method: 'POST', body: JSON.stringify({ task_names: taskNames }),
  })

export const runTask = (name: string, executionMode?: string) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/run`, {
    method: 'POST', body: JSON.stringify({ execution_mode: executionMode }),
  })

export const cancelTask = (name: string) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/cancel`, { method: 'POST' })

export const pinTask = (name: string, pinned?: boolean) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/pin`, {
    method: 'POST', body: JSON.stringify({ pinned }),
  })

export const updateNotes = (name: string, notes: string) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/notes`, {
    method: 'PATCH', body: JSON.stringify({ notes }),
  })

export const updateEnv = (name: string, env: Record<string, any>) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/env`, {
    method: 'PATCH', body: JSON.stringify({ env }),
  })

export const renameTask = (name: string, newName: string) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/rename`, {
    method: 'POST', body: JSON.stringify({ new_name: newName }),
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

/* ── System ── */
export const getMetrics = () => request<SystemMetrics>('/api/system/metrics')

/* ── Launcher ── */
export const getLauncherScripts = () => request<{ items: ScriptCandidate[] }>('/api/launcher/scripts')
export const getLauncherConfigs = (script: string) =>
  request<{ items: ConfigCandidate[] }>(`/api/launcher/configs?script=${encodeURIComponent(script)}`)
export const getLauncherWorkspaces = (script: string, config?: string) => {
  const sp = new URLSearchParams({ script })
  if (config) sp.set('config', config)
  return request<{ items: WorkspaceCandidate[] }>(`/api/launcher/workspaces?${sp}`)
}
export const openLauncherWorkspace = (scriptPath: string, configPath?: string) =>
  request<WorkspaceInfo>('/api/launcher/open', {
    method: 'POST', body: JSON.stringify({ script_path: scriptPath, config_path: configPath }),
  })
