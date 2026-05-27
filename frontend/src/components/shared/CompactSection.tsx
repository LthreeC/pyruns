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
        'space-y-1.5',
        accent ? 'text-accent' : 'text-txt-primary',
        className,
      )}
    >
      <div className="flex items-center gap-1.5 px-0.5 py-0.5">
        {icon}
        <div className="min-w-0">
          <div className={clsx('text-xs font-medium', accent ? 'text-accent' : 'text-txt-primary')}>{title}</div>
          {subtitle && <div className="text-2xs text-txt-tertiary">{subtitle}</div>}
        </div>
      </div>
      <div className={clsx('text-txt-primary', bodyClassName || 'pt-1')}>{children}</div>
    </section>
  )
}
