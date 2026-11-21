import { Check, Minus } from 'lucide-react'
import clsx from 'clsx'

interface Props {
  selected: boolean | 'mixed'
}

export default function SelectionIndicator({ selected }: Props) {
  return (
    <span
      aria-hidden="true"
      className={clsx(
        'inline-flex size-[18px] shrink-0 items-center justify-center rounded border transition-colors',
        selected ? 'border-accent bg-accent text-white' : 'border-border-strong bg-surface-raised',
      )}
    >
      {selected === 'mixed' ? <Minus className="size-3" strokeWidth={2.5} /> : selected && <Check className="size-3" strokeWidth={2.5} />}
    </span>
  )
}
