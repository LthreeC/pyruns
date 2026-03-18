import clsx from 'clsx'
import { STATUS_COLORS, type TaskStatus } from '@/theme/tokens'

interface Props {
  status: TaskStatus
  size?: 'sm' | 'md'
}

export default function StatusBadge({ status, size = 'sm' }: Props) {
  const c = STATUS_COLORS[status] || STATUS_COLORS.pending
  return (
    <span className={clsx(
      'inline-flex items-center gap-1.5 rounded-full border font-medium capitalize',
      c.bg, c.text,
      c.border,
      size === 'sm' ? 'px-2.5 py-1 text-2xs' : 'px-3 py-1.5 text-xs',
    )}>
      <span className={clsx('w-1.5 h-1.5 rounded-full', c.dot)} />
      {status}
    </span>
  )
}
