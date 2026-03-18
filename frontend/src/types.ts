/* ── Task ── */
export interface Task {
  name: string
  status: 'pending' | 'queued' | 'running' | 'completed' | 'failed'
  dir: string
  config: Record<string, any>
  pinned: boolean
  notes: string
  env: Record<string, string>
  created_at: string
  start_times: string[]
  finish_times: string[]
  pids: number[]
  progress: number
  run_index: number
  run_mode: string
  preview_text: string
  search_text: string
  records: any[]
  tracks: any[]
}

/* ── Paginated task list ── */
export interface TaskPage {
  items: Task[]
  total: number
  offset: number
  limit: number
  has_more: boolean
}

/* ── Workspace ── */
export interface WorkspaceInfo {
  run_root: string
  tasks_dir: string
  script_path: string
  script_name: string
  workspace_ready: boolean
  settings: Record<string, any>
  templates: TemplateOption[]
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
  mode_hint: 'yaml' | 'args'
  args_text: string
  run_script: string
  parsed_config: Record<string, any> | null
}

/* ── Dashboard ── */
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

/* ── Generator ── */
export interface GeneratorResult {
  count: number
  items: Task[]
  recent_tasks: Task[]
  run_mode: string
}

export interface PreviewItem {
  index: number
  preview: string
  config: Record<string, any>
}

export interface GeneratorPreview {
  count: number
  items: PreviewItem[]
  run_mode: string
}

/* ── Logs ── */
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
}

/* ── System ── */
export interface SystemMetrics {
  cpu_percent: number
  mem_percent: number
  gpus: { id: number; name: string; util: number; mem_used: number; mem_total: number }[]
}

/* ── Launcher ── */
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
  kind: 'workspace_default' | 'script_dir'
}

export interface WorkspaceCandidate {
  workspace_path: string
  script_path: string
  script_name: string
  config_path: string
  config_name: string
  exists: boolean
}
