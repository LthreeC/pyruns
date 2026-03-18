import { useEffect, useRef } from 'react'
import { X } from 'lucide-react'

interface Props {
  open: boolean
  title: string
  description?: string
  confirmLabel?: string
  confirmVariant?: 'danger' | 'primary'
  onConfirm: () => void
  onCancel: () => void
  children?: React.ReactNode
}

export default function ConfirmDialog({
  open, title, description, confirmLabel = 'Confirm',
  confirmVariant = 'primary', onConfirm, onCancel, children,
}: Props) {
  const ref = useRef<HTMLDialogElement>(null)

  useEffect(() => {
    if (open) ref.current?.showModal()
    else ref.current?.close()
  }, [open])

  if (!open) return null

  return (
    <dialog
      ref={ref}
      className="fixed inset-0 z-50 m-auto bg-surface-raised border border-border rounded-lg shadow-2xl backdrop:bg-black/60 backdrop:backdrop-blur-sm max-w-md w-full p-0"
      onClose={onCancel}
    >
      <div className="p-5">
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-zinc-100">{title}</h3>
          <button onClick={onCancel} className="text-zinc-500 hover:text-zinc-300 transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>
        {description && <p className="text-xs text-zinc-400 mb-4">{description}</p>}
        {children}
        <div className="flex justify-end gap-2 mt-5">
          <button
            onClick={onCancel}
            className="px-3 py-1.5 text-xs text-zinc-400 hover:text-zinc-200 rounded-md hover:bg-surface-overlay transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            className={`px-3 py-1.5 text-xs font-medium rounded-md transition-colors ${
              confirmVariant === 'danger'
                ? 'bg-rose-500/15 text-rose-400 hover:bg-rose-500/25'
                : 'bg-accent/15 text-accent hover:bg-accent/25'
            }`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </dialog>
  )
}
