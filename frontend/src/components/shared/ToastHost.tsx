import { useEffect, useState } from 'react'
import { AlertTriangle, CheckCircle2, ChevronDown, Info, X } from 'lucide-react'
import clsx from 'clsx'
import { useToastStore, type ToastItem } from '@/store'

const TOAST_TIMEOUT_MS: Record<ToastItem['tone'], number> = {
  success: 3200,
  info: 4200,
  error: 6200,
}

const TOAST_STYLES: Record<ToastItem['tone'], string> = {
  success: 'border-emerald-500/20 text-emerald-700 dark:text-emerald-300',
  info: 'border-accent/20 text-accent',
  error: 'border-rose-500/20 text-rose-700 dark:text-rose-300',
}

const ICONS = {
  success: CheckCircle2,
  info: Info,
  error: AlertTriangle,
}

function ToastCard({ toast, onDismiss }: { toast: ToastItem; onDismiss: (id: number) => void }) {
  const Icon = ICONS[toast.tone]
  const [hovered, setHovered] = useState(false)
  const [focused, setFocused] = useState(false)
  const [expanded, setExpanded] = useState(false)

  useEffect(() => {
    if (hovered || focused) return
    const timer = window.setTimeout(() => onDismiss(toast.id), TOAST_TIMEOUT_MS[toast.tone])
    return () => window.clearTimeout(timer)
  }, [focused, hovered, onDismiss, toast.id, toast.tone])

  return (
    <div
      role={toast.tone === 'error' ? 'alert' : 'status'}
      aria-live={toast.tone === 'error' ? 'assertive' : 'polite'}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      onFocus={() => setFocused(true)}
      onBlur={event => {
        if (!event.currentTarget.contains(event.relatedTarget)) setFocused(false)
      }}
      className={clsx(
        'flex w-[min(380px,calc(100vw-2rem))] items-start gap-2 rounded-md border px-3 py-2.5 shadow-md',
        expanded ? 'pointer-events-auto' : 'pointer-events-none',
        'bg-surface-raised',
        TOAST_STYLES[toast.tone],
      )}
    >
      <Icon className="mt-0.5 h-4 w-4 flex-none" />
      <div className="min-w-0 flex-1">
        <div className="break-words text-xs font-semibold">{toast.title}</div>
        {toast.detail && (
          <div
            tabIndex={expanded ? 0 : undefined}
            aria-label="Notification details"
            className={clsx(
              'mt-0.5 whitespace-pre-wrap break-words text-xs leading-5 text-txt-secondary',
              expanded ? 'max-h-32 overflow-y-auto' : 'line-clamp-2',
            )}
          >
            {toast.detail}
          </div>
        )}
      </div>
      <div className="flex flex-none flex-col">
        <button
          type="button"
          onClick={() => onDismiss(toast.id)}
          className="touch-target pointer-events-auto inline-flex h-11 w-11 flex-none items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/35 sm:h-8 sm:w-8"
          aria-label="Dismiss notification"
        >
          <X className="h-3.5 w-3.5" />
        </button>
        {toast.detail && (
          <button
            type="button"
            onClick={() => {
              setExpanded(value => !value)
              setHovered(false)
            }}
            aria-expanded={expanded}
            aria-label={expanded ? 'Collapse notification details' : 'Expand notification details'}
            className="touch-target pointer-events-auto inline-flex h-11 w-11 items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/35 sm:h-6 sm:w-8"
          >
            <ChevronDown className={clsx('h-3.5 w-3.5 transition-transform', expanded && 'rotate-180')} />
          </button>
        )}
      </div>
    </div>
  )
}

export default function ToastHost() {
  const toasts = useToastStore(state => state.toasts)
  const dismiss = useToastStore(state => state.dismiss)

  if (!toasts.length) {
    return null
  }

  return (
    <div className="pointer-events-none fixed bottom-3 right-3 z-[140] flex max-h-[calc(100dvh-1.5rem)] flex-col-reverse gap-2 overflow-hidden sm:bottom-4 sm:right-4 sm:max-h-[calc(100dvh-2rem)]">
      {toasts.map(toast => (
        <ToastCard key={toast.id} toast={toast} onDismiss={dismiss} />
      ))}
    </div>
  )
}
