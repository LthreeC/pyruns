import { useEffect, useRef, useState, type ReactNode } from 'react'
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
      className={`fixed inset-0 z-50 m-auto w-full ${widthClass} rounded-md border border-border-subtle bg-surface-raised p-0 shadow-md backdrop:bg-black/50`}
      aria-modal="true"
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
        className="p-6"
        onMouseDown={() => {
          backdropPointerStartedRef.current = false
        }}
        onClick={event => event.stopPropagation()}
      >
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-txt-primary">{title}</h3>
          <button
            type="button"
            onClick={handleCancel}
            disabled={pending}
            aria-label="Close dialog"
            className="rounded-md p-1 text-txt-tertiary transition-colors hover:bg-surface-hover hover:text-txt-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
        {description && <p className="mb-4 text-xs text-txt-secondary leading-relaxed">{description}</p>}
        {children}
        <div className="flex justify-end gap-2 mt-5">
          <button
            type="button"
            onClick={handleCancel}
            disabled={pending}
            className="rounded-md px-3.5 py-2 text-xs text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary disabled:cursor-not-allowed disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={handleConfirm}
            disabled={pending}
            className={`inline-flex min-w-20 items-center justify-center gap-1.5 rounded-md px-3.5 py-2 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${
              confirmVariant === 'danger'
                ? 'border border-rose-500/20 text-rose-400 hover:bg-rose-500/10'
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
