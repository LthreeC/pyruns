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
        'overflow-hidden rounded-md border',
        accent ? 'border-accent/20 bg-accent/5' : 'border-border-subtle bg-surface-raised/40',
        className,
      )}
    >
      <div className="flex items-center gap-1.5 border-b border-border-subtle px-2.5 py-1.5">
        {icon}
        <div className="min-w-0">
          <div className={clsx('text-xs font-medium', accent ? 'text-accent' : 'text-txt-primary')}>{title}</div>
          {subtitle && <div className="text-2xs text-txt-tertiary">{subtitle}</div>}
        </div>
      </div>
      <div className={clsx('p-2', bodyClassName)}>{children}</div>
    </section>
  )
}
