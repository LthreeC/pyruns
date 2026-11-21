import clsx from 'clsx'
import { STATUS_COLORS, taskStatusLabel } from '@/theme/tokens'
import type { Task } from '@/types'
import { useTranslation } from '@/i18n'

interface Props {
  task: Task
  size?: 'sm' | 'md'
}

export default function StatusBadge({ task, size = 'sm' }: Props) {
  const { t } = useTranslation()
  const c = STATUS_COLORS[task.status] || STATUS_COLORS.pending
  return (
    <span className={clsx(
      'inline-flex items-center gap-1.5 rounded-md font-medium capitalize',
      c.bg, c.text,
      size === 'sm' ? 'px-2.5 py-1 text-2xs' : 'px-3 py-1.5 text-xs',
    )}>
      <span className={clsx('w-1.5 h-1.5 rounded-full', c.dot)} />
      {t(taskStatusLabel(task))}
    </span>
  )
}
