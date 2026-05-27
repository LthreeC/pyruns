import {
  useState,
  useCallback,
  useEffect,
  useRef,
  type ComponentType,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { X, FileText, Settings, StickyNote, Variable, Save, Pencil, Check } from 'lucide-react'
import clsx from 'clsx'
import { stringify as yamlStringify } from 'yaml'
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

const TASK_DETAIL_WIDTH_STORAGE_KEY = 'pyruns.taskDetailPanelWidth'
const DEFAULT_PANEL_WIDTH = 720
const MIN_PANEL_WIDTH = 420
const MAX_PANEL_WIDTH = 1120

function clampPanelWidth(value: number) {
  if (!Number.isFinite(value)) {
    return DEFAULT_PANEL_WIDTH
  }
  const viewportMax = typeof window === 'undefined'
    ? MAX_PANEL_WIDTH
    : Math.max(MIN_PANEL_WIDTH, window.innerWidth - 56)
  return Math.min(Math.min(MAX_PANEL_WIDTH, viewportMax), Math.max(MIN_PANEL_WIDTH, value))
}

function readStoredPanelWidth() {
  if (typeof window === 'undefined') {
    return DEFAULT_PANEL_WIDTH
  }

  try {
    const stored = Number(window.localStorage.getItem(TASK_DETAIL_WIDTH_STORAGE_KEY))
    if (stored) {
      return clampPanelWidth(stored)
    }
  } catch {
    // Keep the default width when persisted state is unavailable.
  }

  return clampPanelWidth(window.innerWidth * 0.44)
}

function buildEnvPairs(task: Task): [string, string][] {
  return Object.entries(task.env || {}).map(([key, value]) => [key, String(value)])
}

export default function TaskDetailPanel({ task, onClose, onRefresh }: Props) {
  const [tab, setTab] = useState<Tab>('info')
  const [notes, setNotes] = useState(task.notes || '')
  const [envPairs, setEnvPairs] = useState<[string, string][]>(buildEnvPairs(task))
  const [saving, setSaving] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [newName, setNewName] = useState(task.name)
  const [notesDirty, setNotesDirty] = useState(false)
  const [envDirty, setEnvDirty] = useState(false)
  const [panelWidth, setPanelWidth] = useState(readStoredPanelWidth)
  const [resizingPanel, setResizingPanel] = useState(false)
  const previousTaskNameRef = useRef(task.name)

  const startPanelResize = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    setResizingPanel(true)
  }, [])

  useEffect(() => {
    const previousTaskName = previousTaskNameRef.current
    previousTaskNameRef.current = task.name

    if (previousTaskName === task.name) {
      return
    }

    setTab('info')
    setNotes(task.notes || '')
    setEnvPairs(buildEnvPairs(task))
    setNewName(task.name)
    setRenaming(false)
    setNotesDirty(false)
    setEnvDirty(false)
  }, [task.name])

  useEffect(() => {
    if (notesDirty || previousTaskNameRef.current !== task.name) {
      return
    }
    setNotes(task.notes || '')
  }, [task.name, task.notes, notesDirty])

  useEffect(() => {
    if (envDirty || previousTaskNameRef.current !== task.name) {
      return
    }
    setEnvPairs(buildEnvPairs(task))
  }, [task.name, task.env, envDirty])

  useEffect(() => {
    if (renaming) {
      return
    }
    setNewName(task.name)
  }, [task.name, renaming])

  useEffect(() => {
    if (!resizingPanel) {
      return
    }

    const previousCursor = document.body.style.cursor
    const previousUserSelect = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    const handlePointerMove = (event: PointerEvent) => {
      const next = clampPanelWidth(window.innerWidth - event.clientX)
      setPanelWidth(next)
      try {
        window.localStorage.setItem(TASK_DETAIL_WIDTH_STORAGE_KEY, String(next))
      } catch {
        // Resizing still works without persisted storage.
      }
    }

    const stopResize = () => setResizingPanel(false)

    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', stopResize, { once: true })

    return () => {
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', stopResize)
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousUserSelect
    }
  }, [resizingPanel])

  const handleSaveNotes = useCallback(async () => {
    setSaving(true)
    try {
      await api.updateNotes(task.name, notes)
      setNotesDirty(false)
      onRefresh()
    } finally {
      setSaving(false)
    }
  }, [task.name, notes, onRefresh])

  const handleSaveEnv = useCallback(async () => {
    setSaving(true)
    const env = Object.fromEntries(envPairs.filter(([key]) => key.trim()))
    try {
      await api.updateEnv(task.name, env)
      setEnvDirty(false)
      onRefresh()
    } finally {
      setSaving(false)
    }
  }, [task.name, envPairs, onRefresh])

  const handleRename = useCallback(async () => {
    if (!newName.trim() || newName === task.name) {
      setRenaming(false)
      return
    }

    setSaving(true)
    try {
      await api.renameTask(task.name, newName.trim())
      onRefresh()
      onClose()
    } catch {
      setNewName(task.name)
    } finally {
      setSaving(false)
      setRenaming(false)
    }
  }, [task.name, newName, onRefresh, onClose])

  const tabs: { key: Tab; label: string; icon: ComponentType<{ className?: string }> }[] = [
    { key: 'info', label: 'Info', icon: FileText },
    { key: 'config', label: (task.config_mode || task.task_kind) === 'shell' ? 'Script' : 'Config', icon: Settings },
    { key: 'notes', label: 'Notes', icon: StickyNote },
    { key: 'env', label: 'Env', icon: Variable },
  ]

  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose}>
      <div className="absolute inset-0 bg-black/30" />
      <div
        className="animate-slide-in relative flex h-full min-w-[360px] max-w-[calc(100vw-56px)] flex-col border-l border-border-subtle bg-surface-raised"
        style={{ width: panelWidth }}
        onClick={event => event.stopPropagation()}
      >
        <button
          type="button"
          aria-label="Resize task detail panel"
          aria-orientation="vertical"
          onPointerDown={startPanelResize}
          className={clsx(
            'absolute left-0 top-0 z-10 h-full w-2 -translate-x-1 cursor-col-resize touch-none transition-colors focus:outline-none focus:ring-2 focus:ring-accent/35',
            resizingPanel ? 'bg-accent/35' : 'bg-transparent hover:bg-accent/20',
          )}
        />
        <div className="flex items-center gap-2 border-b border-border-subtle px-4 py-3">
          <StatusBadge status={task.status as TaskStatus} />

          <div className="min-w-0 flex-1">
            {renaming ? (
              <div className="flex items-center gap-1.5">
                <input
                  autoFocus
                  value={newName}
                  onChange={event => setNewName(event.target.value)}
                  onKeyDown={event => {
                    if (event.key === 'Enter') void handleRename()
                    if (event.key === 'Escape') {
                      setRenaming(false)
                      setNewName(task.name)
                    }
                  }}
                  title="New task name"
                  className="w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 py-1.5 text-sm text-txt-primary outline-none focus:border-border"
                />
                <button
                  type="button"
                  onClick={() => void handleRename()}
                  title="Save name"
                  className="rounded-md p-1.5 text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
                >
                  <Check className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setRenaming(false)
                    setNewName(task.name)
                  }}
                  title="Cancel"
                  className="rounded-md p-1.5 text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <span className="truncate text-sm font-medium text-txt-primary">{task.name}</span>
                <button
                  type="button"
                  onClick={() => setRenaming(true)}
                  title="Rename task"
                  className="rounded-md p-1 text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
                >
                  <Pencil className="h-3 w-3" />
                </button>
              </div>
            )}
          </div>

          <button
            type="button"
            onClick={onClose}
            className="rounded-md p-1.5 text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
            title="Close"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="flex gap-1 border-b border-border-subtle px-3 py-2">
          {tabs.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={clsx(
                'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-colors',
                tab === key
                  ? 'bg-surface-overlay text-txt-primary'
                  : 'text-txt-secondary hover:bg-surface-overlay hover:text-txt-primary'
              )}
            >
              <Icon className="h-3.5 w-3.5" />
              <span>{label}</span>
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {tab === 'info' && <InfoTab task={task} />}
          {tab === 'config' && <ConfigTab task={task} />}
          {tab === 'notes' && (
            <div className="flex h-full flex-col gap-3">
              <textarea
                value={notes}
                onChange={event => {
                  setNotes(event.target.value)
                  setNotesDirty(true)
                }}
                placeholder="Add notes..."
                className="min-h-[220px] flex-1 resize-none rounded-lg border border-border-subtle bg-surface-overlay p-3 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-border"
              />
              <button
                type="button"
                onClick={() => void handleSaveNotes()}
                disabled={saving}
                className="self-end rounded-md border border-border-subtle px-3 py-2 text-xs font-medium text-txt-primary transition-colors hover:bg-surface-overlay disabled:opacity-50"
              >
                <span className="inline-flex items-center gap-1.5">
                  <Save className="h-3.5 w-3.5" />
                  Save Notes
                </span>
              </button>
            </div>
          )}
          {tab === 'env' && (
            <div className="flex flex-col gap-3">
              {envPairs.map(([key, value], index) => (
                <div key={`${key}-${index}`} className="flex items-center gap-2">
                  <input
                    value={key}
                    onChange={event => {
                      const next = [...envPairs]
                      next[index] = [event.target.value, value]
                      setEnvPairs(next)
                      setEnvDirty(true)
                    }}
                    placeholder="KEY"
                    className="w-2/5 rounded-md border border-border-subtle bg-surface-overlay px-2.5 py-1.5 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-border"
                  />
                  <input
                    value={value}
                    onChange={event => {
                      const next = [...envPairs]
                      next[index] = [key, event.target.value]
                      setEnvPairs(next)
                      setEnvDirty(true)
                    }}
                    placeholder="value"
                    className="flex-1 rounded-md border border-border-subtle bg-surface-overlay px-2.5 py-1.5 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-border"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setEnvPairs(envPairs.filter((_, pairIndex) => pairIndex !== index))
                      setEnvDirty(true)
                    }}
                    className="rounded-md p-1 text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-rose-400"
                    title="Remove"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
              ))}

              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => {
                    setEnvPairs([...envPairs, ['', '']])
                    setEnvDirty(true)
                  }}
                  className="text-xs text-txt-secondary transition-colors hover:text-txt-primary"
                >
                  + Add variable
                </button>
                <div className="flex-1" />
                <button
                  type="button"
                  onClick={() => void handleSaveEnv()}
                  disabled={saving}
                  className="rounded-md border border-border-subtle px-3 py-2 text-xs font-medium text-txt-primary transition-colors hover:bg-surface-overlay disabled:opacity-50"
                >
                  <span className="inline-flex items-center gap-1.5">
                    <Save className="h-3.5 w-3.5" />
                    Save
                  </span>
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function getTaskMode(task: Task): string {
  return (task.config_mode || task.task_kind) === 'shell' ? 'shell' : 'config'
}

function formatScalarValue(value: unknown): string {
  if (value === null || value === undefined || value === '') {
    return '(none)'
  }
  return String(value)
}

function formatRecordValue(value: unknown): string {
  if (value === null || value === undefined) {
    return '(empty)'
  }
  if (typeof value !== 'object') {
    return String(value)
  }
  if (Array.isArray(value)) {
    return value.length > 0 ? yamlStringify(value).trim() : '(empty)'
  }
  return Object.keys(value as Record<string, unknown>).length > 0
    ? yamlStringify(value as Record<string, unknown>).trim()
    : '(empty)'
}

function buildRunEntries(task: Task) {
  const totalRuns = Math.max(
    task.start_times?.length ?? 0,
    task.finish_times?.length ?? 0,
    task.pids?.length ?? 0,
    task.records?.length ?? 0,
    task.run_index || 0
  )

  return Array.from({ length: totalRuns }, (_, index) => ({
    index: index + 1,
    start: task.start_times?.[index] || '',
    finish: task.finish_times?.[index] || '',
    pid: task.pids?.[index],
    record: task.records?.[index],
  }))
}

function InfoTab({ task }: { task: Task }) {
  const rows: [string, string][] = [
    ['Status', task.status],
    ['Created', task.created_at],
    ['Mode', getTaskMode(task)],
    ['Run Index', String(task.run_index || 1)],
    ['Directory', task.dir],
  ]

  if (task._load_error) {
    rows.push(['Load Error', task._load_error])
  }

  const runs = buildRunEntries(task)

  return (
    <div className="space-y-5">
      <section className="space-y-2">
        {rows.map(([label, value]) => (
          <div key={label} className="grid grid-cols-[88px_minmax(0,1fr)] gap-3 border-b border-border-subtle py-2">
            <span className="text-xs text-txt-tertiary">{label}</span>
            <span className="break-all font-mono text-xs text-txt-primary">{value}</span>
          </div>
        ))}
      </section>

      <section className="space-y-2">
        <div className="text-2xs uppercase tracking-[0.16em] text-txt-tertiary">Run History</div>
        {runs.length === 0 ? (
          <div className="px-0.5 py-2 text-xs text-txt-secondary">
            No runs recorded yet.
          </div>
        ) : (
          <div className="space-y-3">
            {runs.map(run => (
              <div key={run.index} className="border-t border-border-subtle pt-3">
                <div className="mb-2 text-xs font-medium text-txt-primary">Run #{run.index}</div>
                <div className="space-y-1.5">
                  <div className="grid grid-cols-[72px_minmax(0,1fr)] gap-3">
                    <span className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Start</span>
                    <span className="break-all font-mono text-xs text-txt-primary">{formatScalarValue(run.start)}</span>
                  </div>
                  <div className="grid grid-cols-[72px_minmax(0,1fr)] gap-3">
                    <span className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Finish</span>
                    <span className="break-all font-mono text-xs text-txt-primary">{formatScalarValue(run.finish)}</span>
                  </div>
                  <div className="grid grid-cols-[72px_minmax(0,1fr)] gap-3">
                    <span className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">PID</span>
                    <span className="break-all font-mono text-xs text-txt-primary">{formatScalarValue(run.pid)}</span>
                  </div>
                  <div className="grid grid-cols-[72px_minmax(0,1fr)] gap-3">
                    <span className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Record</span>
                    <pre className="overflow-auto whitespace-pre-wrap rounded-md bg-surface-overlay/60 p-2 font-mono text-xs leading-relaxed text-txt-primary">
                      {formatRecordValue(run.record)}
                    </pre>
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  )
}

function ConfigTab({ task }: { task: Task }) {
  const normalizedConfig = Object.fromEntries(
    Object.entries(task.config || {}).filter(([key]) => !key.startsWith('_meta'))
  )
  const content = task.config_text?.trim()
    ? task.config_text
    : Object.keys(normalizedConfig).length > 0
      ? yamlStringify(normalizedConfig)
      : '(empty)'

  return (
    <div className="space-y-2">
      <div className="text-2xs uppercase tracking-[0.16em] text-txt-tertiary">Payload File</div>
      <div className="font-mono text-xs text-txt-primary">{task.config_file}</div>
      <pre className="overflow-auto whitespace-pre-wrap rounded-md bg-surface-overlay p-4 font-mono text-xs leading-relaxed text-txt-primary">
        {content}
      </pre>
    </div>
  )
}
