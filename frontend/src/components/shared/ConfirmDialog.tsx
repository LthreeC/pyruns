import { useEffect, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'

interface Props {
  open: boolean
  title: string
  description?: string
  confirmLabel?: string
  confirmVariant?: 'danger' | 'primary'
  size?: 'md' | 'lg'
  onConfirm: () => void
  onCancel: () => void
  children?: ReactNode
}

export default function ConfirmDialog({
  open, title, description, confirmLabel = 'Confirm', size = 'md',
  confirmVariant = 'primary', onConfirm, onCancel, children,
}: Props) {
  const ref = useRef<HTMLDialogElement>(null)
  const widthClass = size === 'lg' ? 'max-w-2xl' : 'max-w-md'

  useEffect(() => {
    if (open) ref.current?.showModal()
    else ref.current?.close()
  }, [open])

  if (!open) return null

  return (
    <dialog
      ref={ref}
      className={`fixed inset-0 z-50 m-auto w-full ${widthClass} rounded-2xl border border-border-subtle bg-surface-raised p-0 shadow-[0_24px_80px_-48px_rgba(15,23,42,0.95)] backdrop:bg-black/50`}
      onClose={onCancel}
    >
      <div className="p-6">
        <div className="mb-4 flex items-center justify-between">
          <h3 className="text-sm font-semibold text-txt-primary">{title}</h3>
          <button type="button" onClick={onCancel} className="rounded-lg p-1 text-txt-tertiary transition-colors hover:bg-surface-hover hover:text-txt-primary">
            <X className="w-4 h-4" />
          </button>
        </div>
        {description && <p className="mb-4 text-xs text-txt-secondary leading-relaxed">{description}</p>}
        {children}
        <div className="flex justify-end gap-2 mt-5">
          <button
            type="button"
            onClick={onCancel}
            className="rounded-md px-3.5 py-2 text-xs text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className={`rounded-md px-3.5 py-2 text-xs font-medium transition-colors ${
              confirmVariant === 'danger'
                ? 'border border-rose-500/20 text-rose-400 hover:bg-rose-500/10'
                : 'border border-border-subtle text-txt-primary hover:bg-surface-overlay'
            }`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  )
}
