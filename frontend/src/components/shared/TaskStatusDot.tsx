import { t } from '@/i18n'
import { taskStatusLabel } from '@/theme/tokens'
import type { Task } from '@/types'

export default function TaskStatusDot({ task, decorative = false, logActivity = false }: {
  task: Task; decorative?: boolean; logActivity?: boolean
}) {
  const active = task.status === 'running' && logActivity
  const label = decorative ? undefined : t(taskStatusLabel(task))
  const description = active ? t('New log output') : undefined
  return (
    <span
      className="task-status-dot"
      data-task-tone={task.status}
      data-dashed={task.task_storage === 'remote' || undefined}
      data-log-activity={active || undefined}
      aria-hidden={decorative || undefined}
      role={decorative ? undefined : 'img'}
      aria-label={label}
      aria-description={decorative ? undefined : description}
      title={description || label}
    />
  )
}
