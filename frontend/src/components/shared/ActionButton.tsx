import type { ButtonHTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'

type ActionButtonVariant = 'primary' | 'success' | 'danger' | 'ghost' | 'accentTint'
type ActionButtonSize = 'sm' | 'md'

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  icon?: ReactNode
  variant?: ActionButtonVariant
  size?: ActionButtonSize
  children: ReactNode
}

const VARIANT_STYLES: Record<ActionButtonVariant, string> = {
  primary: 'bg-accent text-white hover:bg-accent-hover',
  success: 'bg-emerald-600 text-white hover:bg-emerald-500',
  danger: 'bg-rose-600 text-white hover:bg-rose-500',
  ghost: 'bg-transparent text-txt-secondary hover:bg-surface-overlay hover:text-txt-primary',
  accentTint: 'bg-accent/8 text-accent hover:bg-accent/12',
}

const SIZE_STYLES: Record<ActionButtonSize, string> = {
  sm: 'gap-1.5 rounded-md px-3 py-1.5 text-xs',
  md: 'gap-2 rounded-md px-3.5 py-2 text-sm',
}

export default function ActionButton({
  icon,
  variant = 'ghost',
  size = 'sm',
  children,
  className,
  ...props
}: Props) {
  return (
    <button
      type="button"
      className={clsx(
        'inline-flex items-center justify-center font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-40',
        VARIANT_STYLES[variant],
        SIZE_STYLES[size],
        className,
      )}
      {...props}
    >
      {icon}
      <span>{children}</span>
    </button>
  )
}
