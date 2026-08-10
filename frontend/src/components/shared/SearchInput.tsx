import { useEffect, useState } from 'react'
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
}

export default function SearchInput({
  value,
  onChange,
  placeholder = 'Search...',
  ariaLabel = 'Search',
  debounceMs = 300,
  className,
}: Props) {
  const [local, setLocal] = useState(value)
  const debounced = useDebouncedValue(local, debounceMs)

  useEffect(() => { onChange(debounced) }, [debounced])
  useEffect(() => { setLocal(value) }, [value])

  return (
    <div className={clsx('relative flex items-center', className)}>
      <Search className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-txt-tertiary" />
      <input
        type="text"
        inputMode="search"
        enterKeyHint="search"
        autoComplete="off"
        value={local}
        onChange={e => setLocal(e.target.value)}
        placeholder={placeholder}
        aria-label={ariaLabel}
        className="touch-input h-11 w-full rounded-md border border-border-subtle bg-surface-overlay py-2 pl-8 pr-11 text-base leading-5 text-txt-primary placeholder:text-txt-tertiary outline-none transition-colors focus:border-border focus:bg-surface-raised sm:h-[34px] sm:pr-8 sm:text-xs"
      />
      {local && (
        <button
          type="button"
          onClick={() => { setLocal(''); onChange('') }}
          aria-label="Clear search"
          title="Clear search"
          className="touch-target absolute right-0 top-1/2 inline-flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-hover hover:text-txt-primary focus:outline-none focus:ring-2 focus:ring-accent/25 sm:right-1 sm:h-7 sm:w-7"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      )}
    </div>
  )
}
