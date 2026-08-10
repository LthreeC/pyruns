import { useEffect, useId, useRef, useState, type ReactNode } from 'react'
import { Loader2, X } from 'lucide-react'

interface Props {
  open: boolean
  title: string
  description?: string
  confirmLabel?: string
  confirmVariant?: 'danger' | 'primary'
  size?: 'md' | 'lg'
  onConfirm: () => void | Promise<void>
  onCancel: () => void
  children?: ReactNode
}

export default function ConfirmDialog({
  open, title, description, confirmLabel = 'Confirm', size = 'md',
  confirmVariant = 'primary', onConfirm, onCancel, children,
}: Props) {
  const ref = useRef<HTMLDialogElement>(null)
  const backdropPointerStartedRef = useRef(false)
  const [pending, setPending] = useState(false)
  const titleId = useId()
  const descriptionId = useId()
  const widthClass = size === 'lg' ? 'max-w-2xl' : 'max-w-md'

  useEffect(() => {
    const dialog = ref.current
    if (open && dialog && !dialog.open) {
      dialog.showModal()
    } else if (!open) {
      setPending(false)
      if (dialog?.open) {
        dialog.close()
      }
    }
  }, [open])

  if (!open) return null

  const handleCancel = () => {
    if (pending) {
      return
    }
    onCancel()
  }

  const handleConfirm = () => {
    if (pending) {
      return
    }

    const result = onConfirm()
    if (result && typeof result.finally === 'function') {
      setPending(true)
      void result
        .catch(() => undefined)
        .finally(() => setPending(false))
    }
  }

  return (
    <dialog
      ref={ref}
      className={`fixed inset-0 z-50 m-auto max-h-[calc(100dvh-1.5rem)] w-[calc(100vw-1.5rem)] ${widthClass} overflow-hidden rounded-md border border-border-subtle bg-surface-raised p-0 shadow-md backdrop:bg-black/50`}
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      onCancel={event => {
        event.preventDefault()
        handleCancel()
      }}
      onMouseDown={event => {
        backdropPointerStartedRef.current = event.target === event.currentTarget
      }}
      onClick={event => {
        if (backdropPointerStartedRef.current && event.target === event.currentTarget) {
          handleCancel()
        }
        backdropPointerStartedRef.current = false
      }}
      aria-busy={pending || undefined}
    >
      <div
        className="flex max-h-[calc(100dvh-1.5rem)] min-h-0 flex-col"
        onMouseDown={() => {
          backdropPointerStartedRef.current = false
        }}
        onClick={event => event.stopPropagation()}
      >
        <div className="flex flex-none items-center justify-between px-5 pb-3 pt-5 sm:px-6 sm:pt-6">
          <h3 id={titleId} className="text-sm font-semibold text-txt-primary">{title}</h3>
          <button
            type="button"
            onClick={handleCancel}
            disabled={pending}
            aria-label="Close dialog"
            className="touch-target inline-flex h-11 w-11 items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-hover hover:text-txt-primary disabled:cursor-not-allowed disabled:opacity-50 sm:h-8 sm:w-8"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        <div className="min-h-0 overflow-y-auto px-5 sm:px-6">
          {description && <p id={descriptionId} className="mb-4 text-xs text-txt-secondary leading-relaxed">{description}</p>}
          {children}
        </div>
        <div className="mt-4 flex flex-none justify-end gap-2 border-t border-border-subtle px-5 py-4 sm:px-6">
          <button
            type="button"
            onClick={handleCancel}
            disabled={pending}
            className="touch-target min-h-11 rounded-md px-3.5 py-2 text-xs text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary disabled:cursor-not-allowed disabled:opacity-50 sm:min-h-9"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={pending}
            className={`touch-target inline-flex min-h-11 min-w-20 items-center justify-center gap-1.5 rounded-md px-3.5 py-2 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60 sm:min-h-9 ${
              confirmVariant === 'danger'
                ? 'border border-rose-500/20 text-rose-700 hover:bg-rose-500/10 dark:text-rose-300'
                : 'border border-border-subtle text-txt-primary hover:bg-surface-overlay'
            }`}
          >
            {pending && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
            {confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  )
}
