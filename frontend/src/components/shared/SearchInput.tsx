import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type Ref } from 'react'
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
  history?: string[]
  historyRevision?: number
  onRemember?: (value: string) => void
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
  history = [],
  historyRevision = 0,
  onRemember,
}: Props) {
  const [local, setLocal] = useState(value)
  const [composing, setComposing] = useState(false)
  const changeRef = useRef(onChange)
  const historyNavigation = useRef<{ items: string[]; index: number; draft: string } | null>(null)
  const historyCaret = useRef<{ input: HTMLTextAreaElement; value: string; position: number } | null>(null)
  changeRef.current = onChange
  const rows = Math.min(4, local.split('\n').length)

  useEffect(() => {
    if (!searchOnType || composing || local === value) return
    const timer = setTimeout(() => changeRef.current(local), debounceMs)
    return () => clearTimeout(timer)
  }, [local, value, debounceMs, searchOnType, composing])
  useEffect(() => { setLocal(value) }, [value])
  useEffect(() => {
    historyNavigation.current = null
    historyCaret.current = null
  }, [historyRevision])
  useLayoutEffect(() => {
    const caret = historyCaret.current
    historyCaret.current = null
    if (caret?.value === local) caret.input.setSelectionRange(caret.position, caret.position)
  }, [local])

  return (
    <div style={{ height: rows > 1 ? rows * 20 + 24 : undefined }} className={clsx('touch-input box-content flex h-11 min-w-0 items-center rounded-md border border-border bg-surface-overlay transition-colors focus-within:border-accent focus-within:ring-1 focus-within:ring-accent/20 sm:h-[34px]', className)}>
      <Search aria-hidden="true" className="search-input-icon pointer-events-none ml-2.5 h-3.5 w-3.5 flex-none text-txt-tertiary" />
      <textarea
        ref={inputRef}
        rows={rows}
        wrap="off"
        inputMode="search"
        enterKeyHint="search"
        autoComplete="off"
        value={local}
        onChange={e => { historyNavigation.current = null; setLocal(e.target.value) }}
        onCompositionStart={() => setComposing(true)}
        onCompositionEnd={() => setComposing(false)}
        onKeyDown={event => {
          if (composing || event.nativeEvent.isComposing) return
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault()
            historyNavigation.current = null
            onRemember?.(local)
            if (local === value) onSubmit?.()
            else onChange(local)
          } else if (event.key === 'Escape') {
            historyNavigation.current = null
            setLocal(value)
            onCancel?.()
          } else if ((event.key === 'ArrowUp' || event.key === 'ArrowDown')
            && !event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey) {
            const input = event.currentTarget
            const previous = event.key === 'ArrowUp'
            if (input.selectionStart !== input.selectionEnd
              || (previous ? local.slice(0, input.selectionStart) : local.slice(input.selectionEnd)).includes('\n')) return
            const navigation = historyNavigation.current ?? { items: history.filter(item => item !== local), index: -1, draft: local }
            if (!navigation.items.length || (!previous && navigation.index === -1)) return
            event.preventDefault()
            navigation.index = Math.max(-1, Math.min(navigation.items.length - 1, navigation.index + (previous ? 1 : -1)))
            historyNavigation.current = navigation
            const next = navigation.index === -1 ? navigation.draft : navigation.items[navigation.index]
            const position = previous ? 0 : next.length
            if (next === local) input.setSelectionRange(position, position)
            else {
              // Restore the caret with the new value, before another input can
              // move it. A later animation frame can overwrite user selection.
              historyCaret.current = { input, value: next, position }
              setLocal(next)
            }
          }
        }}
        placeholder={placeholder}
        aria-label={ariaLabel}
        aria-keyshortcuts={ariaKeyShortcuts}
        title="Ctrl/Cmd+Shift+F to focus; Enter to search; Shift+Enter for a new line; Up/Down for search history"
        className="h-full min-w-0 flex-1 resize-none overflow-x-hidden bg-transparent px-2 py-3 text-base leading-5 text-txt-primary placeholder:text-txt-tertiary outline-none focus-visible:outline-none sm:py-[7px] sm:text-xs sm:leading-5"
      />
      {local && (
        <button
          type="button"
          onClick={() => { historyNavigation.current = null; setLocal(''); onChange('') }}
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
