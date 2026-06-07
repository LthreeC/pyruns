import { useEffect } from 'react'
import { AlertTriangle, CheckCircle2, Info, X } from 'lucide-react'
import clsx from 'clsx'
import { useToastStore, type ToastItem } from '@/store'

const TOAST_TIMEOUT_MS: Record<ToastItem['tone'], number> = {
  success: 3200,
  info: 4200,
  error: 6200,
}

const TOAST_STYLES: Record<ToastItem['tone'], string> = {
  success: 'border-emerald-500/20 text-emerald-300',
  info: 'border-accent/20 text-accent',
  error: 'border-rose-500/20 text-rose-300',
}

const ICONS = {
  success: CheckCircle2,
  info: Info,
  error: AlertTriangle,
}

function ToastCard({ toast, onDismiss }: { toast: ToastItem; onDismiss: (id: number) => void }) {
  const Icon = ICONS[toast.tone]

  useEffect(() => {
    const timer = window.setTimeout(() => onDismiss(toast.id), TOAST_TIMEOUT_MS[toast.tone])
    return () => window.clearTimeout(timer)
  }, [onDismiss, toast.id, toast.tone])

  return (
    <div
      role={toast.tone === 'error' ? 'alert' : 'status'}
      aria-live={toast.tone === 'error' ? 'assertive' : 'polite'}
      className={clsx(
        'pointer-events-auto flex w-[min(380px,calc(100vw-2rem))] items-start gap-2 rounded-md border px-3 py-2.5 shadow-md',
        'bg-surface-raised',
        TOAST_STYLES[toast.tone],
      )}
    >
      <Icon className="mt-0.5 h-4 w-4 flex-none" />
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs font-semibold">{toast.title}</div>
        {toast.detail && (
          <div className="mt-0.5 max-h-8 overflow-hidden text-2xs leading-4 text-txt-secondary" title={toast.detail}>
            {toast.detail}
          </div>
        )}
      </div>
      <button
        type="button"
        onClick={() => onDismiss(toast.id)}
        className="rounded-md p-1 text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/35"
        aria-label="Dismiss notification"
      >
        <X className="h-3.5 w-3.5" />
      </button>
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
    <div className="pointer-events-none fixed bottom-3 right-3 z-[140] flex max-h-[calc(100vh-1.5rem)] flex-col-reverse gap-2 overflow-hidden sm:bottom-4 sm:right-4 sm:max-h-[calc(100vh-2rem)]">
      {toasts.map(toast => (
        <ToastCard key={toast.id} toast={toast} onDismiss={dismiss} />
      ))}
    </div>
  )
}
