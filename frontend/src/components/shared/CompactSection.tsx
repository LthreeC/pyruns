import type { ReactNode } from 'react'
import clsx from 'clsx'

interface Props {
  title: string
  subtitle?: string
  count?: number | string
  icon?: ReactNode
  accent?: boolean
  className?: string
  bodyClassName?: string
  children: ReactNode
}

export default function CompactSection({
  title,
  subtitle,
  count,
  icon,
  accent = false,
  className,
  bodyClassName,
  children,
}: Props) {
  return (
    <section
      className={clsx(
        'space-y-1.5',
        accent ? 'text-accent' : 'text-txt-primary',
        className,
      )}
    >
      <div className="flex items-center gap-1.5 px-0.5 py-0.5">
        {icon}
        <div className="min-w-0">
          <div className="flex min-w-0 items-center gap-1.5">
            <div className={clsx('truncate text-xs font-medium', accent ? 'text-accent' : 'text-txt-primary')}>{title}</div>
            {count !== undefined && (
              <span
                className={clsx(
                  'flex-none rounded-md px-1.5 py-0.5 text-2xs font-medium',
                  accent ? 'bg-accent/10 text-accent' : 'bg-surface-overlay text-txt-secondary',
                )}
              >
                {count}
              </span>
            )}
          </div>
          {subtitle && <div className="text-2xs text-txt-tertiary">{subtitle}</div>}
        </div>
      </div>
      <div className={clsx('text-txt-primary', bodyClassName || 'pt-1')}>{children}</div>
    </section>
  )
}
