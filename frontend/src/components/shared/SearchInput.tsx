import { useEffect, useState, type ReactNode, type Ref } from 'react'
import { Search, X } from 'lucide-react'
import clsx from 'clsx'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'

interface Props {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  ariaLabel?: string
  debounceMs?: number
  className?: string
  inputRef?: Ref<HTMLInputElement>
  ariaKeyShortcuts?: string
  trailingControls?: ReactNode
}

export default function SearchInput({
  value,
  onChange,
  placeholder = 'Search...',
  ariaLabel = 'Search',
  debounceMs = 300,
  className,
  inputRef,
  ariaKeyShortcuts,
  trailingControls,
}: Props) {
  const [local, setLocal] = useState(value)
  const debounced = useDebouncedValue(local, debounceMs)

  useEffect(() => { onChange(debounced) }, [debounced])
  useEffect(() => { setLocal(value) }, [value])

  return (
    <div className={clsx('touch-input box-content flex h-11 min-w-0 items-center rounded-md border border-border bg-surface-overlay transition-colors focus-within:border-accent focus-within:ring-1 focus-within:ring-accent/20 sm:h-[34px]', className)}>
      <Search aria-hidden="true" className="search-input-icon pointer-events-none ml-2.5 h-3.5 w-3.5 flex-none text-txt-tertiary" />
      <input
        ref={inputRef}
        type="text"
        inputMode="search"
        enterKeyHint="search"
        autoComplete="off"
        value={local}
        onChange={e => setLocal(e.target.value)}
        placeholder={placeholder}
        aria-label={ariaLabel}
        aria-keyshortcuts={ariaKeyShortcuts}
        className="h-full min-w-0 flex-1 bg-transparent px-2 text-base leading-5 text-txt-primary placeholder:text-txt-tertiary outline-none focus-visible:outline-none sm:text-xs"
      />
      {local && (
        <button
          type="button"
          onClick={() => { setLocal(''); onChange('') }}
          aria-label="Clear search"
          title="Clear search"
          className="touch-target mr-0.5 inline-flex h-10 w-10 flex-none items-center justify-center rounded text-txt-tertiary transition-colors hover:bg-surface-hover hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/25 sm:h-7 sm:w-7"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
      {trailingControls}
    </div>
  )
}
