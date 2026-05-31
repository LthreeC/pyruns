import {
  useState,
  useCallback,
  useEffect,
  useRef,
  type ComponentType,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import {
  X, FileText, Settings, StickyNote, Variable, Save, Pencil, Check, Plus, Loader2, AlertCircle, CheckCircle2,
} from 'lucide-react'
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
type EnvPair = { id: string; key: string; value: string }
type EnvSaveStatus = 'idle' | 'saved' | 'error'

const TASK_DETAIL_WIDTH_STORAGE_KEY = 'pyruns.taskDetailPanelWidth'
const DEFAULT_PANEL_WIDTH = 720
const MIN_PANEL_WIDTH = 420
const MAX_PANEL_WIDTH = 2400
let nextEnvPairId = 0

function clampPanelWidth(value: number) {
  if (!Number.isFinite(value)) {
    return DEFAULT_PANEL_WIDTH
  }
  const viewportMax = typeof window === 'undefined'
    ? MAX_PANEL_WIDTH
    : Math.max(320, window.innerWidth - 8)
  const viewportMin = Math.min(MIN_PANEL_WIDTH, viewportMax)
  return Math.min(Math.min(MAX_PANEL_WIDTH, viewportMax), Math.max(viewportMin, value))
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

function createEnvPair(key = '', value = ''): EnvPair {
  nextEnvPairId += 1
  return { id: `env-${nextEnvPairId}`, key, value }
}

function buildEnvPairs(task: Task): EnvPair[] {
  return Object.entries(task.env || {}).map(([key, value]) => createEnvPair(key, String(value)))
}

function getDuplicateEnvKeys(envPairs: EnvPair[]): Set<string> {
  const seen = new Set<string>()
  const duplicates = new Set<string>()

  envPairs.forEach(({ key }) => {
    const normalized = key.trim()
    if (!normalized) {
      return
    }
    if (seen.has(normalized)) {
      duplicates.add(normalized)
      return
    }
    seen.add(normalized)
  })

  return duplicates
}

function getEnvValidationMessage(envPairs: EnvPair[]): string {
  const duplicateKeys = getDuplicateEnvKeys(envPairs)
  if (duplicateKeys.size > 0) {
    return `Duplicate key: ${[...duplicateKeys][0]}`
  }

  if (envPairs.some(({ key, value }) => !key.trim() && value.trim())) {
    return 'Add a key before saving this value.'
  }

  return ''
}

export default function TaskDetailPanel({ task, onClose, onRefresh }: Props) {
  const [tab, setTab] = useState<Tab>('info')
  const [notes, setNotes] = useState(task.notes || '')
  const [envPairs, setEnvPairs] = useState(() => buildEnvPairs(task))
  const [saving, setSaving] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [newName, setNewName] = useState(task.name)
  const [notesDirty, setNotesDirty] = useState(false)
  const [envDirty, setEnvDirty] = useState(false)
  const [envSaveStatus, setEnvSaveStatus] = useState<EnvSaveStatus>('idle')
  const [envSaveError, setEnvSaveError] = useState('')
  const [pendingEnvFocusId, setPendingEnvFocusId] = useState<string | null>(null)
  const [panelWidth, setPanelWidth] = useState(readStoredPanelWidth)
  const [resizingPanel, setResizingPanel] = useState(false)
  const previousTaskNameRef = useRef(task.name)
  const envKeyInputRefs = useRef<Record<string, HTMLInputElement | null>>({})
  const suppressNextCloseRef = useRef(false)
  const pendingPanelWidthRef = useRef(panelWidth)
  const panelResizeFrameRef = useRef<number | null>(null)

  const startPanelResize = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault()
    event.stopPropagation()
    try {
      event.currentTarget.setPointerCapture?.(event.pointerId)
    } catch {
      // Synthetic pointer events may not have an active pointer to capture.
    }
    suppressNextCloseRef.current = true
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
    setEnvSaveStatus('idle')
    setEnvSaveError('')
    setPendingEnvFocusId(null)
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
    if (!pendingEnvFocusId) {
      return
    }

    const input = envKeyInputRefs.current[pendingEnvFocusId]
    if (!input) {
      return
    }

    input.focus()
    input.select()
    setPendingEnvFocusId(null)
  }, [pendingEnvFocusId, envPairs])

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

    const persistPanelWidth = (next: number) => {
      try {
        window.localStorage.setItem(TASK_DETAIL_WIDTH_STORAGE_KEY, String(next))
      } catch {
        // Resizing still works without persisted storage.
      }
    }

    const applyPendingPanelWidth = () => {
      panelResizeFrameRef.current = null
      setPanelWidth(pendingPanelWidthRef.current)
    }

    const handlePointerMove = (event: PointerEvent) => {
      suppressNextCloseRef.current = true
      pendingPanelWidthRef.current = clampPanelWidth(window.innerWidth - event.clientX)
      if (panelResizeFrameRef.current == null) {
        panelResizeFrameRef.current = window.requestAnimationFrame(applyPendingPanelWidth)
      }
    }

    const stopResize = () => {
      suppressNextCloseRef.current = true
      if (panelResizeFrameRef.current != null) {
        window.cancelAnimationFrame(panelResizeFrameRef.current)
        panelResizeFrameRef.current = null
      }
      setPanelWidth(pendingPanelWidthRef.current)
      persistPanelWidth(pendingPanelWidthRef.current)
      setResizingPanel(false)
    }

    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', stopResize, { once: true })

    return () => {
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', stopResize)
      if (panelResizeFrameRef.current != null) {
        window.cancelAnimationFrame(panelResizeFrameRef.current)
        panelResizeFrameRef.current = null
      }
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousUserSelect
    }
  }, [resizingPanel])

  const markEnvDirty = useCallback(() => {
    setEnvDirty(true)
    setEnvSaveStatus('idle')
    setEnvSaveError('')
  }, [])

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
    const validationMessage = getEnvValidationMessage(envPairs)
    if (validationMessage) {
      setEnvSaveStatus('error')
      setEnvSaveError(validationMessage)
      return
    }

    setSaving(true)
    setEnvSaveStatus('idle')
    setEnvSaveError('')
    const env = Object.fromEntries(
      envPairs
        .filter(({ key }) => key.trim())
        .map(({ key, value }) => [key.trim(), value])
    )
    try {
      await api.updateEnv(task.name, env)
      setEnvPairs(Object.entries(env).map(([key, value]) => createEnvPair(key, String(value))))
      setEnvDirty(false)
      setEnvSaveStatus('saved')
      onRefresh()
    } catch {
      setEnvSaveStatus('error')
      setEnvSaveError('Could not save environment variables.')
    } finally {
      setSaving(false)
    }
  }, [task.name, envPairs, onRefresh])

  function requestClose() {
    if ((notesDirty || envDirty) && typeof window !== 'undefined' && !window.confirm('Discard unsaved changes?')) {
      return
    }

    onClose()
  }

  function handlePanelBackdropClick() {
    if (suppressNextCloseRef.current) {
      suppressNextCloseRef.current = false
      return
    }

    requestClose()
  }

  const addEnvPair = useCallback(() => {
    const pair = createEnvPair()
    setEnvPairs(current => [...current, pair])
    setPendingEnvFocusId(pair.id)
    markEnvDirty()
  }, [markEnvDirty])

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
  const duplicateEnvKeys = getDuplicateEnvKeys(envPairs)
  const envValidationMessage = getEnvValidationMessage(envPairs)
  const envSaveDisabled = saving || !envDirty || Boolean(envValidationMessage)
  const envSaveButtonLabel = saving ? 'Saving...' : envSaveStatus === 'saved' ? 'Saved' : 'Save'
  const envFeedback = envValidationMessage || envSaveError
  const envFeedbackIsError = envSaveStatus === 'error' || Boolean(envValidationMessage)
  const envSaveTitle = envValidationMessage || (envDirty ? 'Save environment variables' : 'No environment changes to save')

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/30" onClick={handlePanelBackdropClick} />
      <div
        className="animate-slide-in relative flex h-full min-w-[360px] max-w-[calc(100vw-8px)] flex-col border-l border-border-subtle bg-surface-raised"
        style={{ width: panelWidth }}
        onClick={event => event.stopPropagation()}
      >
        <button
          type="button"
          aria-label="Resize task detail panel"
          aria-orientation="vertical"
          onPointerDown={startPanelResize}
          className={clsx(
            'group absolute left-0 top-0 z-20 h-full w-5 -translate-x-2.5 cursor-col-resize touch-none focus:outline-none focus:ring-2 focus:ring-accent/35',
            resizingPanel ? 'bg-accent/10' : 'bg-transparent',
          )}
        >
          <span
            aria-hidden="true"
            className={clsx(
              'absolute left-1/2 top-0 h-full w-px -translate-x-1/2 transition-colors',
              resizingPanel ? 'bg-accent/70' : 'bg-border-subtle group-hover:bg-accent/45',
            )}
          />
        </button>
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
            onClick={requestClose}
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
              <div className="grid grid-cols-[minmax(120px,2fr)_minmax(160px,3fr)_32px] gap-2 px-0.5 text-2xs font-medium text-txt-tertiary">
                <span>Key</span>
                <span>Value</span>
                <span className="sr-only">Actions</span>
              </div>

              {envPairs.length === 0 && (
                <div className="rounded-md border border-dashed border-border-subtle px-3 py-5 text-center text-xs text-txt-tertiary">
                  No environment variables
                </div>
              )}

              {envPairs.map(pair => {
                const normalizedKey = pair.key.trim()
                const keyHasError = (!normalizedKey && pair.value.trim()) || duplicateEnvKeys.has(normalizedKey)

                return (
                <div key={pair.id} className="grid grid-cols-[minmax(120px,2fr)_minmax(160px,3fr)_32px] items-center gap-2">
                  <input
                    ref={node => { envKeyInputRefs.current[pair.id] = node }}
                    value={pair.key}
                    onChange={event => {
                      setEnvPairs(current => current.map(envPair => (
                        envPair.id === pair.id ? { ...envPair, key: event.target.value } : envPair
                      )))
                      markEnvDirty()
                    }}
                    placeholder="KEY"
                    aria-label="Environment variable key"
                    className={clsx(
                      'w-full rounded-md border bg-surface-overlay px-2.5 py-1.5 text-xs font-mono text-txt-primary outline-none transition-colors',
                      keyHasError ? 'border-rose-400/70 focus:border-rose-400' : 'border-border-subtle focus:border-border',
                    )}
                  />
                  <input
                    value={pair.value}
                    onChange={event => {
                      setEnvPairs(current => current.map(envPair => (
                        envPair.id === pair.id ? { ...envPair, value: event.target.value } : envPair
                      )))
                      markEnvDirty()
                    }}
                    placeholder="value"
                    aria-label="Environment variable value"
                    className="w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 py-1.5 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-border"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setEnvPairs(current => current.filter(envPair => envPair.id !== pair.id))
                      markEnvDirty()
                    }}
                    className="inline-flex h-8 w-8 items-center justify-center rounded-md text-txt-secondary transition-colors hover:bg-rose-500/10 hover:text-rose-500 focus:outline-none focus:ring-2 focus:ring-rose-400/35"
                    title="Remove variable"
                    aria-label={`Remove ${pair.key.trim() || 'environment variable'}`}
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
                )
              })}

              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={addEnvPair}
                  className="inline-flex items-center gap-1.5 rounded-md border border-border-subtle px-2.5 py-1.5 text-xs font-medium text-txt-primary transition-colors hover:bg-surface-overlay focus:outline-none focus:ring-2 focus:ring-accent/35"
                  aria-label="Add environment variable"
                >
                  <Plus className="h-3.5 w-3.5" />
                  Add variable
                </button>
                <div className="flex-1" />
                <button
                  type="button"
                  onClick={() => void handleSaveEnv()}
                  disabled={envSaveDisabled}
                  title={envSaveTitle}
                  className={clsx(
                    'inline-flex items-center gap-1.5 rounded-md border px-3 py-2 text-xs font-medium transition-colors disabled:cursor-not-allowed',
                    envSaveStatus === 'saved' && !envDirty
                      ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
                      : envSaveDisabled
                        ? 'border-border-subtle text-txt-secondary opacity-60'
                        : 'border-accent bg-accent text-white hover:bg-accent-hover',
                  )}
                >
                  {saving ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  ) : envSaveStatus === 'saved' ? (
                    <CheckCircle2 className="h-3.5 w-3.5" />
                  ) : (
                    <Save className="h-3.5 w-3.5" />
                  )}
                  {envSaveButtonLabel}
                </button>
              </div>

              <div className="min-h-5 text-2xs">
                {envFeedback ? (
                  <span className={clsx(
                    'inline-flex items-center gap-1.5',
                    envFeedbackIsError ? 'text-rose-500' : 'text-txt-tertiary',
                  )}>
                    <AlertCircle className="h-3.5 w-3.5" />
                    {envFeedback}
                  </span>
                ) : envDirty ? (
                  <span className="text-amber-600 dark:text-amber-400">Unsaved changes</span>
                ) : envSaveStatus === 'saved' ? (
                  <span className="inline-flex items-center gap-1.5 text-emerald-600 dark:text-emerald-400">
                    <CheckCircle2 className="h-3.5 w-3.5" />
                    Saved
                  </span>
                ) : (
                  <span className="text-txt-tertiary">No changes</span>
                )}
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
