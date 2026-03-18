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
