import { useState, useEffect } from 'react'
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

  useEffect(() => { onChange(debounced) }, [debounced])
  useEffect(() => { setLocal(value) }, [value])

  return (
    <div className="relative flex items-center">
      <Search className="pointer-events-none absolute left-2.5 h-3.5 w-3.5 text-txt-tertiary" />
      <input
        type="text"
        value={local}
        onChange={e => setLocal(e.target.value)}
        placeholder={placeholder}
        className="w-full rounded-md border border-border-subtle bg-surface-overlay py-1.5 pl-8 pr-8 text-xs text-txt-primary placeholder:text-txt-tertiary outline-none transition-colors focus:border-border focus:bg-surface-raised"
      />
      {local && (
        <button
          type="button"
          onClick={() => { setLocal(''); onChange('') }}
          className="absolute right-1.5 rounded-md p-1 text-txt-tertiary hover:bg-surface-hover hover:text-txt-primary"
        >
          <X className="w-3 h-3" />
        </button>
      )}
    </div>
  )
}
