export type TaskKind = 'python' | 'shell'
export type WorkspaceKind = 'script' | 'shell'
export type GeneratorMode = 'form' | 'yaml' | 'shell'

export interface Task {
  name: string
  status: 'pending' | 'queued' | 'running' | 'completed' | 'failed'
  dir: string
  config: Record<string, any>
  config_text: string
  config_file: string
  config_mode?: TaskKind | 'config'
  task_kind: TaskKind
  pinned: boolean
  task_order?: number | null
  notes: string
  env: Record<string, string>
  created_at: string
  start_times: string[]
  finish_times: string[]
  pids: number[]
  source_states?: string[]
  progress: number
  run_index: number
  preview_text: string
  search_text: string
  records: any[]
  tracks: any[]
  _load_error?: string
}

export interface TaskPage {
  items: Task[]
  total: number
  offset: number
  limit: number
  has_more: boolean
}

export interface WorkspaceInfo {
  run_root: string
  working_root?: string
  native_file_picker?: boolean
  tasks_dir: string
  script_path: string
  script_name: string
  config_default_source?: string
  config_default_source_name?: string
  project_root?: string
  workspace_kind: WorkspaceKind
  workspace_ready: boolean
  settings: Record<string, any>
  shell_runtime?: ShellRuntimeInfo
  templates: TemplateOption[]
}

export interface ShellRuntimeInfo {
  mode: 'follow' | 'custom'
  source: string
  terminal_kind: string
  display_name: string
  executable: string
}

export interface RuntimeProviderInfo {
  id: 'conda' | string
  label: string
  available: boolean
}

export interface CondaEnvInfo {
  name: string
  path: string
  python_executable: string
  active?: boolean
}

export interface GpuSchedulerSettings {
  enabled: boolean
  task_mode: 'single' | 'multi'
  gpus_per_task: number
  device_ids: number[]
  memory_used_pct: number
  min_free_memory_gb: number
  compute_used_pct: number
  stable_seconds: number
  max_wait_seconds: number
  max_tasks_per_gpu: number
  respect_cuda_visible_devices: boolean
}

export interface RuntimeInfo {
  python_executable: string
  conda_env: string
  conda_executable: string
  global_env: Record<string, string>
  gpu_scheduler?: GpuSchedulerSettings
  process: {
    python_executable: string
    conda_env: string
    conda_prefix: string
  }
  providers: RuntimeProviderInfo[]
  conda: {
    available: boolean
    executable: string
    envs: CondaEnvInfo[]
    error?: string
  }
}

export interface TemplateOption {
  value: string
  label: string
}

export interface TemplateContent {
  value: string
  label: string
  path: string
  content: string
  read_only: boolean
  mode_hint: 'yaml' | 'shell'
  parsed_config: Record<string, any> | null
}

export interface DashboardSummary {
  total: number
  running: number
  queued: number
  completed: number
  failed: number
  pending: number
}

export interface Dashboard {
  workspace: WorkspaceInfo
  summary: DashboardSummary
  recent_tasks: Task[]
  template_count: number
  active_task: Task | null
}

export interface GeneratorResult {
  count: number
  items: Task[]
  recent_tasks: Task[]
  task_kind: TaskKind
}

export interface PreviewItem {
  index: number
  preview: string
  config: Record<string, any>
}

export interface GeneratorPreview {
  count: number
  items: PreviewItem[]
  task_kind: TaskKind
}

export interface TaskLogs {
  task_name: string
  selected_log: string
  available_logs: string[]
  content: string
  offset: number
}

export interface LogStreamMessage {
  type: 'chunk'
  task_name: string
  content: string
  offset?: number
  log_file_name?: string
}

export interface GPUProcessInfo {
  pid: number
  user: string
  name: string
  memory_mb: number
}

export interface GPUMetric {
  id: number
  index: number
  name: string
  uuid: string
  util: number
  mem_used: number
  mem_total: number
  processes: GPUProcessInfo[]
}

export interface SystemMetrics {
  cpu_percent: number
  mem_percent: number
  gpus: GPUMetric[]
}

export interface ScriptCandidate {
  script_path: string
  script_name: string
  label: string
  workspace_path: string
  source: 'workspace' | 'file' | 'workspace+file'
}

export interface ConfigCandidate {
  path: string
  label: string
  kind: 'workspace_default' | 'script_dir' | 'script_config_dir' | 'manual'
}

export interface LauncherConfigsResponse {
  items: ConfigCandidate[]
  requires_config_template: boolean
  config_source: string
}

export interface WorkspaceCandidate {
  workspace_path: string
  script_path: string
  script_name: string
  config_path: string
  config_name: string
  exists: boolean
}

export interface PathValidationResult {
  ok: boolean
  kind: 'python' | 'shell' | 'config' | string
  normalized_path: string
  path_type: 'file' | 'directory' | ''
  message: string
}
