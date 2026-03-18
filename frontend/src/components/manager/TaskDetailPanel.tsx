import { useState, useCallback } from 'react'
import { X, FileText, Settings, StickyNote, Variable, Save } from 'lucide-react'
import clsx from 'clsx'
import StatusBadge from '@/components/shared/StatusBadge'
import type { Task } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import * as api from '@/api'

interface Props {
  task: Task
  onClose: () => void
  onRefresh: () => void
}

type Tab = 'info' | 'config' | 'notes' | 'env'

export default function TaskDetailPanel({ task, onClose, onRefresh }: Props) {
  const [tab, setTab] = useState<Tab>('info')
  const [notes, setNotes] = useState(task.notes || '')
  const [envPairs, setEnvPairs] = useState<[string, string][]>(
    Object.entries(task.env || {}).map(([k, v]) => [k, String(v)])
  )
  const [saving, setSaving] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [newName, setNewName] = useState(task.name)

  const handleSaveNotes = useCallback(async () => {
    setSaving(true)
    try { await api.updateNotes(task.name, notes); onRefresh() }
    finally { setSaving(false) }
  }, [task.name, notes])

  const handleSaveEnv = useCallback(async () => {
    setSaving(true)
    const env = Object.fromEntries(envPairs.filter(([k]) => k.trim()))
    try { await api.updateEnv(task.name, env); onRefresh() }
    finally { setSaving(false) }
  }, [task.name, envPairs])

  const handleRename = useCallback(async () => {
    if (!newName.trim() || newName === task.name) { setRenaming(false); return }
    setSaving(true)
    try { await api.renameTask(task.name, newName.trim()); onRefresh(); onClose() }
    finally { setSaving(false); setRenaming(false) }
  }, [task.name, newName])

  const TABS: { key: Tab; label: string; icon: React.ElementType }[] = [
    { key: 'info', label: 'Info', icon: FileText },
    { key: 'config', label: 'Config', icon: Settings },
    { key: 'notes', label: 'Notes', icon: StickyNote },
    { key: 'env', label: 'Env', icon: Variable },
  ]

  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose}>
      <div className="absolute inset-0 bg-black/40" />
      <div
        className="relative w-full max-w-lg bg-surface-raised border-l border-border-subtle h-full flex flex-col animate-slide-in"
        onClick={e => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border-subtle flex-none">
          <div className="flex items-center gap-2 min-w-0">
            <StatusBadge status={task.status as TaskStatus} />
            {renaming ? (
              <input
                autoFocus
                value={newName}
                onChange={e => setNewName(e.target.value)}
                onBlur={handleRename}
                onKeyDown={e => e.key === 'Enter' && handleRename()}
                className="bg-surface-overlay border border-border rounded px-2 py-0.5 text-sm text-txt-primary outline-none focus:border-accent/50"
              />
            ) : (
              <span
                className="text-sm font-medium text-txt-primary truncate cursor-pointer hover:text-accent transition-colors"
                onDoubleClick={() => setRenaming(true)}
                title="Double-click to rename"
              >
                {task.name}
              </span>
            )}
          </div>
          <button type="button" onClick={onClose} className="p-1 text-txt-tertiary hover:text-txt-primary transition-colors" title="Close">
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-border-subtle flex-none">
          {TABS.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={clsx(
                'flex items-center gap-1.5 px-4 py-2 text-xs transition-colors border-b-2',
                tab === key
                  ? 'text-txt-primary border-accent'
                  : 'text-txt-tertiary border-transparent hover:text-txt-secondary'
              )}
            >
              <Icon className="w-3 h-3" /> {label}
            </button>
          ))}
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4">
          {tab === 'info' && <InfoTab task={task} />}
          {tab === 'config' && <ConfigTab task={task} />}
          {tab === 'notes' && (
            <div className="flex flex-col gap-3 h-full">
              <textarea
                value={notes}
                onChange={e => setNotes(e.target.value)}
                placeholder="Add notes..."
                className="flex-1 min-h-[200px] bg-surface-overlay border border-border rounded-md p-3 text-xs text-txt-primary placeholder:text-txt-tertiary outline-none focus:border-accent/50 resize-none font-mono"
              />
              <button
                type="button"
                onClick={handleSaveNotes}
                disabled={saving}
                className="self-end flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-accent/15 text-accent text-xs font-medium hover:bg-accent/25 transition-colors disabled:opacity-50"
              >
                <Save className="w-3 h-3" /> Save Notes
              </button>
            </div>
          )}
          {tab === 'env' && (
            <div className="flex flex-col gap-3">
              {envPairs.map(([k, v], i) => (
                <div key={i} className="flex items-center gap-2">
                  <input
                    value={k}
                    onChange={e => { const p = [...envPairs]; p[i] = [e.target.value, v]; setEnvPairs(p) }}
                    placeholder="KEY"
                    className="w-1/3 bg-surface-overlay border border-border rounded px-2 py-1.5 text-xs text-txt-primary font-mono outline-none focus:border-accent/50"
                  />
                  <input
                    value={v}
                    onChange={e => { const p = [...envPairs]; p[i] = [k, e.target.value]; setEnvPairs(p) }}
                    placeholder="value"
                    className="flex-1 bg-surface-overlay border border-border rounded px-2 py-1.5 text-xs text-txt-primary font-mono outline-none focus:border-accent/50"
                  />
                  <button
                    type="button"
                    onClick={() => setEnvPairs(envPairs.filter((_, j) => j !== i))}
                    className="text-txt-tertiary hover:text-rose-400 transition-colors"
                    title="Remove"
                  >
                    <X className="w-3 h-3" />
                  </button>
                </div>
              ))}
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => setEnvPairs([...envPairs, ['', '']])}
                  className="text-2xs text-accent hover:text-accent-hover transition-colors"
                >
                  + Add variable
                </button>
                <div className="flex-1" />
                <button
                  type="button"
                  onClick={handleSaveEnv}
                  disabled={saving}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-accent/15 text-accent text-xs font-medium hover:bg-accent/25 transition-colors disabled:opacity-50"
                >
                  <Save className="w-3 h-3" /> Save
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function InfoTab({ task }: { task: Task }) {
  const rows: [string, string][] = [
    ['Status', task.status],
    ['Created', task.created_at],
    ['Run Mode', task.run_mode || '—'],
    ['Run Index', String(task.run_index || 1)],
    ['Directory', task.dir],
  ]
  if (task.start_times?.length) rows.push(['Last Start', task.start_times[task.start_times.length - 1]])
  if (task.finish_times?.length) rows.push(['Last Finish', task.finish_times[task.finish_times.length - 1]])
  if (task.pids?.length) rows.push(['PIDs', task.pids.join(', ')])

  return (
    <div className="space-y-2">
      {rows.map(([label, value]) => (
        <div key={label} className="flex items-start gap-3">
          <span className="text-2xs text-txt-tertiary w-20 flex-none pt-0.5">{label}</span>
          <span className="text-xs text-txt-secondary font-mono break-all">{value}</span>
        </div>
      ))}
    </div>
  )
}

function ConfigTab({ task }: { task: Task }) {
  const yaml = task.config
    ? Object.entries(task.config)
        .filter(([k]) => !k.startsWith('_meta'))
        .map(([k, v]) => `${k}: ${JSON.stringify(v)}`)
        .join('\n')
    : '(empty)'

  return (
    <pre className="text-xs text-txt-secondary font-mono bg-surface-overlay border border-border rounded-md p-3 overflow-auto whitespace-pre-wrap">
      {yaml}
    </pre>
  )
}
