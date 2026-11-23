export const STATUS_LABELS = {
  pending: 'Pending',
  queued: 'Queued',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
} as const

export type TaskStatus = keyof typeof STATUS_LABELS

export const ALL_STATUSES: TaskStatus[] = ['pending', 'queued', 'running', 'completed', 'failed', 'cancelled']

export function isTaskStopping(task: { status: TaskStatus; cancel_requested_at?: number | string | null }) {
  return (task.status === 'running' || task.status === 'queued') && Boolean(task.cancel_requested_at)
}

type TaskStatusDetails = {
  status: TaskStatus
  cancel_requested_at?: number | string | null
  runner_location?: string
  runner_lease_expired?: boolean | null
  task_storage?: string
  local_process_verified?: boolean
}

function taskActivityReport(task: TaskStatusDetails) {
  if (task.status !== 'running' && task.status !== 'queued') return null
  if (task.status === 'running' && task.local_process_verified) return null
  if (task.task_storage === 'remote' || task.runner_lease_expired === true || task.runner_location === 'unknown' ||
    (task.runner_location && task.runner_lease_expired == null)) return 'last'
  return task.runner_location === 'remote' ? 'remote' : null
}

export function taskStatusTone(task: TaskStatusDetails) {
  return isTaskStopping(task) ? 'stopping' : task.status
}

export function taskStatusLabel(task: TaskStatusDetails) {
  if (isTaskStopping(task)) return 'Stopping'
  const report = taskActivityReport(task)
  if (report === 'last') return task.status === 'running' ? 'Last reported running' : 'Last reported queued'
  if (report === 'remote') return task.status === 'running' ? 'Remote reports running' : 'Remote reports queued'
  return STATUS_LABELS[task.status]
}

export const PARAM_TYPE_STYLES = {
  str: 'bg-sky-500/10 text-sky-700 dark:text-sky-300',
  int: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  float: 'bg-teal-500/10 text-teal-700 dark:text-teal-300',
  bool: 'bg-fuchsia-500/10 text-fuchsia-700 dark:text-fuchsia-300',
  list: 'bg-violet-500/10 text-violet-700 dark:text-violet-300',
  null: 'bg-slate-500/10 text-slate-700 dark:text-slate-300',
} as const
