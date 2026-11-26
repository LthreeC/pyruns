import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { ChevronDown, ListFilter, Search } from 'lucide-react'
import { useTranslation } from '@/i18n'
import { useTaskStore } from '@/store'
import SelectionIndicator from './SelectionIndicator'

export default function TaskSourceFilter() {
  const { t } = useTranslation()
  const sources = useTaskStore(state => state.sources)
  const selected = useTaskStore(state => state.selectedSources)
  const change = useTaskStore(state => state.setSelectedSources)
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const [position, setPosition] = useState<CSSProperties>({})
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const allBox = useRef<HTMLButtonElement>(null)
  const searchBox = useRef<HTMLInputElement>(null)
  const id = useId()
  const selectedIds = useMemo(() => selected === null ? null : new Set(selected), [selected])
  const count = selectedIds === null ? sources.length : sources.reduce((n, source) => n + Number(selectedIds.has(source.id)), 0)
  const all = count === sources.length
  const mixed = !all && count > 0
  const hosts = useMemo(() => {
    const counts = new Map<string, number>()
    if (open) {
      for (const source of sources) counts.set(source.host, (counts.get(source.host) ?? 0) + 1)
    }
    return counts
  }, [open, sources])
  const searchable = sources.length >= 8
  const term = searchable ? query.trim().toLowerCase() : ''
  const options = useMemo(() => {
    if (!open) return []
    return sources.map(source => ({
      source,
      label: source.id === 'shared' ? t('Common tasks') : source.id === 'local' ? t('This device') : source.host,
    })).filter(({ source, label }) => !term || [label, source.host, source.id].some(value => value.toLowerCase().includes(term)))
  }, [open, sources, t, term])

  function toggleSource(id: string, active: boolean) {
    const next = sources.filter(source => source.id === id
      ? !active
      : selectedIds === null || selectedIds.has(source.id)
    ).map(source => source.id)
    change(next.length === sources.length ? null : next)
  }

  useLayoutEffect(() => {
    if (!open) {
      setQuery('')
      return
    }
    const place = () => {
      const rect = trigger.current!.getBoundingClientRect()
      const below = window.innerHeight - rect.bottom - 12
      const above = rect.top - 12
      const flip = below < 220 && above > below
      setPosition({
        left: Math.max(8, Math.min(rect.left, window.innerWidth - 248)),
        ...(flip ? { bottom: window.innerHeight - rect.top + 4 } : { top: rect.bottom + 4 }),
        maxHeight: Math.max(0, flip ? above : below),
      })
    }
    place()
    const focusTarget = (window.matchMedia('(pointer: fine)').matches && searchBox.current) || allBox.current
    focusTarget?.focus({ preventScroll: true })
    window.addEventListener('resize', place)
    return () => window.removeEventListener('resize', place)
  }, [open])

  useEffect(() => {
    if (!open) return
    const outside = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('pointerdown', outside)
    return () => document.removeEventListener('pointerdown', outside)
  }, [open])

  return <div ref={root} className="inline-flex shrink-0" onKeyDown={event => {
    if (open && event.key === 'Escape') {
      event.stopPropagation()
      setOpen(false)
      trigger.current?.focus()
    }
  }} onBlur={event => { if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) setOpen(false) }}>
    <button ref={trigger} type="button" aria-label={t('Task sources')} aria-expanded={open}
      aria-controls={open ? id : undefined} title={t('Task sources')} onClick={() => setOpen(value => !value)}
      className="touch-target inline-flex h-9 items-center justify-center gap-1 rounded-md px-1.5 text-xs font-medium text-txt-secondary hover:bg-surface-overlay focus-visible:ring-2 focus-visible:ring-accent aria-expanded:bg-accent/10 aria-expanded:text-accent-ink sm:h-8">
      <ListFilter className="size-4" aria-hidden="true" />
      {!all && <span className="tabular-nums text-accent">{count}</span>}
      <ChevronDown className="size-3" aria-hidden="true" />
    </button>
    {open && <div id={id} role="group" aria-label={t('Task sources')} style={position}
      onKeyDown={event => {
        if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
        const rows = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('button[role="checkbox"]'))
        const index = rows.indexOf(document.activeElement as HTMLButtonElement)
        event.preventDefault()
        const step = event.key === 'ArrowDown' ? 1 : -1
        const next = index < 0 && term ? 1 : index + step
        rows[Math.max(0, Math.min(rows.length - 1, next))]?.focus()
      }}
      className="fixed z-50 flex w-60 max-w-[calc(100vw-16px)] flex-col overflow-y-auto border border-border bg-surface-raised text-sm text-txt-primary shadow-md">
      {searchable && <label className="flex shrink-0 items-center gap-2 border-b border-border-subtle px-3 text-txt-secondary focus-within:text-accent-ink">
        <Search className="size-4 shrink-0" aria-hidden="true" />
        <input ref={searchBox} type="search" aria-label={t('Find a source')} placeholder={t('Find a source')}
          className="h-10 min-w-0 flex-1 bg-transparent text-sm text-txt-primary" value={query}
          onChange={event => setQuery(event.target.value)} />
      </label>}
      <button ref={allBox} type="button" role="checkbox" aria-checked={mixed ? 'mixed' : all}
        className="source-option border-b border-border-subtle font-medium" onClick={() => change(all ? [] : null)}>
        <SelectionIndicator selected={mixed ? 'mixed' : all} />
        {t('Select all sources')}
      </button>
      <div className="min-h-0 overflow-y-auto overscroll-contain">
        {options.map(({ source, label }) => {
          const duplicate = (hosts.get(source.host) ?? 0) > 1
          const active = selectedIds === null || selectedIds.has(source.id)
          return <button key={source.id} type="button" role="checkbox" aria-checked={active}
            title={source.id === 'shared' ? 'tasks/' : source.id === 'local' ? source.host : source.id}
            className="source-option" onClick={() => toggleSource(source.id, active)}>
            <SelectionIndicator selected={active} />
            <span className="min-w-0 truncate">{label}</span>
            {source.id === 'local' && <small className="ml-auto truncate text-txt-secondary">{source.host}</small>}
            {duplicate && source.id.startsWith('tasks-') && <small className="font-mono text-txt-secondary">{source.id.slice(-12)}</small>}
          </button>
        })}
        {term && options.length === 0 && <p className="px-3 py-3 text-txt-secondary" role="status">{t('No matching sources')}</p>}
      </div>
    </div>}
  </div>
}
