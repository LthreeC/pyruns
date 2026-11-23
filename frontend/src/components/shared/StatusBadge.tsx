import clsx from 'clsx'
import { taskStatusLabel } from '@/theme/tokens'
import type { Task } from '@/types'
import { useTranslation } from '@/i18n'
import TaskStatusDot from './TaskStatusDot'

interface Props {
  task: Task
  size?: 'sm' | 'md'
}

export default function StatusBadge({ task, size = 'sm' }: Props) {
  const { t } = useTranslation()
  return (
    <span data-task-tone={task.status} className={clsx(
      'task-status-badge inline-flex items-center gap-1.5 rounded-md font-medium capitalize',
      size === 'sm' ? 'px-2.5 py-1 text-2xs' : 'px-3 py-1.5 text-xs',
    )}>
      <TaskStatusDot task={task} decorative />
      {t(taskStatusLabel(task))}
    </span>
  )
}
