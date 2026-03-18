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
      <Search className="absolute left-2.5 w-3.5 h-3.5 text-zinc-500 pointer-events-none" />
      <input
        type="text"
        value={local}
        onChange={e => setLocal(e.target.value)}
        placeholder={placeholder}
        className="w-full bg-surface-overlay border border-border rounded-md pl-8 pr-7 py-1.5 text-xs text-zinc-200 placeholder:text-zinc-600 outline-none focus:border-accent/50 transition-colors"
      />
      {local && (
        <button
          onClick={() => { setLocal(''); onChange('') }}
          className="absolute right-2 text-zinc-500 hover:text-zinc-300"
        >
          <X className="w-3 h-3" />
        </button>
      )}
    </div>
  )
}
