export const STATUS_COLORS = {
  pending:   { bg: 'bg-gray-500/10',    text: 'text-gray-700 dark:text-gray-300',       dot: 'bg-gray-500',    border: 'border-gray-500/20' },
  queued:    { bg: 'bg-blue-500/10',    text: 'text-blue-700 dark:text-blue-300',       dot: 'bg-blue-500',    border: 'border-blue-500/20' },
  running:   { bg: 'bg-amber-500/10',   text: 'text-amber-800 dark:text-amber-300',     dot: 'bg-amber-500',   border: 'border-amber-500/20' },
  completed: { bg: 'bg-emerald-500/10', text: 'text-emerald-700 dark:text-emerald-300', dot: 'bg-emerald-500', border: 'border-emerald-500/20' },
  failed:    { bg: 'bg-rose-500/10',    text: 'text-rose-700 dark:text-rose-300',       dot: 'bg-rose-500',    border: 'border-rose-500/20' },
  cancelled: { bg: 'bg-slate-500/10',   text: 'text-slate-700 dark:text-slate-300',     dot: 'bg-slate-500',   border: 'border-slate-500/20' },
} as const

export type TaskStatus = keyof typeof STATUS_COLORS

export const STATUS_LABELS: Record<TaskStatus, string> = {
  pending: 'Pending',
  queued: 'Queued',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

export const ALL_STATUSES: TaskStatus[] = ['pending', 'queued', 'running', 'completed', 'failed', 'cancelled']

export const PARAM_TYPE_STYLES = {
  str: 'bg-sky-500/10 text-sky-700 dark:text-sky-300',
  int: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  float: 'bg-teal-500/10 text-teal-700 dark:text-teal-300',
  bool: 'bg-fuchsia-500/10 text-fuchsia-700 dark:text-fuchsia-300',
  list: 'bg-violet-500/10 text-violet-700 dark:text-violet-300',
  null: 'bg-slate-500/10 text-slate-700 dark:text-slate-300',
} as const
