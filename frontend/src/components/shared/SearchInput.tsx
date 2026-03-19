import { useEffect, useRef, useState } from 'react'
import { Search, X } from 'lucide-react'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'

interface Props {
  value: string
  onChange: (value: string) => void
  placeholder?: string
  debounceMs?: number
}

export default function SearchInput({ value, onChange, placeholder = 'Search...', debounceMs = 300 }: Props) {
  const [local, setLocal] = useState(value)
  const debounced = useDebouncedValue(local, debounceMs)
  const textareaRef = useRef<HTMLTextAreaElement | null>(null)

  useEffect(() => { onChange(debounced) }, [debounced])
  useEffect(() => { setLocal(value) }, [value])
  useEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    textarea.style.height = '0px'
    const nextHeight = Math.min(Math.max(textarea.scrollHeight, 34), 120)
    textarea.style.height = `${nextHeight}px`
  }, [local])

  return (
    <div className="relative flex items-start">
      <Search className="pointer-events-none absolute left-2.5 top-2.5 h-3.5 w-3.5 text-txt-tertiary" />
      <textarea
        ref={textareaRef}
        rows={1}
        value={local}
        onChange={e => setLocal(e.target.value)}
        placeholder={placeholder}
        className="min-h-[34px] w-full resize-none overflow-y-auto rounded-md border border-border-subtle bg-surface-overlay py-2 pl-8 pr-8 text-xs leading-5 text-txt-primary placeholder:text-txt-tertiary outline-none transition-colors focus:border-border focus:bg-surface-raised"
      />
      {local && (
        <button
          type="button"
          onClick={() => { setLocal(''); onChange('') }}
          className="absolute right-1.5 top-1.5 rounded-md p-1 text-txt-tertiary hover:bg-surface-hover hover:text-txt-primary"
        >
          <X className="w-3 h-3" />
        </button>
      )}
    </div>
  )
}
