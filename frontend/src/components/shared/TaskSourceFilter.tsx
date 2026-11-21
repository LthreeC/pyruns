import { useEffect, useId, useLayoutEffect, useRef, useState } from 'react'
import { Check, ChevronDown, ListFilter, Minus } from 'lucide-react'
import { useTranslation } from '@/i18n'
import { useTaskStore } from '@/store'

function Checkmark({ checked }: { checked: boolean | 'mixed' }) {
  return <span aria-hidden="true" className={`flex size-4 shrink-0 items-center justify-center border ${checked ? 'border-accent bg-accent text-white' : 'border-border-strong'}`}>
    {checked === 'mixed' ? <Minus size={12} /> : checked && <Check size={12} strokeWidth={3} />}
  </span>
}

export default function TaskSourceFilter() {
  const { t } = useTranslation()
  const sources = useTaskStore(state => state.sources)
  const selected = useTaskStore(state => state.selectedSources)
  const change = useTaskStore(state => state.setSelectedSources)
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState({ left: 0, top: 0 })
  const root = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const allBox = useRef<HTMLButtonElement>(null)
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
      className="inline-flex h-9 items-center gap-1 rounded-md px-1.5 text-xs font-medium text-txt-secondary hover:bg-surface-overlay focus-visible:ring-2 focus-visible:ring-accent sm:h-8">
      <ListFilter className="size-4" aria-hidden="true" />
      {!all && <span className="tabular-nums text-accent">{checked.length}</span>}
      <ChevronDown className="size-3" aria-hidden="true" />
    </button>
    {open && <div id={id} role="group" aria-label={t('Task sources')} style={{ ...position, maxHeight: `calc(100dvh - ${position.top + 8}px)` }}
      className="fixed z-50 w-60 max-w-[calc(100vw-16px)] overflow-y-auto border border-border bg-surface-raised text-sm text-txt-primary shadow-md">
      <button ref={allBox} type="button" role="checkbox" aria-checked={mixed ? 'mixed' : all}
        className="source-option border-b border-border-subtle font-medium" onClick={() => change(all ? [] : null)}>
        <Checkmark checked={mixed ? 'mixed' : all} />
        {t('Select all sources')}
      </button>
      {sources.map(source => {
        const label = source.id === 'shared' ? t('Common tasks') : source.id === 'local' ? t('This device') : source.host
        const duplicate = sources.some(other => other.id !== source.id && other.host === source.host)
        const active = selected === null || selected.includes(source.id)
        return <button key={source.id} type="button" role="checkbox" aria-checked={active}
          title={source.id === 'shared' ? 'tasks/' : source.id === 'local' ? source.host : source.id}
          className="source-option" onClick={() => {
            const next = checked.map(item => item.id).filter(item => item !== source.id)
            if (!active) next.push(source.id)
            change(next.length === sources.length ? null : next)
          }}>
          <Checkmark checked={active} />
          <span className="min-w-0 truncate">{label}</span>
          {source.id === 'local' && <small className="ml-auto truncate text-txt-secondary">{source.host}</small>}
          {duplicate && source.id.startsWith('tasks-') && <small className="font-mono text-txt-secondary">{source.id.slice(-12)}</small>}
        </button>
      })}
    </div>}
  </div>
}
