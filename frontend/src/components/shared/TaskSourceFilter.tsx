import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { ChevronDown, ListFilter } from 'lucide-react'
import { useTranslation } from '@/i18n'
import { useTaskStore } from '@/store'

export default function TaskSourceFilter() {
  const { t } = useTranslation()
  const sources = useTaskStore(state => state.sources)
  const selected = useTaskStore(state => state.selectedSources)
  const change = useTaskStore(state => state.setSelectedSources)
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState({ left: 0, top: 0 })
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const allBox = useRef<HTMLInputElement>(null)
  const id = useId()
  const checked = sources.filter(source => selected === null || selected.includes(source.id))
  const all = checked.length === sources.length
  const mixed = !all && checked.length > 0

  useLayoutEffect(() => {
    if (!open) return
    const place = () => {
      const rect = trigger.current!.getBoundingClientRect()
      setPosition({ left: Math.max(8, Math.min(rect.left, window.innerWidth - 248)), top: rect.bottom + 4 })
    }
    place()
    allBox.current?.focus()
    window.addEventListener('resize', place)
    return () => window.removeEventListener('resize', place)
  }, [open])

  useLayoutEffect(() => {
    if (allBox.current) allBox.current.indeterminate = mixed
  }, [open, mixed])

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
  }} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false) }}>
    <button ref={trigger} type="button" aria-label={t('Task sources')} aria-expanded={open}
      aria-controls={open ? id : undefined} title={t('Task sources')} onClick={() => setOpen(value => !value)}
      className="inline-flex h-9 items-center gap-1 rounded-md px-1.5 text-xs font-medium text-txt-secondary hover:bg-surface-overlay focus-visible:ring-2 focus-visible:ring-accent sm:h-8">
      <ListFilter className="size-4" aria-hidden="true" />
      {!all && <span className="tabular-nums text-accent">{checked.length}</span>}
      <ChevronDown className="size-3" aria-hidden="true" />
    </button>
    {open && <div id={id} role="group" aria-label={t('Task sources')} style={{ ...position, maxHeight: `calc(100dvh - ${position.top + 8}px)` }}
      className="fixed z-50 w-60 max-w-[calc(100vw-16px)] overflow-y-auto rounded-xl border border-border-subtle bg-surface-raised p-1.5 text-sm text-txt-primary shadow-lg">
      <label className="flex min-h-9 cursor-pointer items-center gap-2 rounded-md px-2 hover:bg-surface-overlay">
        <input ref={allBox} type="checkbox" checked={all} onChange={() => change(all ? [] : null)} />
        {t('Select all sources')}
      </label>
      <div className="my-1 border-t border-border-subtle" />
      {sources.map(source => {
        const label = source.id === 'shared' ? t('Common tasks') : source.id === 'local' ? t('This device') : source.host
        const duplicate = sources.some(other => other.id !== source.id && other.host === source.host)
        return <label key={source.id} title={source.id === 'shared' ? 'tasks/' : source.id === 'local' ? source.host : source.id}
          className="flex min-h-9 cursor-pointer items-center gap-2 rounded-md px-2 hover:bg-surface-overlay">
          <input type="checkbox" checked={selected === null || selected.includes(source.id)} onChange={event => {
            const next = checked.map(item => item.id).filter(item => item !== source.id)
            if (event.target.checked) next.push(source.id)
            change(next.length === sources.length ? null : next)
          }} />
          <span className="min-w-0 truncate">{label}</span>
          {source.id === 'local' && <small className="truncate text-txt-tertiary">{source.host}</small>}
          {duplicate && source.id.startsWith('tasks-') && <small className="font-mono text-txt-secondary">{source.id.slice(-12)}</small>}
        </label>
      })}
    </div>}
  </div>
}
