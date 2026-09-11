import {
  useState,
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  type ComponentType,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import {
  X, FileText, Settings, StickyNote, Variable, Save, Pencil, Check, Plus, Loader2, AlertCircle, CheckCircle2,
  ChevronDown,
} from 'lucide-react'
import clsx from 'clsx'
import { stringify as yamlStringify } from 'yaml'
import StatusBadge from '@/components/shared/StatusBadge'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import CopyButton from '@/components/shared/CopyButton'
import ActionButton from '@/components/shared/ActionButton'
import { useTaskDetailDraftStore, useToastStore } from '@/store'
import type { RunEnvironment, Task } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import { errorMessage } from '@/utils/errors'
import { formatElapsedDuration, formatStoredCommand, parseTaskTimestampMillis } from '@/utils/taskRuntime'
import * as api from '@/api'

interface Props {
  task: Task
  onClose: () => void
  onTaskUpdated: (task: Task) => void
  onRefresh: () => void
}

type Tab = 'info' | 'config' | 'notes' | 'env'
type EnvPair = { id: string; key: string; value: string }
type EnvSaveStatus = 'idle' | 'saved' | 'error'

const ENV_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]*$/

const TASK_DETAIL_WIDTH_STORAGE_KEY = 'pyruns.taskDetailPanelWidth'
const DEFAULT_PANEL_WIDTH = 720
const MIN_PANEL_WIDTH = 420
const MAX_PANEL_WIDTH = 960
let nextEnvPairId = 0

function clampPanelWidth(value: number) {
  if (!Number.isFinite(value)) {
    return DEFAULT_PANEL_WIDTH
  }
  const viewportMax = typeof window === 'undefined'
    ? MAX_PANEL_WIDTH
    : Math.max(0, window.innerWidth - 8)
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

function buildEnvPairsFromEnv(env: Record<string, string> = {}): EnvPair[] {
  return Object.entries(env || {}).map(([key, value]) => createEnvPair(key, String(value)))
}

function buildEnvPairs(task: Task): EnvPair[] {
  return buildEnvPairsFromEnv(task.env || {})
}

function copyEnv(env: Record<string, string> = {}) {
  return Object.fromEntries(
    Object.entries(env || {}).map(([key, value]) => [key, String(value)])
  )
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

  const invalidPair = envPairs.find(({ key }) => key.trim() && !ENV_NAME_PATTERN.test(key.trim()))
  if (invalidPair) {
    return `Invalid environment variable name: ${invalidPair.key.trim()}`
  }

  return ''
}

export default function TaskDetailPanel({ task, onClose, onTaskUpdated, onRefresh }: Props) {
  const [tab, setTab] = useState<Tab>('info')
  const [notes, setNotes] = useState(task.notes || '')
  const [envPairs, setEnvPairs] = useState(() => buildEnvPairs(task))
  const [saving, setSaving] = useState(false)
  const [renaming, setRenaming] = useState(false)
  const [newName, setNewName] = useState(task.name)
  const [notesDirty, setNotesDirty] = useState(false)
  const [notesSaved, setNotesSaved] = useState(false)
  const [notesConflict, setNotesConflict] = useState(false)
  const [notesSaveError, setNotesSaveError] = useState('')
  const [envDirty, setEnvDirty] = useState(false)
  const [envConflict, setEnvConflict] = useState(false)
  const [envSaveStatus, setEnvSaveStatus] = useState<EnvSaveStatus>('idle')
  const [envSaveError, setEnvSaveError] = useState('')
  const [pendingEnvFocusId, setPendingEnvFocusId] = useState<string | null>(null)
  const [panelWidth, setPanelWidth] = useState(readStoredPanelWidth)
  const [resizingPanel, setResizingPanel] = useState(false)
  const [discardConfirmOpen, setDiscardConfirmOpen] = useState(false)
  const previousTaskNameRef = useRef(task.name)
  const currentTaskNameRef = useRef(task.name)
  const envKeyInputRefs = useRef<Record<string, HTMLInputElement | null>>({})
  const suppressNextCloseRef = useRef(false)
  const backdropPointerStartedRef = useRef(false)
  const pendingPanelWidthRef = useRef(panelWidth)
  const panelResizeFrameRef = useRef<number | null>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const previousFocusRef = useRef<HTMLElement | null>(null)
  const taskRequestSeqRef = useRef(0)
  const notesDraftRevisionRef = useRef(0)
  const envDraftRevisionRef = useRef(0)
  const envBaseRef = useRef(copyEnv(task.env || {}))
  const notesBaseRef = useRef(task.notes || '')
  const notify = useToastStore(state => state.notify)
  const setTaskDetailDraftDirty = useTaskDetailDraftStore(state => state.setDirty)
  const clearTaskDetailDraft = useTaskDetailDraftStore(state => state.clear)
  currentTaskNameRef.current = task.name

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

  const resizePanelByKeyboard = useCallback((event: ReactKeyboardEvent<HTMLButtonElement>) => {
    let nextWidth: number | null = null
    if (event.key === 'ArrowLeft') nextWidth = panelWidth + 16
    if (event.key === 'ArrowRight') nextWidth = panelWidth - 16
    if (event.key === 'Home') nextWidth = MIN_PANEL_WIDTH
    if (event.key === 'End') nextWidth = MAX_PANEL_WIDTH
    if (nextWidth == null) return

    event.preventDefault()
    const next = clampPanelWidth(nextWidth)
    pendingPanelWidthRef.current = next
    setPanelWidth(next)
    try {
      window.localStorage.setItem(TASK_DETAIL_WIDTH_STORAGE_KEY, String(next))
    } catch {
      // Keyboard resizing remains usable without persisted preferences.
    }
  }, [panelWidth])

  useEffect(() => {
    const previousTaskName = previousTaskNameRef.current
    previousTaskNameRef.current = task.name

    if (previousTaskName === task.name) {
      return
    }

    notesDraftRevisionRef.current += 1
    envDraftRevisionRef.current += 1
    setTab('info')
    setNotes(task.notes || '')
    setEnvPairs(buildEnvPairs(task))
    setNewName(task.name)
    setSaving(false)
    setRenaming(false)
    setNotesDirty(false)
    setNotesSaved(false)
    setNotesConflict(false)
    setNotesSaveError('')
    setEnvDirty(false)
    setEnvConflict(false)
    setEnvSaveStatus('idle')
    setEnvSaveError('')
    setPendingEnvFocusId(null)
    setDiscardConfirmOpen(false)
    notesBaseRef.current = task.notes || ''
    envBaseRef.current = copyEnv(task.env || {})
  }, [task.name])

  useEffect(() => () => {
    taskRequestSeqRef.current += 1
  }, [task.name])

  useEffect(() => {
    if (notesDirty || previousTaskNameRef.current !== task.name) {
      return
    }
    const incomingNotes = task.notes || ''
    notesBaseRef.current = incomingNotes
    setNotes(incomingNotes)
    setNotesConflict(false)
    setNotesSaveError('')
  }, [task.name, task.notes, notesDirty])

  useEffect(() => {
    if (envDirty || previousTaskNameRef.current !== task.name) {
      return
    }
    const incomingEnv = copyEnv(task.env || {})
    envBaseRef.current = incomingEnv
    setEnvConflict(false)
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
    window.addEventListener('pointercancel', stopResize, { once: true })

    return () => {
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', stopResize)
      window.removeEventListener('pointercancel', stopResize)
      if (panelResizeFrameRef.current != null) {
        window.cancelAnimationFrame(panelResizeFrameRef.current)
        panelResizeFrameRef.current = null
      }
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousUserSelect
    }
  }, [resizingPanel])

  const markEnvDirty = useCallback(() => {
    envDraftRevisionRef.current += 1
    setEnvDirty(true)
    setEnvSaveStatus('idle')
    setEnvSaveError('')
  }, [])

  const handleSaveNotes = useCallback(async () => {
    const requestId = ++taskRequestSeqRef.current
    const taskName = task.name
    const draftRevision = notesDraftRevisionRef.current
    const expectedNotes = notesBaseRef.current
    setSaving(true)
    setNotesSaved(false)
    setNotesSaveError('')
    try {
      const response = await api.updateNotes(taskName, notes, expectedNotes)
      if (requestId !== taskRequestSeqRef.current || currentTaskNameRef.current !== taskName) return
      const savedNotes = response.task?.notes ?? notes
      notesBaseRef.current = savedNotes
      onTaskUpdated(response.task)
      setNotesConflict(false)
      setNotesSaveError('')
      if (notesDraftRevisionRef.current === draftRevision) {
        setNotes(savedNotes)
        setNotesDirty(false)
        setNotesSaved(true)
      }
      onRefresh()
    } catch (err) {
      if (requestId !== taskRequestSeqRef.current || currentTaskNameRef.current !== taskName) return
      if (err instanceof api.ApiError && err.status === 409) {
        try {
          const latestTask = await api.getTask(taskName, true)
          if (requestId !== taskRequestSeqRef.current || currentTaskNameRef.current !== taskName) return
          notesBaseRef.current = latestTask.notes || ''
        } catch {
          if (requestId !== taskRequestSeqRef.current || currentTaskNameRef.current !== taskName) return
          setNotesConflict(false)
          setNotesSaveError('Newer notes exist, but their latest version could not be loaded. Your draft is safe. Retry Save Notes before replacing anything.')
          onRefresh()
          return
        }
        setNotesConflict(true)
        setNotesSaveError('')
        onRefresh()
        return
      }
      const message = errorMessage(err)
      setNotesSaveError(`Could not save notes. Your draft is safe. ${message}`)
    } finally {
      if (requestId === taskRequestSeqRef.current) setSaving(false)
    }
  }, [task.name, notes, onTaskUpdated, onRefresh])

  const handleSaveEnv = useCallback(async () => {
    const validationMessage = getEnvValidationMessage(envPairs)
    if (validationMessage) {
      setEnvSaveStatus('error')
      setEnvSaveError(validationMessage)
      return
    }

    const requestId = ++taskRequestSeqRef.current
    const taskName = task.name
    const draftRevision = envDraftRevisionRef.current
    const expectedEnv = envBaseRef.current
    setSaving(true)
    setEnvSaveStatus('idle')
    setEnvSaveError('')
    const env = Object.fromEntries(
      envPairs
        .filter(({ key }) => key.trim())
        .map(({ key, value }) => [key.trim(), value])
    )
    try {
      const response = await api.updateEnv(taskName, env, expectedEnv)
      if (requestId !== taskRequestSeqRef.current || currentTaskNameRef.current !== taskName) return
      const savedEnv = copyEnv(response.task?.env || env)
      envBaseRef.current = savedEnv
      onTaskUpdated(response.task)
      setEnvConflict(false)
      if (envDraftRevisionRef.current === draftRevision) {
        setEnvPairs(buildEnvPairsFromEnv(savedEnv))
        setEnvDirty(false)
        setEnvSaveStatus('saved')
      }
      onRefresh()
    } catch (err) {
      if (requestId !== taskRequestSeqRef.current || currentTaskNameRef.current !== taskName) return
      if (err instanceof api.ApiError && err.status === 409) {
        try {
          const latestTask = await api.getTask(taskName, true)
          if (requestId !== taskRequestSeqRef.current || currentTaskNameRef.current !== taskName) return
          envBaseRef.current = copyEnv(latestTask.env || {})
        } catch {
          if (requestId !== taskRequestSeqRef.current || currentTaskNameRef.current !== taskName) return
          setEnvConflict(false)
          setEnvSaveStatus('error')
          setEnvSaveError('Newer environment variables exist, but their latest version could not be loaded. Your draft is safe. Retry Save before replacing anything.')
          onRefresh()
          return
        }
        setEnvConflict(true)
        setEnvSaveStatus('error')
        setEnvSaveError('')
        onRefresh()
        return
      }
      if (envDraftRevisionRef.current !== draftRevision) return
      setEnvSaveStatus('error')
      setEnvSaveError(errorMessage(err))
    } finally {
      if (requestId === taskRequestSeqRef.current) setSaving(false)
    }
  }, [task.name, envPairs, onTaskUpdated, onRefresh])

  function requestClose() {
    if (hasUnsavedChanges) {
      setDiscardConfirmOpen(true)
      return
    }

    clearTaskDetailDraft(task.name)
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
    if (notesDirty || envDirty) {
      notify({
        tone: 'info',
        title: 'Save task details before renaming',
        detail: 'Save or discard the Notes and Env changes first.',
      })
      return
    }

    const requestId = ++taskRequestSeqRef.current
    const taskName = task.name
    setSaving(true)
    try {
      await api.renameTask(taskName, newName.trim())
      if (requestId !== taskRequestSeqRef.current || currentTaskNameRef.current !== taskName) return
      onRefresh()
      clearTaskDetailDraft(taskName)
      onClose()
      notify({ tone: 'success', title: 'Task renamed', detail: newName.trim() })
    } catch (err) {
      if (requestId !== taskRequestSeqRef.current || currentTaskNameRef.current !== taskName) return
      setNewName(task.name)
      notify({ tone: 'error', title: 'Could not rename task', detail: errorMessage(err) })
    } finally {
      if (requestId === taskRequestSeqRef.current) {
        setSaving(false)
        setRenaming(false)
      }
    }
  }, [clearTaskDetailDraft, envDirty, task.name, newName, notesDirty, onRefresh, onClose, notify])

  const tabs: { key: Tab; label: string; icon: ComponentType<{ className?: string }> }[] = [
    { key: 'info', label: 'Info', icon: FileText },
    { key: 'config', label: isShellTask(task) ? 'Script' : 'Config', icon: Settings },
    { key: 'notes', label: 'Notes', icon: StickyNote },
    { key: 'env', label: 'Env', icon: Variable },
  ]
  const duplicateEnvKeys = getDuplicateEnvKeys(envPairs)
  const envValidationMessage = getEnvValidationMessage(envPairs)
  const envSaveDisabled = saving || !envDirty || Boolean(envValidationMessage)
  const envSaveButtonLabel = saving
    ? 'Saving...'
    : envConflict
      ? 'Replace Env'
      : envSaveStatus === 'saved'
        ? 'Saved'
        : 'Save'
  const envFeedback = envValidationMessage
    || (envConflict ? 'Another editor saved newer environment variables. Your draft is unchanged.' : envSaveError)
  const envFeedbackIsError = envConflict || envSaveStatus === 'error' || Boolean(envValidationMessage)
  const envSaveTitle = envValidationMessage
    || (envConflict
      ? 'Replace the newer environment variables with this draft'
      : envDirty
        ? 'Save environment variables'
        : 'No environment changes to save')
  const notesFeedback = notesConflict
    ? 'Another editor saved newer notes. Your draft is unchanged.'
    : notesSaveError
  const renameDirty = renaming && newName.trim() !== '' && newName.trim() !== task.name
  const hasUnsavedChanges = notesDirty || envDirty || renameDirty

  useLayoutEffect(() => {
    setTaskDetailDraftDirty(task.name, hasUnsavedChanges)
  }, [hasUnsavedChanges, setTaskDetailDraftDirty, task.name])

  useEffect(() => () => {
    clearTaskDetailDraft(task.name)
  }, [clearTaskDetailDraft, task.name])

  useEffect(() => {
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    const focusFrame = window.requestAnimationFrame(() => closeButtonRef.current?.focus())
    return () => {
      window.cancelAnimationFrame(focusFrame)
      const previousFocus = previousFocusRef.current
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  }, [])

  useEffect(() => {
    if (discardConfirmOpen) return

    const handleKeyDown = (event: KeyboardEvent) => {
      if (document.querySelector('dialog[open]')) {
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        if (hasUnsavedChanges) {
          setDiscardConfirmOpen(true)
        } else {
          clearTaskDetailDraft(task.name)
          onClose()
        }
        return
      }
      if (event.key !== 'Tab') return

      const focusable = Array.from(panelRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      ) || []).filter(element => element.offsetParent !== null)
      if (focusable.length === 0) {
        event.preventDefault()
        panelRef.current?.focus()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || !panelRef.current?.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || !panelRef.current?.contains(active))) {
        event.preventDefault()
        first.focus()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [clearTaskDetailDraft, discardConfirmOpen, hasUnsavedChanges, onClose, task.name])

  return (
    <>
      <div className="fixed inset-0 z-50 flex justify-end">
        <div
          className="absolute inset-0 bg-black/40"
          onPointerDown={event => {
            backdropPointerStartedRef.current = event.target === event.currentTarget
          }}
          onClick={event => {
            if (backdropPointerStartedRef.current && event.target === event.currentTarget) {
              handlePanelBackdropClick()
            }
            backdropPointerStartedRef.current = false
          }}
        />
        <div
          ref={panelRef}
          role="dialog"
          aria-modal="true"
          aria-labelledby="task-detail-title"
          tabIndex={-1}
          className="animate-slide-in relative flex h-full min-w-0 max-w-[calc(100vw-8px)] flex-col border-l border-border-subtle bg-surface-raised"
          style={{ width: panelWidth }}
          onPointerDown={() => {
            backdropPointerStartedRef.current = false
          }}
          onClick={event => event.stopPropagation()}
        >
        <span id="task-detail-title" className="sr-only">Task details for {task.name}</span>
        <button
          type="button"
          role="separator"
          tabIndex={0}
          aria-label="Resize task detail panel"
          aria-orientation="vertical"
          aria-valuemin={Math.min(MIN_PANEL_WIDTH, clampPanelWidth(MAX_PANEL_WIDTH))}
          aria-valuemax={clampPanelWidth(MAX_PANEL_WIDTH)}
          aria-valuenow={panelWidth}
          onPointerDown={startPanelResize}
          onKeyDown={resizePanelByKeyboard}
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
                  className="touch-input w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 py-1.5 text-sm text-txt-primary outline-none focus:border-border"
                />
                <button
                  type="button"
                  onClick={() => void handleRename()}
                  title="Save name"
                  aria-label="Save task name"
                  className="touch-target inline-flex h-11 w-11 items-center justify-center rounded-md text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary sm:h-8 sm:w-8"
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
                  aria-label="Cancel task rename"
                  className="touch-target inline-flex h-11 w-11 items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary sm:h-8 sm:w-8"
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            ) : (
              <div className="flex items-center gap-2">
                <span className="min-w-0 select-text break-all text-sm font-medium text-txt-primary">{task.name}</span>
                <button
                  type="button"
                  onClick={() => setRenaming(true)}
                  title="Rename task"
                  aria-label="Rename task"
                  className="touch-target inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary sm:h-8 sm:w-8"
                >
                  <Pencil className="h-3 w-3" />
                </button>
              </div>
            )}
          </div>

          <button
            ref={closeButtonRef}
            type="button"
            onClick={requestClose}
            className="touch-target inline-flex h-11 w-11 shrink-0 items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary sm:h-8 sm:w-8"
            title="Close"
            aria-label="Close task details"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div role="tablist" aria-label="Task detail sections" className="grid grid-cols-4 gap-1 border-b border-border-subtle px-3 py-2">
          {tabs.map(({ key, label, icon: Icon }) => (
            <button
              key={key}
              type="button"
              id={`task-detail-tab-${key}`}
              role="tab"
              aria-selected={tab === key}
              aria-controls={`task-detail-panel-${key}`}
              tabIndex={tab === key ? 0 : -1}
              onClick={() => setTab(key)}
              onKeyDown={event => {
                if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
                event.preventDefault()
                const currentIndex = tabs.findIndex(item => item.key === key)
                const nextIndex = event.key === 'Home'
                  ? 0
                  : event.key === 'End'
                    ? tabs.length - 1
                    : (currentIndex + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
                const nextTab = tabs[nextIndex].key
                setTab(nextTab)
                panelRef.current?.querySelector<HTMLElement>(`#task-detail-tab-${nextTab}`)?.focus()
              }}
              className={clsx(
                'touch-target flex min-h-11 min-w-0 items-center justify-center gap-1 rounded-md px-1.5 py-2 text-xs font-medium transition-colors sm:min-h-9 sm:gap-1.5 sm:px-3 sm:py-1.5',
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

        <div
          key={tab}
          id={`task-detail-panel-${tab}`}
          role="tabpanel"
          aria-labelledby={`task-detail-tab-${tab}`}
          className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5"
        >
          {tab === 'info' && <InfoTab task={task} />}
          {tab === 'config' && <ConfigTab task={task} />}
          {tab === 'notes' && (
            <div className="flex min-h-full flex-col gap-3">
              <label htmlFor="task-notes" className="text-sm font-semibold text-txt-primary">Notes</label>
              <textarea
                id="task-notes"
                value={notes}
                onChange={event => {
                  notesDraftRevisionRef.current += 1
                  setNotes(event.target.value)
                  setNotesDirty(true)
                  setNotesSaved(false)
                }}
                placeholder="Add notes..."
                aria-label="Task notes"
                className="min-h-[220px] flex-1 resize-y rounded-lg border border-border bg-surface-base p-3 text-sm leading-relaxed text-txt-primary placeholder:text-txt-secondary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50"
              />
              {notesFeedback && (
                <div
                  role="alert"
                  className={clsx(
                    'rounded-md border px-3 py-2 text-xs',
                    notesConflict
                      ? 'border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300'
                      : 'border-rose-500/30 bg-rose-500/10 text-rose-700 dark:text-rose-300',
                  )}
                >
                  {notesFeedback}
                </div>
              )}
              <div className="sticky bottom-0 flex flex-wrap items-center justify-between gap-2 border-t border-border-subtle bg-surface-raised py-3">
                <span role="status" className="text-xs text-txt-secondary">{notesFeedback ? '' : notesDirty ? 'Unsaved changes' : notesSaved ? 'Saved' : ''}</span>
                <ActionButton
                  onClick={() => void handleSaveNotes()}
                  disabled={saving || !notesDirty}
                  variant="primary"
                  icon={saving ? <Loader2 className="h-3.5 w-3.5 motion-safe:animate-spin" /> : <Save className="h-3.5 w-3.5" />}
                >
                  {saving ? 'Saving...' : notesConflict ? 'Replace Notes' : 'Save Notes'}
                </ActionButton>
              </div>
            </div>
          )}
          {tab === 'env' && (
            <div className="flex flex-col gap-3">
              <div className="space-y-1">
                <h3 className="text-sm font-semibold text-txt-primary">Environment Variables</h3>
                <p className="text-xs leading-5 text-txt-secondary">Task overrides applied on the next run.</p>
              </div>

              {envPairs.length > 0 && (
                <div className="hidden grid-cols-[minmax(0,2fr)_minmax(0,3fr)_36px] gap-2 px-0.5 text-xs font-medium text-txt-secondary sm:grid">
                  <span>Key</span>
                  <span>Value</span>
                  <span className="sr-only">Actions</span>
                </div>
              )}

              {envPairs.length === 0 && (
                <div className="rounded-lg border border-dashed border-border bg-surface-base px-3 py-6 text-center text-xs text-txt-secondary">
                  No environment variables
                </div>
              )}

              {envPairs.map(pair => {
                const normalizedKey = pair.key.trim()
                const keyHasError = (!normalizedKey && pair.value.trim())
                  || duplicateEnvKeys.has(normalizedKey)
                  || Boolean(normalizedKey && !ENV_NAME_PATTERN.test(normalizedKey))

                return (
                <div key={pair.id} className="grid grid-cols-[minmax(0,1fr)_44px] items-start gap-2 rounded-lg border border-border bg-surface-base p-3 sm:grid-cols-[minmax(0,2fr)_minmax(0,3fr)_36px] sm:rounded-none sm:border-0 sm:bg-transparent sm:p-0">
                  <label className="min-w-0 space-y-1">
                    <span className="text-xs font-medium text-txt-secondary sm:hidden">Key</span>
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
                      aria-invalid={Boolean(keyHasError)}
                      aria-describedby={keyHasError ? 'task-env-feedback' : undefined}
                      className={clsx(
                        'touch-input min-h-11 min-w-0 w-full rounded-md border bg-surface-base px-2.5 py-1.5 font-mono text-xs text-txt-primary placeholder:text-txt-secondary transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 sm:min-h-9',
                        keyHasError ? 'border-rose-400' : 'border-border',
                      )}
                    />
                  </label>
                  <label className="col-start-1 row-start-2 min-w-0 space-y-1 sm:col-auto sm:row-auto">
                    <span className="text-xs font-medium text-txt-secondary sm:hidden">Value</span>
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
                      className="touch-input min-h-11 min-w-0 w-full rounded-md border border-border bg-surface-base px-2.5 py-1.5 font-mono text-xs text-txt-primary placeholder:text-txt-secondary transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/50 sm:min-h-9"
                    />
                  </label>
                  <button
                    type="button"
                    onClick={() => {
                      setEnvPairs(current => current.filter(envPair => envPair.id !== pair.id))
                      markEnvDirty()
                    }}
                    className="touch-target col-start-2 row-start-1 inline-flex h-11 w-11 items-center justify-center rounded-md text-txt-secondary transition-colors hover:bg-rose-500/10 hover:text-rose-700 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-rose-400/50 dark:hover:text-rose-300 sm:col-auto sm:row-auto sm:h-9 sm:w-9"
                    title="Remove variable"
                    aria-label={`Remove ${pair.key.trim() || 'environment variable'}`}
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </div>
                )
              })}

              <div className="sticky bottom-0 flex flex-wrap items-center justify-between gap-2 border-t border-border-subtle bg-surface-raised py-3">
                <ActionButton
                  onClick={addEnvPair}
                  variant="secondary"
                  icon={<Plus className="h-3.5 w-3.5" />}
                  aria-label="Add environment variable"
                >
                  Add variable
                </ActionButton>
                <ActionButton
                  onClick={() => void handleSaveEnv()}
                  disabled={envSaveDisabled}
                  title={envSaveTitle}
                  variant={envSaveStatus === 'saved' && !envDirty ? 'secondary' : 'primary'}
                  className={envSaveStatus === 'saved' && !envDirty ? 'disabled:!opacity-100' : undefined}
                  icon={saving
                    ? <Loader2 className="h-3.5 w-3.5 motion-safe:animate-spin" />
                    : envSaveStatus === 'saved'
                      ? <CheckCircle2 className="h-3.5 w-3.5" />
                      : <Save className="h-3.5 w-3.5" />}
                >
                  {envSaveButtonLabel}
                </ActionButton>
              </div>

              <div id="task-env-feedback" role={envFeedback ? 'alert' : 'status'} className="text-xs">
                {envFeedback ? (
                  <span className={clsx(
                    'inline-flex items-start gap-1.5 [overflow-wrap:anywhere]',
                    envFeedbackIsError ? 'text-rose-700 dark:text-rose-300' : 'text-txt-secondary',
                  )}>
                    <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    {envFeedback}
                  </span>
                ) : envDirty ? (
                  <span className="text-amber-700 dark:text-amber-300">Unsaved changes</span>
                ) : null}
              </div>
            </div>
          )}
        </div>
        </div>
      </div>
      <ConfirmDialog
        open={discardConfirmOpen}
        title="Discard changes?"
        description="Unsaved task details will be lost."
        confirmLabel="Discard"
        confirmVariant="danger"
        onConfirm={() => {
          setDiscardConfirmOpen(false)
          clearTaskDetailDraft(task.name)
          onClose()
        }}
        onCancel={() => setDiscardConfirmOpen(false)}
      />
    </>
  )
}

function isShellTask(task: Task) {
  return task.task_kind === 'shell'
}

function getTaskMode(task: Task): string {
  return isShellTask(task) ? 'shell' : 'python'
}

function formatScalarValue(value: unknown): string {
  if (value === null || value === undefined || value === '') {
    return '(none)'
  }
  return String(value)
}

function formatDurationSeconds(value: number | null | undefined): string {
  return typeof value === 'number' && Number.isFinite(value) ? `${value.toFixed(3)}s` : '(none)'
}

function formatRunDuration(
  value: number | null | undefined,
  start: string | number | null | undefined,
  finish: string | number | null | undefined,
): string {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return formatDurationSeconds(value)
  }

  const startMillis = parseTaskTimestampMillis(start)
  const finishMillis = parseTaskTimestampMillis(finish)
  if (startMillis === null || finishMillis === null || finishMillis < startMillis) {
    return '(none)'
  }

  return formatDurationSeconds((finishMillis - startMillis) / 1000)
}

function formatTimestampValue(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '(none)'
  if (typeof value === 'number' || /^\d+(?:\.\d+)?$/.test(String(value).trim())) {
    const millis = parseTaskTimestampMillis(value)
    return millis === null ? String(value) : new Date(millis).toLocaleString()
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
    task.durations?.length ?? 0,
    task.exit_codes?.length ?? 0,
    task.source_states?.length ?? 0,
    task.run_environments?.length ?? 0,
    task.run_statuses?.length ?? 0,
    task.records?.length ?? 0,
  )

  return Array.from({ length: totalRuns }, (_, index) => ({
    index: index + 1,
    start: task.start_times?.[index] || '',
    finish: task.finish_times?.[index] || '',
    pid: task.pids?.[index],
    duration: task.durations?.[index],
    exitCode: task.exit_codes?.[index],
    source: task.source_states?.[index] || '',
    environment: task.run_environments?.[index],
    status: task.run_statuses?.[index] || '',
    record: task.records?.[index],
    recordText: formatRecordValue(task.records?.[index]),
  }))
}

function gpuAssignmentLabel(task: Task): string {
  const assignment = task._gpu_assignment
  const assigned = assignment?.gpu_ids?.length
    ? assignment.gpu_ids.join(',')
    : assignment?.env?.PYRUNS_ASSIGNED_GPUS || task.env?.PYRUNS_ASSIGNED_GPUS || ''
  const visible = assignment?.cuda_visible_devices
    || assignment?.env?.CUDA_VISIBLE_DEVICES
    || task.env?.CUDA_VISIBLE_DEVICES
    || ''
  if (assigned && visible && assigned !== visible) {
    return `GPU ${assigned} | CUDA_VISIBLE_DEVICES=${visible}`
  }
  if (assigned) return `GPU ${assigned}`
  if (visible) return `CUDA_VISIBLE_DEVICES=${visible}`
  return ''
}

function InfoValueRow({
  label,
  value,
  copyLabel,
  detail,
  mono = false,
}: {
  label: string
  value: string
  copyLabel?: string
  detail?: string
  mono?: boolean
}) {
  return (
    <div className="grid grid-cols-[88px_minmax(0,1fr)] items-start gap-3 border-b border-border-subtle py-2.5 last:border-b-0 sm:grid-cols-[112px_minmax(0,1fr)]">
      <span className="text-xs font-medium leading-6 text-txt-secondary">{label}</span>
      <div className="flex min-w-0 items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className={clsx('whitespace-pre-wrap text-xs leading-6 text-txt-primary [overflow-wrap:anywhere]', mono && 'font-mono')}>
            {value}
          </div>
          {detail && <div className="mt-0.5 whitespace-pre-wrap font-mono text-2xs leading-5 text-txt-secondary [overflow-wrap:anywhere]">{detail}</div>}
        </div>
        {copyLabel && value !== '(none)' && (
          <CopyButton value={[value, detail].filter(Boolean).join('\n')} label={copyLabel} size="xs" />
        )}
      </div>
    </div>
  )
}

function RunMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 bg-surface-raised px-3 py-2.5">
      <dt className="text-xs text-txt-secondary">{label}</dt>
      <dd className="mt-1 break-words text-xs font-medium tabular-nums text-txt-primary" title={value}>
        {value}
      </dd>
    </div>
  )
}

function RunEnvironmentRows({ environment }: { environment?: RunEnvironment | null }) {
  if (!environment) {
    return <div className="py-2 text-xs text-txt-secondary">Environment was not recorded for this run.</div>
  }
  const groups = new Map<string, number[]>()
  for (const gpu of environment.gpus) {
    const capacity = gpu.memory_total_mb != null && gpu.memory_total_mb > 0
      ? ` · ${(gpu.memory_total_mb / 1024).toFixed(0)} GiB`
      : ''
    const label = `${gpu.name}${capacity}`
    groups.set(label, [...(groups.get(label) || []), gpu.index])
  }
  const devices = [...groups].map(([label, indexes]) =>
    `${indexes.length} × ${label} · GPU ${indexes.join(', ')}`,
  ).join('\n')
  const scope = { assigned: 'Assigned', visible: 'Visible', detected: 'Detected', disabled: 'Disabled' }[environment.gpu_scope]
  let gpuValue = environment.gpu_scope === 'disabled'
    ? 'No visible CUDA GPUs'
    : environment.gpu_status !== 'ok'
      ? 'GPU information unavailable'
      : devices ? `${scope}: ${devices}` : 'No NVIDIA GPUs detected'
  if (environment.gpu_status !== 'ok' && environment.assigned_gpu_ids.length) {
    gpuValue += ` · Assigned GPU ${environment.assigned_gpu_ids.join(', ')}`
  }
  const visibility = environment.cuda_visible_devices != null
    ? `CUDA_VISIBLE_DEVICES=${environment.cuda_visible_devices || '(empty)'}`
    : 'CUDA visibility unrestricted'
  const rows = [
    ['Host', environment.host],
    ['System', environment.system],
    ['GPU', gpuValue],
    ['Launcher', [environment.launcher, environment.conda_env && `Conda: ${environment.conda_env}`].filter(Boolean).join(' · ')],
  ]
  return <>{rows.map(([label, value]) => (
    <InfoValueRow
      key={label}
      label={label}
      value={value || 'Not recorded'}
      detail={label === 'GPU' ? visibility : undefined}
      mono={label === 'Launcher'}
      copyLabel={value ? `Copy ${label.toLowerCase()}` : undefined}
    />
  ))}</>
}

function InfoTab({ task }: { task: Task }) {
  const runs = buildRunEntries(task)
  const [openRunIndexes, setOpenRunIndexes] = useState<Set<number>>(
    () => new Set(),
  )
  const previousRunTaskRef = useRef(task.name)
  useEffect(() => {
    const taskChanged = previousRunTaskRef.current !== task.name
    if (taskChanged) {
      setOpenRunIndexes(new Set<number>())
    }
    previousRunTaskRef.current = task.name
  }, [task.name])

  const live = task.status === 'running' || task.status === 'queued'
  const [nowMillis, setNowMillis] = useState(Date.now)
  useEffect(() => {
    setNowMillis(Date.now())
    if (!live) return
    const interval = window.setInterval(() => setNowMillis(Date.now()), 1000)
    return () => window.clearInterval(interval)
  }, [live, task.name])

  const rows: [string, string][] = [
    ['Created', task.created_at],
    ['Mode', getTaskMode(task)],
    ['Recorded Runs', String(runs.length)],
    ['Directory', task.dir],
  ]

  if (task._load_error) {
    rows.push(['Load Error', task._load_error])
  }

  const queuedRunIndex = Number(task.gpu_wait?.run_index || 0)
  const displayedRunIndex = task.status === 'queued'
    ? Math.max(1, queuedRunIndex || (task.run_index > runs.length ? task.run_index : runs.length + 1))
    : task.status === 'running'
      ? Math.max(1, task.run_index || runs.length)
      : runs.length
  const displayedRun = displayedRunIndex > 0 ? runs[displayedRunIndex - 1] : undefined
  const launchMatchesRun = Boolean(
    task.launch_command
    && (!task.launch_run_index || task.launch_run_index === displayedRunIndex),
  )
  const runStartMillis = task.status === 'queued'
    ? parseTaskTimestampMillis(task.gpu_wait?.started_at ?? task.queued_at)
    : launchMatchesRun
      ? parseTaskTimestampMillis(task.launch_started_at)
      : parseTaskTimestampMillis(displayedRun?.start)
  const elapsedSeconds = runStartMillis === null ? null : Math.max(0, (nowMillis - runStartMillis) / 1000)
  const durationLabel = task.status === 'queued' ? 'Waited' : task.status === 'running' ? 'Elapsed' : 'Duration'
  const durationValue = live
    ? elapsedSeconds === null ? '(none)' : formatElapsedDuration(elapsedSeconds)
    : formatRunDuration(displayedRun?.duration, displayedRun?.start, displayedRun?.finish)
  const displayedStatus = task.status === 'queued' || task.status === 'running'
    ? task.status
    : displayedRun?.status || task.status
  const displayedStart = task.status === 'queued'
    ? formatTimestampValue(task.queued_at ?? task.gpu_wait?.started_at)
    : formatScalarValue(displayedRun?.start)
  const running = task.status === 'running'
  const gpuLabel = running ? gpuAssignmentLabel(task) : ''
  const currentMetrics = live && displayedRunIndex > 0
    ? [
        ['Run', `#${displayedRunIndex}`],
        ['Status', displayedStatus],
        ['Started', displayedStart],
        [durationLabel, durationValue],
        ['PID', formatScalarValue(displayedRun?.pid)],
        ...(gpuLabel ? [['GPU', gpuLabel]] : []),
      ]
    : []
  const configuredCommand = formatStoredCommand(task.cmd)
  const command = launchMatchesRun ? String(task.launch_command) : configuredCommand
  const commandLabel = launchMatchesRun
    ? 'Launch Command'
    : Array.isArray(task.cmd)
      ? 'Configured argv'
      : 'Configured Command'
  const executionWorkdir = launchMatchesRun ? task.launch_workdir : task.workdir
  const shell = [task.shell_kind, task.shell_executable].filter(Boolean).join(' | ')
  const executionRows: [string, string, string?][] = [
    ...(executionWorkdir ? [['Working Directory', executionWorkdir, 'Copy working directory'] as [string, string, string]] : []),
    ...(task.script ? [['Script', task.script, 'Copy script path'] as [string, string, string]] : []),
    ...(shell ? [['Shell', shell, 'Copy shell information'] as [string, string, string]] : []),
    ...(task.command_mode && task.command_mode !== getTaskMode(task) ? [['Command Mode', task.command_mode] as [string, string]] : []),
    ...(running && task.runner_host ? [['Runner Host', task.runner_host, 'Copy runner host'] as [string, string, string]] : []),
    ...(running && task.runner_id ? [['Runner ID', task.runner_id, 'Copy runner ID'] as [string, string, string]] : []),
    ...(running && task.lease_until ? [['Lease Until', formatTimestampValue(task.lease_until)] as [string, string]] : []),
  ]

  return (
    <div className="space-y-6">
      {currentMetrics.length > 0 && (
        <section className="space-y-2" aria-labelledby="task-current-run-heading">
          <h3 id="task-current-run-heading" className="text-sm font-semibold text-txt-primary">
            {task.status === 'running' ? 'Current Run' : 'Scheduled Run'}
          </h3>
          <dl className="grid grid-cols-[repeat(auto-fit,minmax(132px,1fr))] gap-px overflow-hidden rounded-md border border-border-subtle bg-border-subtle">
            {currentMetrics.map(([label, value]) => <RunMetric key={label} label={label} value={value} />)}
          </dl>
        </section>
      )}

      <section className="space-y-2" aria-labelledby="task-overview-heading">
        <h3 id="task-overview-heading" className="text-sm font-semibold text-txt-primary">Task</h3>
        <div className="rounded-lg border border-border px-3">
          {rows.map(([label, value]) => (
            <InfoValueRow
              key={label}
              label={label}
              value={value}
              mono={label === 'Directory'}
              copyLabel={label === 'Directory' ? 'Copy task directory' : undefined}
            />
          ))}
        </div>
      </section>

      {(displayedRunIndex > 0 || live) && (
        <section className="space-y-2" aria-label="Run environment">
          <div className="flex items-center justify-between gap-2">
            <h3 className="text-sm font-semibold text-txt-primary">Run Environment</h3>
            {displayedRunIndex > 0 && <span className="rounded-md bg-surface-overlay px-2 py-1 text-xs font-medium text-txt-secondary">Run #{displayedRunIndex}</span>}
          </div>
          <div className="rounded-lg border border-border px-3">
            {task.status === 'queued' ? (
              <div className="py-3 text-xs text-txt-secondary">Environment will be recorded when this run starts.</div>
            ) : <RunEnvironmentRows environment={displayedRun?.environment} />}
          </div>
          {displayedRun?.environment && task.status !== 'queued' && (
            <p className="text-xs leading-5 text-txt-secondary">Recorded at launch · GPUs reflect assignment or visibility.</p>
          )}
        </section>
      )}

      <section className="space-y-2" aria-labelledby="task-execution-heading">
        <h3 id="task-execution-heading" className="text-sm font-semibold text-txt-primary">Execution</h3>
        {command ? (
          <div className="overflow-hidden rounded-lg border border-border bg-surface-base">
            <div className="flex items-center justify-between gap-2 border-b border-border bg-surface-overlay px-3 py-1.5">
              <span className="text-xs font-medium text-txt-secondary">{commandLabel}</span>
              <CopyButton value={command} label={`Copy ${commandLabel.toLowerCase()}`} size="xs" />
            </div>
            <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-all p-3 font-mono text-xs leading-relaxed text-txt-primary">
              {command}
            </pre>
          </div>
        ) : (
          <div className="border-b border-border-subtle py-2 text-xs text-txt-secondary">
            Launch command was not recorded for this run.
          </div>
        )}
        {executionRows.length > 0 && (
          <div className="rounded-lg border border-border px-3">
            {executionRows.map(([label, value, copyLabel]) => (
              <InfoValueRow key={label} label={label} value={value} copyLabel={copyLabel} mono={['Working Directory', 'Script', 'Shell', 'Runner ID'].includes(label)} />
            ))}
          </div>
        )}
      </section>

      <section className="space-y-2" aria-labelledby="task-history-heading">
        <h3 id="task-history-heading" className="text-sm font-semibold text-txt-primary">Run History</h3>
        {runs.length === 0 ? (
          <div className="px-0.5 py-2 text-xs text-txt-secondary">
            No runs recorded yet.
          </div>
        ) : (
          <div className="overflow-hidden rounded-lg border border-border">
            {runs.map(run => (
              <details
                key={run.index}
                open={openRunIndexes.has(run.index)}
                onToggle={event => {
                  const isOpen = event.currentTarget.open
                  setOpenRunIndexes(current => {
                    const next = new Set(current)
                    if (isOpen) {
                      next.add(run.index)
                    } else {
                      next.delete(run.index)
                    }
                    return next
                  })
                }}
                className="group border-b border-border-subtle last:border-b-0 [content-visibility:auto] [contain-intrinsic-size:48px]"
              >
                <summary className="flex min-h-11 cursor-pointer list-none items-center gap-2 px-3 py-2 text-xs transition-colors hover:bg-surface-overlay focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/35">
                  <span className="font-medium text-txt-primary">Run #{run.index}</span>
                  <span className="text-txt-secondary">{run.status || (run.index === task.run_index ? task.status : '')}</span>
                  <span className="ml-auto tabular-nums text-txt-secondary">{formatRunDuration(run.duration, run.start, run.finish)}</span>
                  <ChevronDown className="h-3.5 w-3.5 text-txt-secondary transition-transform group-open:rotate-180" />
                </summary>
                <div className="border-t border-border-subtle px-3 pb-2">
                  <InfoValueRow label="Start" value={formatScalarValue(run.start)} />
                  <InfoValueRow label="Finish" value={formatScalarValue(run.finish)} />
                  <InfoValueRow label="PID" value={formatScalarValue(run.pid)} />
                  <InfoValueRow label="Exit Code" value={formatScalarValue(run.exitCode)} />
                  {run.source && <InfoValueRow label="Source" value={run.source} mono copyLabel={`Copy source state for run ${run.index}`} />}
                  {run.index !== displayedRunIndex && (
                    <div className="my-3 rounded-md border border-border bg-surface-base px-3">
                      <h4 className="border-b border-border-subtle py-2.5 text-xs font-semibold text-txt-primary">Environment</h4>
                      <RunEnvironmentRows environment={run.environment} />
                    </div>
                  )}
                  {run.recordText !== '(empty)' && <InfoValueRow label="Record" value={run.recordText} mono copyLabel={`Copy record for run ${run.index}`} />}
                </div>
              </details>
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
      : ''
  const shell = isShellTask(task)
  const title = shell ? 'Script' : 'Configuration'

  return (
    <section className="overflow-hidden rounded-lg border border-border bg-surface-base" aria-label={title}>
      <div className="flex items-center justify-between gap-2 border-b border-border bg-surface-overlay px-3 py-2">
        <h3 className="text-sm font-semibold text-txt-primary">{title}</h3>
        {content && (
          <CopyButton value={content} label={shell ? 'Copy script' : 'Copy configuration'} size="xs" />
        )}
      </div>
      {content ? (
        <pre className="whitespace-pre-wrap p-3 font-mono text-xs leading-relaxed text-txt-primary [overflow-wrap:anywhere]">
          {content}
        </pre>
      ) : <p className="p-3 text-xs text-txt-secondary">No {shell ? 'script' : 'configuration'} recorded for this task.</p>}
    </section>
  )
}
