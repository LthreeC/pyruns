export type TaskKind = 'python' | 'shell'
export type WorkspaceKind = 'script' | 'shell'
export type GeneratorMode = 'form' | 'yaml' | 'shell'
export type TaskSortMode = 'priority' | 'manual' | 'activity_desc' | 'activity_asc' | 'name_asc' | 'name_desc'

export interface GPUWaitDeviceStatus {
  index: number
  uuid?: string
  name?: string
  eligible: boolean
  reason?: string
  memory_used_pct?: number | null
  free_memory_gb?: number | null
  compute_util_pct?: number | null
}

export interface GPUWaitStatus {
  state: 'waiting' | 'assigned' | 'timed_out' | string
  run_index?: number
  started_at?: number | string | null
  deadline_at?: number | string | null
  waited_seconds?: number
  remaining_seconds?: number | null
  max_wait_seconds?: number
  requested_gpu_count?: number
  eligible_gpu_count?: number
  total_gpu_count?: number
  reason?: string
  updated_at?: number | string | null
  devices?: GPUWaitDeviceStatus[]
}

export type TaskSearchField = 'name' | 'notes' | 'config' | 'script' | 'env' | 'log'
export type TaskSearchScope = 'all' | TaskSearchField
export interface TaskSearchOptions {
  matchCase: boolean
  wholeWord: boolean
  useRegex: boolean
}

export interface TaskSearchMatch {
  field: TaskSearchField
  location: string
  snippet: string
  match_start: number
  match_end: number
  log_file?: string
  log_identity?: string
  line?: number
  offset?: number
}

export interface TaskGpuAssignment {
  task_name?: string
  run_index?: number
  gpu_ids?: number[]
  cuda_visible_devices?: string
  env?: Record<string, string>
  waited_seconds?: number
}

export interface RunEnvironment {
  host: string
  system: string
  launcher: string
  conda_env?: string
  cuda_visible_devices: string | null
  assigned_gpu_ids: number[]
  gpu_scope: 'assigned' | 'visible' | 'detected' | 'disabled'
  gpu_status: 'ok' | 'unavailable'
  gpus: { index: number; uuid: string; name: string; memory_total_mb: number | null }[]
}

export interface Task {
  name: string
  display_name?: string
  status: 'pending' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled'
  dir: string
  task_identity?: string | null
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
  durations?: (number | null)[]
  exit_codes?: (number | null)[]
  source_states?: string[]
  run_environments?: (RunEnvironment | null)[]
  run_statuses?: string[]
  pid_create_times?: (number | null)[]
  progress: number
  run_index: number
  preview_text: string
  search_text: string
  search_matches?: TaskSearchMatch[]
  search_match_count?: number
  records: any[]
  tracks: any[]
  gpu_wait?: GPUWaitStatus | null
  script?: string | null
  command_mode?: string | null
  cmd?: string | string[] | null
  workdir?: string | null
  shell_executable?: string | null
  shell_kind?: string | null
  runner_id?: string | null
  runner_host?: string | null
  runner_location?: 'local' | 'remote' | 'unknown'
  local_process_verified?: boolean
  task_storage?: 'shared' | 'local' | 'remote'
  task_source?: string
  task_source_host?: string
  runner_lease_expired?: boolean | null
  lease_heartbeat?: number | null
  cancel_requested_at?: number | string | null
  lease_until?: number | string | null
  queued_at?: string | number | null
  launch_command?: string | null
  launch_workdir?: string | null
  launch_started_at?: number | null
  launch_run_index?: number | null
  _gpu_assignment?: TaskGpuAssignment | null
  _load_error?: string
}

export interface TaskPage {
  task_sources?: { id: string; host: string }[]
  search_errors?: string[]
  search_limit_hit?: boolean
  search_complete?: boolean
  items: Task[]
  total: number
  offset: number
  limit: number
  has_more: boolean
  status_counts?: TaskStatusCounts
}

export type TaskStatusCounts = Record<Task['status'], number>

export interface WorkspaceInfo {
  run_root: string
  device_host?: string
  working_root?: string
  native_file_picker?: boolean
  tasks_dir: string
  script_path: string
  script_name: string
  config_default_source?: string
  config_default_source_name?: string
  default_template?: TemplateOption | null
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
  selection_mode: 'auto' | 'specified'
  gpus_per_task: number
  device_ids: number[]
  min_free_memory_gb: number
  min_idle_pct: number
  stable_seconds: number
  max_wait_seconds: number
  respect_cuda_visible_devices: boolean
  require_same_gpu_model: boolean
}

export interface RuntimeInfo {
  task_storage?: 'shared' | 'local'
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

export interface UiUpdateResult {
  ok: boolean
  previous_version: string
  installed_version: string
  exit_code: number
  operation?: 'restart'
  error?: string
}

export interface SystemInfo {
  version: string
  installed_version: string
  restart_required: boolean
  instance_id: string
  update_supported: boolean
  restart_supported?: boolean
  update_state: string
  scheduled_operation?: UiScheduledOperation | null
  last_update: UiUpdateResult | null
}

export interface UiUpdateResponse {
  ok: boolean
  instance_id: string
  version: string
  state: string
  operation?: 'upgrade' | 'restart' | null
  target_version?: string
  scheduled_operation?: UiScheduledOperation | null
}

export interface UiScheduledOperation {
  id: string
  operation: 'upgrade' | 'restart'
  target_version: string
  error?: string
}

export interface UiVersionCheck {
  current_version: string
  latest_version: string
  update_available: boolean
  operation?: 'restart'
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
  cancelled: number
  pending: number
}

export interface Dashboard {
  workspace?: WorkspaceInfo
  summary: DashboardSummary
  local_activity?: { running: number; queued: number }
  recent_tasks: Task[]
  template_count?: number
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
  config_text?: string
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
  reset?: boolean
  log_identity?: string
  tail_truncated?: boolean
  tail_limit_bytes?: number
}

export interface LogStreamMessage {
  type: 'chunk' | 'reset' | 'ready' | 'heartbeat' | 'error'
  task_name: string
  content?: string
  detail?: string
  offset?: number
  log_file_name?: string
  log_identity?: string
}

export interface TaskEventMessage {
  type: 'ready' | 'changed' | 'heartbeat'
  revision: number
}

export interface GPUProcessInfo {
  pid: number
  name: string
  user: string
  memory_mb: number | null
}

export interface GPUProcessDetails {
  pid: number
  available: boolean
  user: string
  process_name: string
  status: string
  executable: string
  command_line: string
  command_line_truncated: boolean
  working_directory: string
  created_at: number | null
  host_memory_mb: number | null
  host_memory_percent: number | null
  thread_count: number | null
  parent_pid: number | null
}

export interface GPUMetric {
  id: number
  index: number
  name: string
  uuid: string
  util: number | null
  mem_used: number | null
  mem_total: number | null
  mem_util?: number | null
  mem_free?: number | null
  temperature_c?: number | null
  fan_speed_pct?: number | null
  power_draw_w?: number | null
  power_limit_w?: number | null
  performance_state?: string
  compute_mode?: string
  graphics_clock_mhz?: number | null
  memory_clock_mhz?: number | null
  pci_bus_id?: string
  driver_version?: string
  processes: GPUProcessInfo[]
  processes_error?: string
}

export interface SystemMetrics {
  host?: string
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

export interface RunInput {
  schema_version: 1
  run_index: number
  captured_at: string
  task_kind: string
  payload_file: string
  payload_text: string
  script: string | null
  command: string[] | string | null
  workdir: string
  shell_executable: string | null
  environment: Record<string, string>
  launch_argv: string[]
}

export interface RunInputDiff {
  schema_version: 1
  left: { reference: string; available: boolean }
  right: { reference: string; available: boolean }
  available: boolean
  equal: boolean | null
  changed_fields: string[]
  diff: string
  truncated?: boolean
}

export interface RunSummary {
  task_name: string
  run_index: number
  reference: string
  status: string | null
  start_time: string | number | null
  finish_time: string | number | null
  duration_seconds: number | null
  exit_code: number | null
  source_state: string | null
  environment: RunEnvironment | null
  record: unknown
  input_available: boolean
}

export interface RunComparison {
  schema_version: 1
  left: RunSummary
  right: RunSummary
  input_diff: RunInputDiff
  metric_rows?: { key: string; left: string; right: string; changed: boolean; delta: string | null }[]
}
