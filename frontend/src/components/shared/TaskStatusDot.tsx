import { t } from '@/i18n'
import { taskStatusLabel } from '@/theme/tokens'
import type { Task } from '@/types'

export default function TaskStatusDot({ task, decorative = false }: { task: Task; decorative?: boolean }) {
  const label = decorative ? undefined : t(taskStatusLabel(task))
  return (
    <span
      className="task-status-dot"
      data-task-tone={task.status}
      data-dashed={task.task_storage === 'remote' || undefined}
      aria-hidden={decorative || undefined}
      role={decorative ? undefined : 'img'}
      aria-label={label}
      title={label}
    />
  )
}
