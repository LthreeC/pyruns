import { AlertTriangle, Check, Copy, LoaderCircle } from 'lucide-react'
import { useEffect, useRef, useState, type ButtonHTMLAttributes } from 'react'
import clsx from 'clsx'
import { copyText } from '@/utils/clipboard'

interface Props extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'onClick'> {
  value: string
  label?: string
  size?: 'xs' | 'sm' | 'md'
}

const SIZE_STYLES = {
  xs: 'h-6 w-6',
  sm: 'h-8 w-8',
  md: 'h-9 w-9',
} as const

export default function CopyButton({
  value,
  label = 'Copy to clipboard',
  size = 'sm',
  className,
  disabled,
  ...props
}: Props) {
  const [state, setState] = useState<'idle' | 'copying' | 'copied' | 'failed'>('idle')
  const resetTimerRef = useRef<number | null>(null)
  const available = Boolean(String(value ?? ''))

  useEffect(() => () => {
    if (resetTimerRef.current !== null) {
      window.clearTimeout(resetTimerRef.current)
    }
  }, [])

  const handleCopy = async () => {
    if (!available || disabled || state === 'copying') {
      return
    }
    setState('copying')
    const copied = await copyText(value)
    setState(copied ? 'copied' : 'failed')
    if (resetTimerRef.current !== null) {
      window.clearTimeout(resetTimerRef.current)
    }
    resetTimerRef.current = window.setTimeout(() => {
      resetTimerRef.current = null
      setState('idle')
    }, copied ? 1400 : 2200)
  }

  const statusLabel = state === 'copied'
    ? 'Copied'
    : state === 'failed'
      ? 'Copy failed'
      : state === 'copying'
        ? 'Copying'
        : label
  return (
    <button
      type="button"
      {...props}
      disabled={disabled || !available || state === 'copying'}
      onClick={handleCopy}
      className={clsx(
        'touch-target inline-flex flex-none items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/35 disabled:cursor-not-allowed disabled:opacity-45',
        SIZE_STYLES[size],
        state === 'copied' && 'text-emerald-700 dark:text-emerald-300',
        state === 'failed' && 'text-rose-700 dark:text-rose-300',
        className,
      )}
      aria-label={statusLabel}
      title={statusLabel}
    >
      {state === 'copying'
        ? <LoaderCircle className="h-3.5 w-3.5 motion-safe:animate-spin" />
        : state === 'copied'
          ? <Check className="h-3.5 w-3.5" />
          : state === 'failed'
            ? <AlertTriangle className="h-3.5 w-3.5" />
            : <Copy className="h-3.5 w-3.5" />}
    </button>
  )
}
