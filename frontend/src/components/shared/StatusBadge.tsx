import clsx from 'clsx'
import { isTaskStopping, STATUS_COLORS, taskStatusLabel } from '@/theme/tokens'
import type { Task } from '@/types'
import { useTranslation } from '@/i18n'

interface Props {
  task: Task
  size?: 'sm' | 'md'
}

export default function StatusBadge({ task, size = 'sm' }: Props) {
  const { t } = useTranslation()
  const c = STATUS_COLORS[task.status] || STATUS_COLORS.pending
  const stopping = isTaskStopping(task)
  return (
    <span className={clsx(
      'inline-flex items-center gap-1.5 rounded-md font-medium capitalize',
      stopping ? 'bg-accent/10 text-accent-ink' : [c.bg, c.text],
      size === 'sm' ? 'px-2.5 py-1 text-2xs' : 'px-3 py-1.5 text-xs',
    )}>
      <span aria-hidden="true" className={clsx('size-1.5 rounded-full', stopping ? 'border border-current' : c.dot)} />
      {t(taskStatusLabel(task))}
    </span>
  )
}
