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
  primary: 'border border-accent bg-accent text-white shadow-sm shadow-accent/20 hover:bg-accent-hover',
  success: 'border border-emerald-700/80 bg-emerald-600 text-white shadow-sm shadow-emerald-950/30 hover:bg-emerald-500',
  danger: 'border border-rose-800/80 bg-rose-600 text-white shadow-sm shadow-rose-950/30 hover:bg-rose-500',
  ghost: 'border border-border-subtle bg-transparent text-txt-secondary hover:bg-surface-overlay hover:text-txt-primary',
  accentTint: 'border border-accent/25 bg-accent/8 text-accent hover:bg-accent/12',
}

const SIZE_STYLES: Record<ActionButtonSize, string> = {
  sm: 'gap-1.5 rounded-md px-3 py-1.5 text-xs',
  md: 'gap-2 rounded-lg px-3.5 py-2 text-sm',
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
