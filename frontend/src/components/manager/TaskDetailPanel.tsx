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
} from 'lucide-react'
import clsx from 'clsx'
import { stringify as yamlStringify } from 'yaml'
import StatusBadge from '@/components/shared/StatusBadge'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import { useTaskDetailDraftStore, useToastStore } from '@/store'
import type { Task } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import { errorMessage } from '@/utils/errors'
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
const MAX_PANEL_WIDTH = 2400
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
      }
      onRefresh()
      notify({ tone: 'success', title: 'Notes saved', detail: taskName })
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
          notify({
            tone: 'error',
            title: 'Could not load newer notes',
            detail: 'Your draft was kept. Retry Save Notes to check the latest version.',
          })
          return
        }
        setNotesConflict(true)
        setNotesSaveError('')
        onRefresh()
        notify({
          tone: 'error',
          title: 'Notes changed elsewhere',
          detail: 'Your draft was kept. Saving it again will replace the newer notes.',
        })
        return
      }
      const message = errorMessage(err)
      setNotesSaveError(`Could not save notes. Your draft is safe. ${message}`)
      notify({ tone: 'error', title: 'Could not save notes', detail: message })
    } finally {
      if (requestId === taskRequestSeqRef.current) setSaving(false)
    }
  }, [task.name, notes, onTaskUpdated, onRefresh, notify])

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
          notify({
            tone: 'error',
            title: 'Could not load newer environment',
            detail: 'Your draft was kept. Retry Save to check the latest version.',
          })
          return
        }
        setEnvConflict(true)
        setEnvSaveStatus('error')
        setEnvSaveError('')
        onRefresh()
        notify({
          tone: 'error',
          title: 'Environment changed elsewhere',
          detail: 'Your draft was kept. Saving it again will replace the newer environment.',
        })
        return
      }
      if (envDraftRevisionRef.current !== draftRevision) return
      setEnvSaveStatus('error')
      setEnvSaveError(errorMessage(err))
    } finally {
      if (requestId === taskRequestSeqRef.current) setSaving(false)
    }
  }, [task.name, envPairs, onTaskUpdated, onRefresh, notify])

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
          className="absolute inset-0 bg-black/30"
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
                'touch-target flex min-h-11 min-w-0 items-center justify-center gap-1 rounded-md px-1.5 py-2 text-xs transition-colors sm:min-h-0 sm:gap-1.5 sm:px-3 sm:py-1.5',
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
          id={`task-detail-panel-${tab}`}
          role="tabpanel"
          aria-labelledby={`task-detail-tab-${tab}`}
          className="flex-1 overflow-y-auto p-4"
        >
          {tab === 'info' && <InfoTab task={task} />}
          {tab === 'config' && <ConfigTab task={task} />}
          {tab === 'notes' && (
            <div className="flex h-full flex-col gap-3">
              <textarea
                value={notes}
                onChange={event => {
                  notesDraftRevisionRef.current += 1
                  setNotes(event.target.value)
                  setNotesDirty(true)
                }}
                placeholder="Add notes..."
                aria-label="Task notes"
                className="min-h-[220px] flex-1 resize-none rounded-lg border border-border-subtle bg-surface-overlay p-3 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-border"
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
              <button
                type="button"
                onClick={() => void handleSaveNotes()}
                disabled={saving}
                className="touch-target min-h-11 self-end rounded-md border border-border-subtle px-3 py-2 text-xs font-medium text-txt-primary transition-colors hover:bg-surface-overlay disabled:opacity-50"
              >
                <span className="inline-flex items-center gap-1.5">
                  <Save className="h-3.5 w-3.5" />
                  {notesConflict ? 'Replace Notes' : 'Save Notes'}
                </span>
              </button>
            </div>
          )}
          {tab === 'env' && (
            <div className="flex flex-col gap-3">
              <div className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)_44px] gap-2 px-0.5 text-2xs font-medium text-txt-tertiary sm:grid-cols-[minmax(120px,2fr)_minmax(160px,3fr)_44px]">
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
                const keyHasError = (!normalizedKey && pair.value.trim())
                  || duplicateEnvKeys.has(normalizedKey)
                  || Boolean(normalizedKey && !ENV_NAME_PATTERN.test(normalizedKey))

                return (
                <div key={pair.id} className="grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)_44px] items-center gap-2 sm:grid-cols-[minmax(120px,2fr)_minmax(160px,3fr)_44px]">
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
                      'touch-input min-h-11 min-w-0 w-full rounded-md border bg-surface-overlay px-2.5 py-1.5 text-xs font-mono text-txt-primary outline-none transition-colors sm:min-h-0',
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
                    className="touch-input min-h-11 min-w-0 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 py-1.5 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-border sm:min-h-0"
                  />
                  <button
                    type="button"
                    onClick={() => {
                      setEnvPairs(current => current.filter(envPair => envPair.id !== pair.id))
                      markEnvDirty()
                    }}
                    className="touch-target inline-flex h-11 w-11 items-center justify-center rounded-md text-txt-secondary transition-colors hover:bg-rose-500/10 hover:text-rose-700 focus:outline-none focus:ring-2 focus:ring-rose-400/35 dark:hover:text-rose-300"
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
                  className="touch-target inline-flex min-h-11 items-center gap-1.5 rounded-md border border-border-subtle px-2.5 py-1.5 text-xs font-medium text-txt-primary transition-colors hover:bg-surface-overlay focus:outline-none focus:ring-2 focus:ring-accent/35"
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
                    'touch-target inline-flex min-h-11 items-center gap-1.5 rounded-md border px-3 py-2 text-xs font-medium transition-colors disabled:cursor-not-allowed',
                    envSaveStatus === 'saved' && !envDirty
                      ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
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
                    envFeedbackIsError ? 'text-rose-700 dark:text-rose-300' : 'text-txt-tertiary',
                  )}>
                    <AlertCircle className="h-3.5 w-3.5" />
                    {envFeedback}
                  </span>
                ) : envDirty ? (
                  <span className="text-amber-700 dark:text-amber-300">Unsaved changes</span>
                ) : envSaveStatus === 'saved' ? (
                  <span className="inline-flex items-center gap-1.5 text-emerald-700 dark:text-emerald-300">
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
  return typeof value === 'number' ? `${value.toFixed(3)}s` : '(none)'
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
    task.records?.length ?? 0,
    task.run_index || 0
  )

  return Array.from({ length: totalRuns }, (_, index) => ({
    index: index + 1,
    start: task.start_times?.[index] || '',
    finish: task.finish_times?.[index] || '',
    pid: task.pids?.[index],
    duration: task.durations?.[index],
    exitCode: task.exit_codes?.[index],
    source: task.source_states?.[index] || '',
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
                    <span className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Duration</span>
                    <span className="break-all font-mono text-xs text-txt-primary">{formatDurationSeconds(run.duration)}</span>
                  </div>
                  <div className="grid grid-cols-[72px_minmax(0,1fr)] gap-3">
                    <span className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Exit Code</span>
                    <span className="break-all font-mono text-xs text-txt-primary">{formatScalarValue(run.exitCode)}</span>
                  </div>
                  <div className="grid grid-cols-[72px_minmax(0,1fr)] gap-3">
                    <span className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Source</span>
                    <pre className="overflow-auto whitespace-pre-wrap break-all rounded-md bg-surface-overlay/60 p-2 font-mono text-xs leading-relaxed text-txt-primary">
                      {formatScalarValue(run.source)}
                    </pre>
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
