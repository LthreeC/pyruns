import { Check } from 'lucide-react'
import clsx from 'clsx'

interface Props {
  selected: boolean
  className?: string
}

export default function SelectionIndicator({ selected, className }: Props) {
  return (
    <span
      className={clsx(
        'flex h-4.5 w-4.5 items-center justify-center rounded border transition-colors',
        selected
          ? 'border-accent bg-accent text-white'
          : 'border-border-strong bg-surface-overlay text-transparent',
        className,
      )}
    >
      <Check className="h-3 w-3" />
    </span>
  )
}
