import type { ReactNode } from 'react'
import clsx from 'clsx'

interface Props {
  title: string
  subtitle?: string
  icon?: ReactNode
  accent?: boolean
  className?: string
  bodyClassName?: string
  children: ReactNode
}

export default function CompactSection({
  title,
  subtitle,
  icon,
  accent = false,
  className,
  bodyClassName,
  children,
}: Props) {
  return (
    <section
      className={clsx(
        'overflow-hidden rounded-lg border',
        accent ? 'border-accent/20 bg-accent/5' : 'border-border-subtle bg-surface-raised/40',
        className,
      )}
    >
      <div className="flex items-center gap-2 border-b border-border-subtle px-3 py-2">
        {icon}
        <div className="min-w-0">
          <div className={clsx('text-sm font-medium', accent ? 'text-accent' : 'text-txt-primary')}>{title}</div>
          {subtitle && <div className="text-2xs text-txt-tertiary">{subtitle}</div>}
        </div>
      </div>
      <div className={clsx('p-2.5', bodyClassName)}>{children}</div>
    </section>
  )
}
