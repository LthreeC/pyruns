export const STATUS_COLORS = {
  pending:   { bg: 'bg-gray-500/10',    text: 'text-gray-400',    dot: 'bg-gray-400',    border: 'border-gray-500/20' },
  queued:    { bg: 'bg-blue-500/10',     text: 'text-blue-400',    dot: 'bg-blue-400',    border: 'border-blue-500/20' },
  running:   { bg: 'bg-amber-500/10',    text: 'text-amber-400',   dot: 'bg-amber-400',   border: 'border-amber-500/20' },
  completed: { bg: 'bg-emerald-500/10',  text: 'text-emerald-400', dot: 'bg-emerald-400', border: 'border-emerald-500/20' },
  failed:    { bg: 'bg-rose-500/10',     text: 'text-rose-400',    dot: 'bg-rose-400',    border: 'border-rose-500/20' },
} as const

export type TaskStatus = keyof typeof STATUS_COLORS

export const STATUS_LABELS: Record<TaskStatus, string> = {
  pending: 'Pending',
  queued: 'Queued',
  running: 'Running',
  completed: 'Completed',
  failed: 'Failed',
}

export const ALL_STATUSES: TaskStatus[] = ['pending', 'queued', 'running', 'completed', 'failed']

export const PARAM_TYPE_STYLES = {
  str: 'border-sky-500/25 bg-sky-500/10 text-sky-700 dark:text-sky-300',
  int: 'border-emerald-500/25 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  float: 'border-teal-500/25 bg-teal-500/10 text-teal-700 dark:text-teal-300',
  bool: 'border-fuchsia-500/25 bg-fuchsia-500/10 text-fuchsia-700 dark:text-fuchsia-300',
  list: 'border-violet-500/25 bg-violet-500/10 text-violet-700 dark:text-violet-300',
  null: 'border-slate-500/25 bg-slate-500/10 text-slate-700 dark:text-slate-300',
} as const
