import { useEffect, useRef, useState, type ReactNode, type Ref } from 'react'
import { Search, X } from 'lucide-react'
import clsx from 'clsx'

interface Props {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  ariaLabel?: string
  debounceMs?: number
  className?: string
  inputRef?: Ref<HTMLTextAreaElement>
  ariaKeyShortcuts?: string
  trailingControls?: ReactNode
  searchOnType?: boolean
  onSubmit?: () => void
  onCancel?: () => void
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
  searchOnType = true,
  onSubmit,
  onCancel,
}: Props) {
  const [local, setLocal] = useState(value)
  const [composing, setComposing] = useState(false)
  const changeRef = useRef(onChange)
  changeRef.current = onChange
  const rows = Math.min(4, local.split('\n').length)

  useEffect(() => {
    if (!searchOnType || composing || local === value) return
    const timer = setTimeout(() => changeRef.current(local), debounceMs)
    return () => clearTimeout(timer)
  }, [local, value, debounceMs, searchOnType, composing])
  useEffect(() => { setLocal(value) }, [value])

  return (
    <div style={{ height: rows > 1 ? rows * 20 + 24 : undefined }} className={clsx('touch-input box-content flex h-11 min-w-0 items-center rounded-md border border-border bg-surface-overlay transition-colors focus-within:border-accent focus-within:ring-1 focus-within:ring-accent/20 sm:h-[34px]', className)}>
      <Search aria-hidden="true" className="search-input-icon pointer-events-none ml-2.5 h-3.5 w-3.5 flex-none text-txt-tertiary" />
      <textarea
        ref={inputRef}
        rows={rows}
        inputMode="search"
        enterKeyHint="search"
        autoComplete="off"
        value={local}
        onChange={e => setLocal(e.target.value)}
        onCompositionStart={() => setComposing(true)}
        onCompositionEnd={() => setComposing(false)}
        onKeyDown={event => {
          if (event.nativeEvent.isComposing) return
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault()
            if (local === value) onSubmit?.()
            else onChange(local)
          } else if (event.key === 'Escape') {
            setLocal(value)
            onCancel?.()
          }
        }}
        placeholder={placeholder}
        aria-label={ariaLabel}
        aria-keyshortcuts={ariaKeyShortcuts}
        title="Enter to search; Shift+Enter for a new line"
        className="h-full min-w-0 flex-1 resize-none bg-transparent px-2 py-3 text-base leading-5 text-txt-primary placeholder:text-txt-tertiary outline-none focus-visible:outline-none sm:py-[7px] sm:text-xs sm:leading-5"
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
