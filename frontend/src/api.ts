import type {
  Dashboard,
  GeneratorPreview,
  GeneratorResult,
  Task,
  TaskLogs,
  TaskPage,
  TaskSortMode,
  TemplateContent,
  WorkspaceInfo,
  SystemMetrics,
  ScriptCandidate,
  ConfigCandidate,
  LauncherConfigsResponse,
  WorkspaceCandidate,
  GeneratorMode,
  PathValidationResult,
  RuntimeInfo,
  GpuSchedulerSettings,
  SystemInfo,
  UiUpdateResponse,
  UiVersionCheck,
} from './types'

const BASE = ''

export class ApiError extends Error {
  readonly status: number
  readonly body: unknown

  constructor(status: number, message: string, body: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.body = body
  }
}

type UnauthorizedListener = (error: ApiError) => void

const unauthorizedListeners = new Set<UnauthorizedListener>()
let authorizationEpoch = 0

export function beginAuthorizationAttempt() {
  authorizationEpoch += 1
}

export function subscribeUnauthorized(listener: UnauthorizedListener) {
  unauthorizedListeners.add(listener)
  return () => {
    unauthorizedListeners.delete(listener)
  }
}

function errorMessage(body: unknown, status: number) {
  if (
    body
    && typeof body === 'object'
    && 'detail' in body
    && typeof body.detail === 'string'
    && body.detail.trim()
  ) {
    return body.detail
  }
  return `HTTP ${status}`
}

async function responseError(res: Response, requestAuthorizationEpoch: number) {
  const body: unknown = await res.json().catch(() => undefined)
  const error = new ApiError(res.status, errorMessage(body, res.status), body)
  if (res.status === 401 && requestAuthorizationEpoch === authorizationEpoch) {
    authorizationEpoch += 1
    for (const listener of unauthorizedListeners) {
      try {
        listener(error)
      } catch {
        // The request must still reject with the original authentication error.
      }
    }
  }
  return error
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const requestAuthorizationEpoch = authorizationEpoch
  const res = await fetch(`${BASE}${url}`, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  if (!res.ok) {
    throw await responseError(res, requestAuthorizationEpoch)
  }
  return res.json()
}

export const getWorkspace = () => request<WorkspaceInfo>('/api/workspace')
export const setRunRoot = (path: string) =>
  request<WorkspaceInfo>('/api/workspace/run-root', { method: 'POST', body: JSON.stringify({ path }) })
export const openShellWorkspace = () =>
  request<WorkspaceInfo>('/api/workspace/shell', { method: 'POST' })
export const getRuntimeInfo = () => request<RuntimeInfo>('/api/runtime')
export const updateRuntimeInfo = (payload: Partial<{
  python_executable: string
  conda_env: string
  conda_executable: string
  global_env: Record<string, string>
  global_env_text: string
  gpu_scheduler?: Partial<GpuSchedulerSettings>
}>, refreshProviders = false) =>
  request<RuntimeInfo>(`/api/runtime?refresh_providers=${refreshProviders}`, { method: 'PATCH', body: JSON.stringify(payload) })

export const getTemplates = () => request<{ items: { value: string; label: string }[] }>('/api/templates')
export const getTemplateContent = (value: string) =>
  request<TemplateContent>(`/api/templates/content?value=${encodeURIComponent(value)}`)
export const pickGeneratorShellFile = () =>
  request<TemplateContent>('/api/generator/pick-shell-file', { method: 'POST' })

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
  summary?: boolean
  compact?: boolean
  sort?: TaskSortMode
} = {}) => {
  const sp = new URLSearchParams()
  if (params.query) sp.set('query', params.query)
  if (params.status && params.status !== 'All') sp.set('status', params.status)
  if (params.offset != null) sp.set('offset', String(params.offset))
  if (params.limit != null) sp.set('limit', String(params.limit))
  if (params.refresh != null) sp.set('refresh', String(params.refresh))
  if (params.summary != null) sp.set('summary', String(params.summary))
  if (params.compact != null) sp.set('compact', String(params.compact))
  if (params.sort) sp.set('sort', params.sort)
  return request<TaskPage>(`/api/tasks?${sp}`)
}

export const getTask = (name: string, refresh = true) =>
  request<Task>(`/api/tasks/${encodeURIComponent(name)}?refresh=${refresh}`)

export function createTaskEventStream(): WebSocket {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  return new WebSocket(`${proto}//${location.host}/api/tasks/events`)
}

export const batchRunTasks = (taskNames: string[], maxWorkers?: number) =>
  request<{ count: number; items: Task[]; skipped: string[] }>('/api/tasks/batch/run', {
    method: 'POST',
    body: JSON.stringify({ task_names: taskNames, max_workers: maxWorkers }),
  })

export const batchDeleteTasks = (taskNames: string[]) =>
  request<{ count: number; deleted: string[] }>('/api/tasks/batch/delete', {
    method: 'POST',
    body: JSON.stringify({ task_names: taskNames }),
  })

export async function exportTasksCsv(taskNames: string[]): Promise<Blob> {
  const requestAuthorizationEpoch = authorizationEpoch
  const res = await fetch(`${BASE}/api/tasks/export/csv`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ task_names: taskNames }),
  })
  if (!res.ok) {
    throw await responseError(res, requestAuthorizationEpoch)
  }
  return res.blob()
}

export const runTask = (name: string) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/run`, {
    method: 'POST',
  })

export const cancelTask = (name: string) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/cancel`, { method: 'POST' })

export const pinTask = (name: string, pinned?: boolean) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/pin`, {
    method: 'POST',
    body: JSON.stringify({ pinned }),
  })

export const reorderTasks = (items: { name: string; pinned: boolean }[]) =>
  request<{ count: number; items: Task[] }>('/api/tasks/reorder', {
    method: 'POST',
    body: JSON.stringify({ items }),
  })

export const updateNotes = (name: string, notes: string, expectedNotes: string) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/notes`, {
    method: 'PATCH',
    body: JSON.stringify({ notes, expected_notes: expectedNotes }),
  })

export const updateEnv = (name: string, env: Record<string, any>, expectedEnv: Record<string, any>) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/env`, {
    method: 'PATCH',
    body: JSON.stringify({ env, expected_env: expectedEnv }),
  })

export const renameTask = (name: string, newName: string) =>
  request<{ ok: boolean; task: Task }>(`/api/tasks/${encodeURIComponent(name)}/rename`, {
    method: 'POST',
    body: JSON.stringify({ new_name: newName }),
  })

export const getTaskLogs = (name: string, options: {
  logFileName?: string
  offset?: number
  logIdentity?: string
  tailBytes?: number
  tailLines?: number
  chunkSize?: number
} = {}) => {
  const sp = new URLSearchParams()
  if (options.logFileName) sp.set('log_file_name', options.logFileName)
  if (options.offset != null) sp.set('offset', String(options.offset))
  if (options.logIdentity) sp.set('log_identity', options.logIdentity)
  if (options.tailBytes != null) sp.set('tail_bytes', String(options.tailBytes))
  if (options.tailLines != null) sp.set('tail_lines', String(options.tailLines))
  if (options.chunkSize != null) sp.set('chunk_size', String(options.chunkSize))
  return request<TaskLogs>(`/api/tasks/${encodeURIComponent(name)}/logs?${sp}`)
}

export function createLogStream(taskName: string, options: {
  logFileName?: string
  offset?: number
  logIdentity?: string
} = {}): WebSocket {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const sp = new URLSearchParams()
  if (options.logFileName) sp.set('log_file_name', options.logFileName)
  if (options.offset != null) sp.set('offset', String(options.offset))
  if (options.logIdentity) sp.set('log_identity', options.logIdentity)
  const query = sp.toString()
  return new WebSocket(`${proto}//${location.host}/api/tasks/${encodeURIComponent(taskName)}/logs/stream${query ? `?${query}` : ''}`)
}

export const getMetrics = (includeProcesses = false) => request<SystemMetrics>(
  `/api/system/metrics?include_processes=${includeProcesses ? 'true' : 'false'}`,
)

export const getSystemInfo = () => request<SystemInfo>('/api/system/info')

export const checkPyrunsUpdate = () => request<UiVersionCheck>('/api/system/update/check')

export const updatePyruns = () => request<UiUpdateResponse>('/api/system/update', {
  method: 'POST',
})

export const restartPyruns = () => request<UiUpdateResponse>('/api/system/restart', {
  method: 'POST',
})

export const getLauncherScripts = () => request<{ items: ScriptCandidate[] }>('/api/launcher/scripts')
export const getLauncherConfigs = (script: string) =>
  request<LauncherConfigsResponse>(`/api/launcher/configs?script=${encodeURIComponent(script)}`)
export const getLauncherWorkspaces = (script: string, config?: string) => {
  const sp = new URLSearchParams({ script })
  if (config) sp.set('config', config)
  return request<{ items: WorkspaceCandidate[] }>(`/api/launcher/workspaces?${sp}`)
}

export const validateLauncherPath = (kind: 'python' | 'shell' | 'config', path: string, script?: string) => {
  const sp = new URLSearchParams({ kind, path })
  if (script) sp.set('script', script)
  return request<PathValidationResult>(`/api/launcher/validate-path?${sp}`)
}

export const openLauncherWorkspace = (scriptPath: string, configPath?: string) =>
  request<WorkspaceInfo>('/api/launcher/open', {
    method: 'POST',
    body: JSON.stringify({ script_path: scriptPath, config_path: configPath }),
  })

export const pickLauncherScriptPath = () =>
  request<WorkspaceCandidate>('/api/launcher/pick-script-path', { method: 'POST' })

export const pickLauncherConfigPath = (scriptPath: string) =>
  request<ConfigCandidate>('/api/launcher/pick-config-path', {
    method: 'POST',
    body: JSON.stringify({ script_path: scriptPath }),
  })

export const pickLauncherScript = () =>
  request<WorkspaceInfo>('/api/launcher/pick-script', { method: 'POST' })

export const pickLauncherShellRoot = () =>
  request<WorkspaceInfo>('/api/launcher/pick-shell-root', { method: 'POST' })

export const openLauncherShellRoot = (path: string) =>
  request<WorkspaceInfo>('/api/launcher/open-shell-root', {
    method: 'POST',
    body: JSON.stringify({ path }),
  })
