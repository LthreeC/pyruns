import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { Terminal as XTerminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { SearchAddon, type ISearchOptions } from '@xterm/addon-search'
import '@xterm/xterm/css/xterm.css'
import {
  AlertTriangle,
  ChevronDown,
  ChevronUp,
  Cpu,
  Download,
  FileDown,
  LoaderCircle,
  Pin,
  Play,
  RefreshCw,
  Rows3,
  Search,
  Square,
  Wifi,
  WifiOff,
  X,
} from 'lucide-react'
import clsx from 'clsx'
import { appendMonitorLogContent, useMonitorStore, useTaskStore, useToastStore, useWorkspaceStore } from '@/store'
import {
  useLogStream,
  useTaskEvents,
  type LogStreamStatus,
  type TaskEventStreamStatus,
} from '@/hooks/useWebSocket'
import { usePolling } from '@/hooks/usePolling'
import SearchInput from '@/components/shared/SearchInput'
import StatusBadge from '@/components/shared/StatusBadge'
import SelectionIndicator from '@/components/shared/SelectionIndicator'
import EmptyState from '@/components/shared/EmptyState'
import ActionButton from '@/components/shared/ActionButton'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import CompactSection from '@/components/shared/CompactSection'
import TaskDetailPanel from '@/components/manager/TaskDetailPanel'
import type { GPUWaitStatus, LogStreamMessage, Task } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import { errorMessage } from '@/utils/errors'
import * as api from '@/api'
import {
  DEFAULT_MONITOR_LINE_HEIGHT,
  DEFAULT_MONITOR_SCROLLBACK,
  resolveMonitorChunkSize,
  resolveMonitorLineHeight,
  resolveMonitorScrollback,
} from '@/utils/monitorSettings'
import { configureReadOnlyTerminalInput } from '@/utils/monitorAccessibility'

const MONITOR_SIDEBAR_WIDTH_STORAGE_KEY = 'pyruns.monitorSidebarWidthPct'
const DEFAULT_MONITOR_SIDEBAR_WIDTH = 14
const MIN_MONITOR_SIDEBAR_WIDTH = 10
const MAX_MONITOR_SIDEBAR_WIDTH = 35
const COMPACT_MONITOR_SIDEBAR_HEIGHT = 'clamp(18rem, 45vh, 24rem)'
const QUEUE_LOG_NAME = 'queue.log'
const RUN_LOG_PATTERN = /^run\d+\.log$/
// Coalesce tiny stdout chunks so carriage-return progress bars paint as one frame.
const LOG_STREAM_FLUSH_MS = 50
const TASK_EVENT_REFRESH_DEBOUNCE_MS = 120
const TASK_EVENT_FALLBACK_POLL_MS = 60_000
const TASK_EVENT_DEGRADED_POLL_MS = 5_000
const TERMINAL_SEARCH_HIGHLIGHT_LIMIT = 1000
// Keep this aligned with the fields blanked by the server's compact Monitor payload.
const COMPACT_MONITOR_DETAIL_FIELDS = new Set([
  'config',
  'config_text',
  'log',
  'env',
  'cmd',
  'start_times',
  'finish_times',
  'pids',
  'durations',
  'exit_codes',
  'source_states',
  'records',
  'tracks',
  'notes',
  'preview_text',
  'search_text',
])
const TERMINAL_SEARCH_OPTIONS: ISearchOptions = {
  decorations: {
    matchBackground: '#1F4E79',
    matchBorder: '#4FC1FF',
    matchOverviewRuler: '#4FC1FF',
    activeMatchBackground: '#5F3B00',
    activeMatchBorder: '#F59E0B',
    activeMatchColorOverviewRuler: '#F59E0B',
  },
}
type PendingLiveLogChunk = {
  content: string
  offset?: number
  logIdentity?: string
}

function clampMonitorSidebarWidth(value: number) {
  if (!Number.isFinite(value)) {
    return DEFAULT_MONITOR_SIDEBAR_WIDTH
  }
  return Math.min(MAX_MONITOR_SIDEBAR_WIDTH, Math.max(MIN_MONITOR_SIDEBAR_WIDTH, value))
}

function readStoredMonitorSidebarWidth(fallback: number) {
  if (typeof window === 'undefined') {
    return clampMonitorSidebarWidth(fallback)
  }

  try {
    const stored = Number(window.localStorage.getItem(MONITOR_SIDEBAR_WIDTH_STORAGE_KEY))
    if (stored) {
      return clampMonitorSidebarWidth(stored)
    }
  } catch {
    // Ignore storage failures and keep the workspace default.
  }

  return clampMonitorSidebarWidth(fallback)
}

function readCompactMonitorLayout() {
  if (typeof window === 'undefined') {
    return false
  }
  return window.matchMedia('(max-width: 700px)').matches
}

export function appendedMonitorLogDelta(previous: string, next: string): string | null {
  if (!next) {
    return previous ? null : ''
  }
  if (next.startsWith(previous)) {
    return next.slice(previous.length)
  }
  if (previous.endsWith(next)) {
    return ''
  }
  if (!previous) {
    return null
  }

  const firstLineEnd = next.indexOf('\n')
  const markerLength = Math.min(firstLineEnd >= 0 ? firstLineEnd + 1 : next.length, 256)
  const marker = next.slice(0, markerLength)
  const searchStart = Math.max(0, previous.length - next.length)
  let candidate = previous.indexOf(marker, searchStart)
  let attempts = 0
  while (candidate >= 0 && attempts < 8) {
    const overlapLength = previous.length - candidate
    if (overlapLength <= next.length && next.startsWith(previous.slice(candidate))) {
      return next.slice(overlapLength)
    }
    candidate = previous.indexOf(marker, candidate + 1)
    attempts += 1
  }
  return null
}

function mergeMonitorTaskSummary(current: Task, refreshed: Task): Task {
  const summary = Object.fromEntries(
    Object.entries(refreshed).filter(([key]) => !COMPACT_MONITOR_DETAIL_FIELDS.has(key)),
  )
  return { ...current, ...summary }
}

export default function MonitorPage() {
  const {
    monitorTasks,
    monitorTotal,
    monitorHasMore,
    monitorLoading,
    monitorError,
    fetchMonitorTasks,
    upsertMonitorTask,
  } = useTaskStore()
  const workspace = useWorkspaceStore(state => state.workspace)
  const {
    selectedTaskName, logContent, logOffset, logIdentity, availableLogs, selectedLog,
    logTailTruncated, logTailLimitBytes, loading, exportIds,
    selectTask, selectLogFile, toggleExport, selectAllExport, clearExport,
  } = useMonitorStore()

  const [sidebarQuery, setSidebarQuery] = useState('')
  const [exportMode, setExportMode] = useState(false)
  const [detailTask, setDetailTask] = useState<Task | null>(null)
  const [selectedTaskSnapshot, setSelectedTaskSnapshot] = useState<Task | null>(null)
  const [streamStatus, setStreamStatus] = useState<LogStreamStatus>('idle')
  const [taskEventStatus, setTaskEventStatus] = useState<TaskEventStreamStatus>('idle')
  const [taskActionPending, setTaskActionPending] = useState<'run' | 'cancel' | null>(null)
  const [stopConfirmTask, setStopConfirmTask] = useState('')
  const monitorShellRef = useRef<HTMLDivElement>(null)
  const termContainerRef = useRef<HTMLDivElement>(null)
  const xtermRef = useRef<XTerminal | null>(null)
  const fitAddonRef = useRef<FitAddon | null>(null)
  const searchAddonRef = useRef<SearchAddon | null>(null)
  const observerRef = useRef<ResizeObserver | null>(null)
  const renderedLogRef = useRef<{ key: string; content: string; offset: number } | null>(null)
  const selectedTaskNameRef = useRef<string | null>(selectedTaskName)
  const selectedLogRef = useRef(selectedLog)
  const logOffsetRef = useRef(logOffset)
  const logIdentityRef = useRef(logIdentity)
  const workspaceKey = String(workspace?.run_root || '')
  const workspaceKeyRef = useRef(workspaceKey)
  const detailWorkspaceKeyRef = useRef('')
  const selectedTaskSnapshotWorkspaceKeyRef = useRef('')
  const taskActionRequestRef = useRef(0)
  const liveLogNameRef = useRef('')
  const queuedLiveLogTaskRef = useRef<{ taskName: string | null; runLogName: string }>({
    taskName: null,
    runLogName: '',
  })
  const manualHistoricalLogRef = useRef<{ taskName: string | null; logName: string }>({
    taskName: null,
    logName: '',
  })
  const terminalSearchInputRef = useRef<HTMLInputElement | null>(null)
  const terminalSearchShortcutScopeRef = useRef(false)
  const terminalSearchQueryRef = useRef('')
  const livePollingKeyRef = useRef('')
  const livePollInFlightRef = useRef(false)
  const wsStreamActiveRef = useRef(false)
  const pendingLiveLogChunkRef = useRef({ key: '', chunks: [] as PendingLiveLogChunk[] })
  const liveLogFlushTimerRef = useRef<number | null>(null)
  const taskRefreshTimerRef = useRef<number | null>(null)
  const taskRefreshInFlightRef = useRef(false)
  const taskRefreshQueuedRef = useRef(false)
  const [terminalSearchOpen, setTerminalSearchOpen] = useState(false)
  const [terminalSearchQuery, setTerminalSearchQuery] = useState('')
  const [terminalSearchStatus, setTerminalSearchStatus] = useState('')
  const selectedTaskFromList = useMemo(
    () => monitorTasks.find(task => task.name === selectedTaskName),
    [monitorTasks, selectedTaskName],
  )
  const selectedTask = selectedTaskFromList
    ?? (
      selectedTaskSnapshotWorkspaceKeyRef.current === workspaceKey
      && selectedTaskSnapshot?.name === selectedTaskName
        ? selectedTaskSnapshot
        : undefined
    )
  const runLogName = selectedTask ? `run${Math.max(selectedTask.run_index || 1, 1)}.log` : ''
  const liveLogName = selectedTask?.status === 'queued' ? QUEUE_LOG_NAME : runLogName
  const isFollowingQueuedTask = queuedLiveLogTaskRef.current.taskName === selectedTask?.name
  const isLive = Boolean(selectedTask && (
    (selectedTask.status === 'queued' && (!selectedLog || selectedLog === QUEUE_LOG_NAME))
    || (
      selectedTask.status === 'running'
      && (
        !selectedLog
        || selectedLog === runLogName
        || (isFollowingQueuedTask && selectedLog === QUEUE_LOG_NAME)
      )
    )
  ))
  const canUseLogStream = isLive
  const monitorChunkSize = resolveMonitorChunkSize(workspace?.settings)
  const monitorScrollback = resolveMonitorScrollback(workspace?.settings)
  const monitorLineHeight = resolveMonitorLineHeight(workspace?.settings)
  const sidebarWidthRaw = Number(workspace?.settings?.monitor_sidebar_width_pct ?? 14)
  const settingsSidebarWidthPct = Number.isFinite(sidebarWidthRaw)
    ? Math.min(35, Math.max(10, sidebarWidthRaw))
    : 14
  const [monitorSidebarWidthPct, setMonitorSidebarWidthPct] = useState(() => readStoredMonitorSidebarWidth(settingsSidebarWidthPct))
  const [compactMonitorLayout, setCompactMonitorLayout] = useState(readCompactMonitorLayout)
  const [resizingMonitorSidebar, setResizingMonitorSidebar] = useState(false)
  const notify = useToastStore(state => state.notify)
  const pendingMonitorSidebarWidthRef = useRef(monitorSidebarWidthPct)
  const monitorResizeFrameRef = useRef<number | null>(null)
  const terminalVisible = Boolean(selectedTaskName)
  const monitorShellClassName = clsx(
    'flex h-full w-full max-w-full min-w-0 overflow-hidden',
    compactMonitorLayout ? 'flex-col' : 'flex-row',
  )
  const refreshMonitorTasks = useCallback(
    () => fetchMonitorTasks({ query: sidebarQuery, refresh: true, workspaceKey }),
    [fetchMonitorTasks, sidebarQuery, workspaceKey],
  )
  const refreshDetachedSelectedTask = useCallback(async () => {
    if (!selectedTaskName || selectedTaskFromList) {
      return
    }
    const requestedWorkspaceKey = workspaceKey
    try {
      const task = await api.getTask(selectedTaskName, false)
      if (
        task.name === selectedTaskNameRef.current
        && workspaceKeyRef.current === requestedWorkspaceKey
      ) {
        selectedTaskSnapshotWorkspaceKeyRef.current = requestedWorkspaceKey
        setSelectedTaskSnapshot(task)
      }
    } catch (error) {
      if (
        /not found/i.test(errorMessage(error))
        && selectedTaskNameRef.current === selectedTaskName
        && workspaceKeyRef.current === requestedWorkspaceKey
      ) {
        setSelectedTaskSnapshot(null)
        useMonitorStore.setState({
          selectedTaskName: null,
          logContent: '',
          logOffset: 0,
          logIdentity: '',
          availableLogs: [],
          selectedLog: '',
          logTailTruncated: false,
          logTailLimitBytes: 0,
        })
      }
      // Keep the selected log visible for transient transport failures.
    }
  }, [selectedTaskFromList, selectedTaskName, workspaceKey])
  const refreshMonitorSnapshot = useCallback(async () => {
    await Promise.all([
      fetchMonitorTasks({
        query: sidebarQuery,
        refresh: true,
        background: true,
        workspaceKey,
      }),
      refreshDetachedSelectedTask(),
    ])
  }, [fetchMonitorTasks, refreshDetachedSelectedTask, sidebarQuery, workspaceKey])
  const refreshMonitorSnapshotRef = useRef(refreshMonitorSnapshot)
  refreshMonitorSnapshotRef.current = refreshMonitorSnapshot
  const runTaskSnapshotRefresh = useCallback(async () => {
    if (taskRefreshInFlightRef.current) {
      taskRefreshQueuedRef.current = true
      return
    }

    taskRefreshInFlightRef.current = true
    try {
      do {
        taskRefreshQueuedRef.current = false
        try {
          await refreshMonitorSnapshotRef.current()
        } catch {
          // The store exposes the degraded state; the fallback poll will retry.
        }
      } while (taskRefreshQueuedRef.current)
    } finally {
      taskRefreshInFlightRef.current = false
    }
  }, [])
  const scheduleTaskSnapshotRefresh = useCallback(() => {
    if (taskRefreshTimerRef.current !== null) {
      return
    }
    taskRefreshTimerRef.current = window.setTimeout(() => {
      taskRefreshTimerRef.current = null
      void runTaskSnapshotRefresh()
    }, TASK_EVENT_REFRESH_DEBOUNCE_MS)
  }, [runTaskSnapshotRefresh])

  useTaskEvents({
    onInvalidate: scheduleTaskSnapshotRefresh,
    onStatusChange: setTaskEventStatus,
    enabled: Boolean(workspaceKey),
    generationKey: workspaceKey,
  })
  usePolling(
    refreshMonitorSnapshot,
    taskEventStatus === 'live' ? TASK_EVENT_FALLBACK_POLL_MS : TASK_EVENT_DEGRADED_POLL_MS,
    Boolean(workspaceKey),
    false,
  )

  useEffect(() => {
    const handleVisibilityChange = () => {
      if (document.visibilityState === 'visible') {
        scheduleTaskSnapshotRefresh()
      }
    }
    document.addEventListener('visibilitychange', handleVisibilityChange)
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange)
  }, [scheduleTaskSnapshotRefresh])

  useEffect(() => () => {
    if (taskRefreshTimerRef.current !== null) {
      window.clearTimeout(taskRefreshTimerRef.current)
      taskRefreshTimerRef.current = null
    }
    taskRefreshQueuedRef.current = false
  }, [workspaceKey])
  useEffect(() => {
    setStopConfirmTask('')
    setTaskActionPending(null)
  }, [workspaceKey])
  terminalSearchQueryRef.current = terminalSearchQuery

  const runTerminalSearch = useCallback((direction: 'next' | 'previous' = 'next', incremental = false) => {
    const query = terminalSearchQueryRef.current
    const searchAddon = searchAddonRef.current
    if (!searchAddon) {
      return false
    }

    if (!query) {
      searchAddon.clearDecorations()
      setTerminalSearchStatus('')
      return false
    }

    const found = direction === 'previous'
      ? searchAddon.findPrevious(query, TERMINAL_SEARCH_OPTIONS)
      : searchAddon.findNext(query, { ...TERMINAL_SEARCH_OPTIONS, incremental })
    if (!found) {
      setTerminalSearchStatus('No match')
    }
    return found
  }, [])

  const isTerminalSearchShortcutTarget = useCallback((target: EventTarget | null) => {
    if (!(target instanceof Node)) {
      return false
    }
    return Boolean(
      termContainerRef.current?.contains(target)
      || terminalSearchInputRef.current?.contains(target),
    )
  }, [])

  const closeTerminalSearch = useCallback((restoreTerminalFocus = true) => {
    setTerminalSearchOpen(false)
    setTerminalSearchQuery('')
    setTerminalSearchStatus('')
    searchAddonRef.current?.clearDecorations()
    if (restoreTerminalFocus) {
      window.requestAnimationFrame(() => xtermRef.current?.focus())
    }
  }, [])

  useEffect(() => {
    taskActionRequestRef.current += 1
    detailWorkspaceKeyRef.current = ''
    selectedTaskSnapshotWorkspaceKeyRef.current = ''
    selectedTaskNameRef.current = null
    selectedLogRef.current = ''
    logOffsetRef.current = 0
    logIdentityRef.current = ''
    liveLogNameRef.current = ''
    livePollingKeyRef.current = ''
    livePollInFlightRef.current = false
    wsStreamActiveRef.current = false
    queuedLiveLogTaskRef.current = { taskName: null, runLogName: '' }
    manualHistoricalLogRef.current = { taskName: null, logName: '' }
    if (liveLogFlushTimerRef.current !== null) {
      window.clearTimeout(liveLogFlushTimerRef.current)
      liveLogFlushTimerRef.current = null
    }
    pendingLiveLogChunkRef.current = { key: '', chunks: [] }
    renderedLogRef.current = null
    xtermRef.current?.clear()
    xtermRef.current?.reset()
    setSidebarQuery('')
    setExportMode(false)
    setDetailTask(null)
    setSelectedTaskSnapshot(null)
    setTaskActionPending(null)
    setStreamStatus('idle')
    closeTerminalSearch(false)
  }, [closeTerminalSearch, workspaceKey])

  const startMonitorSidebarResize = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault()
    if (compactMonitorLayout) {
      return
    }
    setResizingMonitorSidebar(true)
  }, [compactMonitorLayout])

  const resizeMonitorSidebarByKeyboard = useCallback((event: ReactKeyboardEvent<HTMLButtonElement>) => {
    const deltas: Record<string, number> = { ArrowLeft: -2, ArrowRight: 2 }
    let next = monitorSidebarWidthPct
    if (event.key in deltas) {
      next = clampMonitorSidebarWidth(monitorSidebarWidthPct + deltas[event.key])
    } else if (event.key === 'Home') {
      next = MIN_MONITOR_SIDEBAR_WIDTH
    } else if (event.key === 'End') {
      next = MAX_MONITOR_SIDEBAR_WIDTH
    } else {
      return
    }
    event.preventDefault()
    pendingMonitorSidebarWidthRef.current = next
    setMonitorSidebarWidthPct(next)
    try {
      window.localStorage.setItem(MONITOR_SIDEBAR_WIDTH_STORAGE_KEY, String(next))
    } catch {
      // Keyboard resizing remains available when storage is blocked.
    }
  }, [monitorSidebarWidthPct])

  useEffect(() => {
    void refreshMonitorTasks().catch(err => {
      notify({
        tone: 'error',
        title: 'Could not load monitor tasks',
        detail: errorMessage(err),
      })
    })
  }, [notify, refreshMonitorTasks])

  useEffect(() => {
    if (selectedTaskFromList) {
      selectedTaskSnapshotWorkspaceKeyRef.current = workspaceKey
      setSelectedTaskSnapshot(selectedTaskFromList)
    }
  }, [selectedTaskFromList, workspaceKey])

  useEffect(() => {
    if (typeof window === 'undefined') {
      return
    }

    const query = window.matchMedia('(max-width: 700px)')
    const handleChange = () => setCompactMonitorLayout(query.matches)
    handleChange()
    query.addEventListener('change', handleChange)
    return () => query.removeEventListener('change', handleChange)
  }, [])

  useEffect(() => {
    try {
      if (!window.localStorage.getItem(MONITOR_SIDEBAR_WIDTH_STORAGE_KEY)) {
        setMonitorSidebarWidthPct(settingsSidebarWidthPct)
      }
    } catch {
      setMonitorSidebarWidthPct(settingsSidebarWidthPct)
    }
  }, [settingsSidebarWidthPct])

  useEffect(() => {
    if (!resizingMonitorSidebar) {
      return
    }

    const previousCursor = document.body.style.cursor
    const previousUserSelect = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    const persistMonitorSidebarWidth = (next: number) => {
      try {
        window.localStorage.setItem(MONITOR_SIDEBAR_WIDTH_STORAGE_KEY, String(next))
      } catch {
        // Runtime resizing should continue even when persistence is blocked.
      }
    }

    const fitMonitorTerminal = () => {
      try {
        fitAddonRef.current?.fit()
      } catch {
        // The terminal may be hidden while the sidebar is still resizable.
      }
    }

    const applyPendingMonitorSidebarWidth = () => {
      monitorResizeFrameRef.current = null
      setMonitorSidebarWidthPct(pendingMonitorSidebarWidthRef.current)
      fitMonitorTerminal()
    }

    const handlePointerMove = (event: PointerEvent) => {
      const rect = monitorShellRef.current?.getBoundingClientRect()
      const left = rect?.left ?? 0
      const width = rect?.width || window.innerWidth || 1
      pendingMonitorSidebarWidthRef.current = clampMonitorSidebarWidth(((event.clientX - left) / width) * 100)
      if (monitorResizeFrameRef.current == null) {
        monitorResizeFrameRef.current = window.requestAnimationFrame(applyPendingMonitorSidebarWidth)
      }
    }

    const stopResize = () => {
      if (monitorResizeFrameRef.current != null) {
        window.cancelAnimationFrame(monitorResizeFrameRef.current)
        monitorResizeFrameRef.current = null
      }
      setMonitorSidebarWidthPct(pendingMonitorSidebarWidthRef.current)
      fitMonitorTerminal()
      persistMonitorSidebarWidth(pendingMonitorSidebarWidthRef.current)
      setResizingMonitorSidebar(false)
    }

    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', stopResize, { once: true })
    window.addEventListener('pointercancel', stopResize, { once: true })

    return () => {
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', stopResize)
      window.removeEventListener('pointercancel', stopResize)
      if (monitorResizeFrameRef.current != null) {
        window.cancelAnimationFrame(monitorResizeFrameRef.current)
        monitorResizeFrameRef.current = null
      }
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousUserSelect
    }
  }, [resizingMonitorSidebar])

  useEffect(() => {
    const term = new XTerminal({
      convertEol: true,
      cursorBlink: false,
      disableStdin: true,
      screenReaderMode: true,
      scrollback: DEFAULT_MONITOR_SCROLLBACK,
      fontSize: 13,
      fontFamily: "'JetBrains Mono', 'Cascadia Code', Consolas, monospace",
      lineHeight: DEFAULT_MONITOR_LINE_HEIGHT,
      allowProposedApi: true,
      theme: {
        background: '#0A0A0B',
        foreground: '#E4E4E7',
        cursor: '#E4E4E7',
        selectionBackground: '#5E6AD240',
        selectionForeground: '#FFFFFF',
        black: '#27272A',
        red: '#F43F5E',
        green: '#10B981',
        yellow: '#F59E0B',
        blue: '#3B82F6',
        magenta: '#A855F7',
        cyan: '#06B6D4',
        white: '#E4E4E7',
        brightBlack: '#52525B',
        brightRed: '#FB7185',
        brightGreen: '#34D399',
        brightYellow: '#FBBF24',
        brightBlue: '#60A5FA',
        brightMagenta: '#C084FC',
        brightCyan: '#22D3EE',
        brightWhite: '#FAFAFA',
      },
    })

    term.attachCustomKeyEventHandler(event => {
      const isCopyShortcut = (event.ctrlKey || event.metaKey)
        && !event.altKey
        && event.key.toLowerCase() === 'c'
      if (!isCopyShortcut) {
        return true
      }

      const selection = term.getSelection()
      if (!selection) {
        return true
      }

      event.preventDefault()
      if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
        void navigator.clipboard.writeText(selection)
          .then(() => notify({
            tone: 'success',
            title: 'Log copied',
            detail: `${selection.length} character${selection.length === 1 ? '' : 's'} copied.`,
          }))
          .catch(err => notify({ tone: 'error', title: 'Could not copy log', detail: errorMessage(err) }))
      } else {
        notify({ tone: 'error', title: 'Clipboard unavailable', detail: 'Use the browser or terminal copy shortcut instead.' })
      }
      return false
    })

    const fitAddon = new FitAddon()
    const searchAddon = new SearchAddon({ highlightLimit: TERMINAL_SEARCH_HIGHLIGHT_LIMIT })
    const searchResultsDisposable = searchAddon.onDidChangeResults(({ resultIndex, resultCount }) => {
      if (!terminalSearchQueryRef.current) {
        setTerminalSearchStatus('')
        return
      }
      if (resultCount <= 0) {
        setTerminalSearchStatus('No match')
        return
      }
      setTerminalSearchStatus(resultIndex >= 0 ? `${resultIndex + 1}/${resultCount}` : `${TERMINAL_SEARCH_HIGHLIGHT_LIMIT}+`)
    })
    term.loadAddon(fitAddon)
    term.loadAddon(searchAddon)

    xtermRef.current = term
    fitAddonRef.current = fitAddon
    searchAddonRef.current = searchAddon

    return () => {
      observerRef.current?.disconnect()
      searchResultsDisposable.dispose()
      term.dispose()
      xtermRef.current = null
      fitAddonRef.current = null
      searchAddonRef.current = null
    }
  }, [notify])

  useEffect(() => {
    if (xtermRef.current) {
      xtermRef.current.options.scrollback = monitorScrollback
    }
  }, [monitorScrollback])

  useEffect(() => {
    if (!xtermRef.current) {
      return
    }
    xtermRef.current.options.lineHeight = monitorLineHeight
    const rafId = window.requestAnimationFrame(() => {
      try {
        fitAddonRef.current?.fit()
      } catch {
        // xterm can reject fitting while its container is between layouts.
      }
    })
    return () => window.cancelAnimationFrame(rafId)
  }, [monitorLineHeight])

  useEffect(() => {
    if (!terminalVisible) {
      observerRef.current?.disconnect()
      return
    }

    const term = xtermRef.current
    const fitAddon = fitAddonRef.current
    const container = termContainerRef.current
    if (!term || !fitAddon || !container) return

    if (!term.element) {
      term.open(container)
    } else if (term.element.parentElement !== container) {
      container.appendChild(term.element)
    }
    configureReadOnlyTerminalInput(term.textarea)

    const fitTerminal = () => {
      try {
        fitAddon.fit()
      } catch {
        // Ignore transient size errors while the panel is mounting.
      }
    }

    const rafId = requestAnimationFrame(fitTerminal)
    observerRef.current?.disconnect()

    const observer = new ResizeObserver(fitTerminal)
    observer.observe(container)
    observerRef.current = observer

    return () => {
      cancelAnimationFrame(rafId)
      observer.disconnect()
    }
  }, [terminalVisible])

  const renderKey = `${workspaceKey}::${selectedTaskName ?? ''}::${selectedLog || ''}`
  const shouldShowNoLogPlaceholder = !loading && availableLogs.length === 0 && !selectedLog

  useEffect(() => {
    const term = xtermRef.current
    if (!term) return

    if (!selectedTaskName) {
      term.clear()
      term.reset()
      renderedLogRef.current = null
      return
    }

    const previous = renderedLogRef.current
    const needsFreshRender = !previous || previous.key !== renderKey

    if (needsFreshRender) {
      term.clear()
      term.reset()
      if (logContent) {
        term.write(logContent)
      } else if (shouldShowNoLogPlaceholder) {
        term.write('\x1b[2m  < NO LOG >\x1b[0m\r\n')
      }
      renderedLogRef.current = { key: renderKey, content: logContent, offset: logOffset }
      return
    }

    if (logContent === previous.content) {
      previous.offset = logOffset
      return
    }

    const nextChunk = logOffset < previous.offset
      ? null
      : appendedMonitorLogDelta(previous.content, logContent)
    if (nextChunk !== null) {
      if (nextChunk) {
        term.write(nextChunk)
      }
    } else {
      term.clear()
      term.reset()
      if (logContent) {
        term.write(logContent)
      } else if (shouldShowNoLogPlaceholder) {
        term.write('\x1b[2m  < NO LOG >\x1b[0m\r\n')
      }
    }

    renderedLogRef.current = { key: renderKey, content: logContent, offset: logOffset }
  }, [renderKey, selectedTaskName, logContent, logOffset, shouldShowNoLogPlaceholder])

  useEffect(() => {
    if (!selectedTaskName && terminalSearchOpen) {
      closeTerminalSearch(false)
    }
  }, [closeTerminalSearch, selectedTaskName, terminalSearchOpen])

  useEffect(() => {
    if (!terminalSearchOpen) {
      searchAddonRef.current?.clearDecorations()
      setTerminalSearchStatus('')
      return
    }

    const rafId = window.requestAnimationFrame(() => {
      terminalSearchInputRef.current?.focus()
      terminalSearchInputRef.current?.select()
    })
    return () => window.cancelAnimationFrame(rafId)
  }, [terminalSearchOpen])

  useEffect(() => {
    if (!terminalSearchOpen) {
      return
    }
    runTerminalSearch('next', true)
  }, [renderKey, runTerminalSearch, terminalSearchOpen, terminalSearchQuery])

  useEffect(() => {
    const handleTerminalPointerScope = (event: PointerEvent) => {
      terminalSearchShortcutScopeRef.current = isTerminalSearchShortcutTarget(event.target)
    }

    window.addEventListener('pointerdown', handleTerminalPointerScope, true)
    return () => window.removeEventListener('pointerdown', handleTerminalPointerScope, true)
  }, [isTerminalSearchShortcutTarget])

  useEffect(() => {
    const handleTerminalSearchShortcut = (event: KeyboardEvent) => {
      const key = event.key.toLowerCase()
      const isFind = (event.ctrlKey || event.metaKey) && !event.altKey && key === 'f'
      const activeElement = document.activeElement
      const activeIsPageRoot = activeElement === document.body || activeElement === document.documentElement
      const shortcutTargetsTerminal = (
        isTerminalSearchShortcutTarget(event.target)
        || isTerminalSearchShortcutTarget(activeElement)
        || (terminalSearchShortcutScopeRef.current && activeIsPageRoot)
      )

      if (isFind && selectedTaskNameRef.current && shortcutTargetsTerminal) {
        event.preventDefault()
        terminalSearchShortcutScopeRef.current = true
        setTerminalSearchOpen(true)
        window.requestAnimationFrame(() => {
          terminalSearchInputRef.current?.focus()
          terminalSearchInputRef.current?.select()
        })
        return
      }

      if (!terminalSearchOpen || key !== 'f3' || !shortcutTargetsTerminal) {
        return
      }

      event.preventDefault()
      runTerminalSearch(event.shiftKey ? 'previous' : 'next')
    }

    window.addEventListener('keydown', handleTerminalSearchShortcut, true)
    return () => window.removeEventListener('keydown', handleTerminalSearchShortcut, true)
  }, [isTerminalSearchShortcutTarget, runTerminalSearch, terminalSearchOpen])

  useEffect(() => {
    setDetailTask(current => {
      if (!current) {
        return current
      }

      const refreshed = monitorTasks.find(task => task.name === current.name)
      if (!refreshed) {
        // Filtering, pagination, or a transient refresh may hide the summary.
        // Keep the full detail and any local draft until the user closes it.
        return current
      }
      if (refreshed === current) {
        return current
      }

      return mergeMonitorTaskSummary(current, refreshed)
    })
  }, [monitorTasks])

  selectedTaskNameRef.current = selectedTaskName
  selectedLogRef.current = selectedLog
  logOffsetRef.current = logOffset
  logIdentityRef.current = logIdentity
  liveLogNameRef.current = liveLogName
  workspaceKeyRef.current = workspaceKey

  useEffect(() => {
    const taskName = selectedTask?.name ?? null
    const taskStatus = selectedTask?.status ?? null
    const manualHistory = manualHistoricalLogRef.current
    if (manualHistory.taskName && manualHistory.taskName !== taskName) {
      manualHistoricalLogRef.current = { taskName: null, logName: '' }
    }

    if (!taskName) {
      queuedLiveLogTaskRef.current = { taskName: null, runLogName: '' }
      return
    }

    const viewingQueueOrLiveLog = !selectedLog || selectedLog === QUEUE_LOG_NAME
    if (taskStatus === 'queued' && viewingQueueOrLiveLog) {
      queuedLiveLogTaskRef.current = { taskName, runLogName }
      return
    }

    if (taskStatus !== 'running' || !runLogName) {
      return
    }

    const queuedLiveTask = queuedLiveLogTaskRef.current
    if (queuedLiveTask.taskName !== taskName) {
      return
    }

    const currentManualHistory = manualHistoricalLogRef.current
    if (
      currentManualHistory.taskName === taskName
      && currentManualHistory.logName
      && currentManualHistory.logName !== QUEUE_LOG_NAME
      && currentManualHistory.logName !== runLogName
    ) {
      return
    }

    if (selectedLog && selectedLog !== QUEUE_LOG_NAME && selectedLog !== runLogName) {
      return
    }

    if (selectedLog === runLogName) {
      queuedLiveLogTaskRef.current = { taskName: null, runLogName: '' }
      return
    }

    const fallbackTimer = window.setTimeout(() => {
      if (
        selectedTaskNameRef.current !== taskName
        || selectedLogRef.current !== QUEUE_LOG_NAME
        || queuedLiveLogTaskRef.current.taskName !== taskName
      ) {
        return
      }
      queuedLiveLogTaskRef.current = { taskName: null, runLogName: '' }
      if (liveLogFlushTimerRef.current !== null) {
        window.clearTimeout(liveLogFlushTimerRef.current)
        liveLogFlushTimerRef.current = null
      }
      pendingLiveLogChunkRef.current = { key: '', chunks: [] }
      void selectLogFile(runLogName)
        .catch(err => notify({ tone: 'error', title: 'Could not load run log', detail: errorMessage(err) }))
    }, 1500)
    return () => window.clearTimeout(fallbackTimer)
  }, [notify, runLogName, selectLogFile, selectedLog, selectedTask?.name, selectedTask?.status])

  const flushLiveLogChunkBuffer = useCallback(() => {
    if (liveLogFlushTimerRef.current !== null) {
      window.clearTimeout(liveLogFlushTimerRef.current)
      liveLogFlushTimerRef.current = null
    }

    const buffer = pendingLiveLogChunkRef.current
    if (buffer.chunks.length === 0) {
      return
    }

    pendingLiveLogChunkRef.current = { key: '', chunks: [] }

    const activeTaskName = selectedTaskNameRef.current
    const activeLog = selectedLogRef.current || liveLogNameRef.current
    const activeKey = activeTaskName
      ? `${workspaceKeyRef.current}::${activeTaskName}::${activeLog}`
      : ''
    if (buffer.key === activeKey) {
      useMonitorStore.setState(state => {
        let nextContent = state.logContent
        let nextOffset = state.logOffset
        let nextIdentity = state.logIdentity

        for (const chunk of buffer.chunks) {
          const chunkOffset = typeof chunk.offset === 'number' && Number.isFinite(chunk.offset)
            ? Math.max(0, Math.trunc(chunk.offset))
            : null
          if (chunkOffset !== null && chunkOffset <= nextOffset) {
            nextOffset = Math.max(nextOffset, chunkOffset)
            continue
          }
          nextContent = appendMonitorLogContent(nextContent, chunk.content)
          if (chunk.logIdentity) {
            nextIdentity = chunk.logIdentity
          }
          if (chunkOffset !== null) {
            nextOffset = Math.max(nextOffset, chunkOffset)
          }
        }

        return { logContent: nextContent, logOffset: nextOffset, logIdentity: nextIdentity }
      })
    }
  }, [])

  useEffect(() => {
    return () => {
      if (liveLogFlushTimerRef.current !== null) {
        window.clearTimeout(liveLogFlushTimerRef.current)
        liveLogFlushTimerRef.current = null
      }
      pendingLiveLogChunkRef.current = { key: '', chunks: [] }
    }
  }, [])

  useEffect(() => {
    const key = `${workspaceKey}::${selectedTaskName ?? ''}::${liveLogName}`
    if (livePollingKeyRef.current === key) {
      return
    }
    livePollingKeyRef.current = key
    livePollInFlightRef.current = false
    wsStreamActiveRef.current = false
  }, [liveLogName, selectedTaskName, workspaceKey])

  const handleChunk = useCallback((message: LogStreamMessage) => {
    const activeTaskName = selectedTaskNameRef.current
    const activeWorkspaceKey = workspaceKeyRef.current
    if (!activeTaskName || message.task_name !== activeTaskName) {
      return
    }

    let activeLog = selectedLogRef.current
    const liveLog = liveLogNameRef.current
    const messageLog = message.log_file_name || liveLog
    const currentLog = activeLog || liveLog
    const queueToRunTransition = Boolean(
      messageLog
      && messageLog !== currentLog
      && currentLog === QUEUE_LOG_NAME
      && RUN_LOG_PATTERN.test(messageLog)
      && queuedLiveLogTaskRef.current.taskName === activeTaskName
    )

    if (queueToRunTransition) {
      if (liveLogFlushTimerRef.current !== null) {
        window.clearTimeout(liveLogFlushTimerRef.current)
        liveLogFlushTimerRef.current = null
      }
      pendingLiveLogChunkRef.current = { key: '', chunks: [] }
      queuedLiveLogTaskRef.current = { taskName: null, runLogName: '' }
      selectedLogRef.current = messageLog
      liveLogNameRef.current = messageLog
      logOffsetRef.current = 0
      logIdentityRef.current = ''
      activeLog = messageLog
      useTaskStore.setState(state => ({
        monitorTasks: state.monitorTasks.map(task => (
          task.name === activeTaskName && task.status === 'queued'
            ? { ...task, status: 'running' }
            : task
        )),
      }))
      selectedTaskSnapshotWorkspaceKeyRef.current = activeWorkspaceKey
      setSelectedTaskSnapshot(current => (
        current?.name === activeTaskName && current.status === 'queued'
          ? { ...current, status: 'running' }
          : current
      ))
      useMonitorStore.setState(state => ({
        selectedLog: messageLog,
        logContent: '',
        logOffset: 0,
        logIdentity: '',
        logTailTruncated: false,
        logTailLimitBytes: 0,
        availableLogs: state.availableLogs.includes(messageLog)
          ? state.availableLogs
          : [messageLog, ...state.availableLogs],
      }))
    }

    if (messageLog && activeLog && messageLog !== activeLog) {
      return
    }
    if (!activeLog && messageLog && liveLog && messageLog !== liveLog) {
      return
    }

    wsStreamActiveRef.current = true
    if (message.type === 'reset') {
      if (liveLogFlushTimerRef.current !== null) {
        window.clearTimeout(liveLogFlushTimerRef.current)
        liveLogFlushTimerRef.current = null
      }
      pendingLiveLogChunkRef.current = { key: '', chunks: [] }
      const nextOffset = typeof message.offset === 'number' && Number.isFinite(message.offset)
        ? Math.max(0, Math.trunc(message.offset))
        : 0
      const nextIdentity = String(message.log_identity || '')
      logOffsetRef.current = nextOffset
      logIdentityRef.current = nextIdentity
      useMonitorStore.setState(state => (
        state.workspaceKey === activeWorkspaceKey && state.selectedTaskName === activeTaskName
          ? {
              logContent: message.content || '',
              logOffset: nextOffset,
              logIdentity: nextIdentity,
              logTailTruncated: false,
              logTailLimitBytes: 0,
            }
          : state
      ))
      return
    }

    const key = `${activeWorkspaceKey}::${activeTaskName}::${activeLog || messageLog || liveLog}`
    const buffer = pendingLiveLogChunkRef.current
    const chunk: PendingLiveLogChunk = typeof message.offset === 'number' && Number.isFinite(message.offset)
      ? { content: message.content, offset: message.offset, logIdentity: message.log_identity }
      : { content: message.content, logIdentity: message.log_identity }
    if (buffer.key === key) {
      buffer.chunks.push(chunk)
    } else {
      pendingLiveLogChunkRef.current = { key, chunks: [chunk] }
    }
    if (liveLogFlushTimerRef.current === null) {
      liveLogFlushTimerRef.current = window.setTimeout(flushLiveLogChunkBuffer, LOG_STREAM_FLUSH_MS)
    }
  }, [flushLiveLogChunkBuffer])

  const handleLogStreamDisconnect = useCallback(() => {
    flushLiveLogChunkBuffer()
    wsStreamActiveRef.current = false
  }, [flushLiveLogChunkBuffer])

  const handleLogStreamStatus = useCallback((status: LogStreamStatus) => {
    wsStreamActiveRef.current = status === 'live'
    setStreamStatus(status)
  }, [])

  useLogStream({
    taskName: selectedTaskName,
    onChunk: handleChunk,
    onDisconnect: handleLogStreamDisconnect,
    onStatusChange: handleLogStreamStatus,
    enabled: !loading && isLive && canUseLogStream,
    logFileName: selectedLog || liveLogName || undefined,
    offset: logOffsetRef.current,
    logIdentity: logIdentityRef.current,
    generationKey: workspaceKey,
  })

  const pollLiveLog = useCallback(async () => {
    const activeTaskName = selectedTaskNameRef.current
    const liveLog = liveLogNameRef.current
    if (!activeTaskName || !liveLog || (canUseLogStream && wsStreamActiveRef.current) || livePollInFlightRef.current) {
      return
    }

    livePollInFlightRef.current = true
    const requestedWorkspaceKey = workspaceKeyRef.current
    const requestedLog = selectedLogRef.current || liveLog
    const monitorState = useMonitorStore.getState()
    const currentOffset = monitorState.logOffset
    const currentIdentity = monitorState.logIdentity
    try {
      const logs = await api.getTaskLogs(activeTaskName, {
        logFileName: requestedLog,
        offset: currentOffset,
        logIdentity: currentIdentity,
        chunkSize: monitorChunkSize,
      })
      if (
        selectedTaskNameRef.current !== activeTaskName
        || workspaceKeyRef.current !== requestedWorkspaceKey
      ) {
        return
      }
      const stillViewingLog = selectedLogRef.current || liveLogNameRef.current
      if (logs.selected_log && stillViewingLog && logs.selected_log !== stillViewingLog) {
        return
      }

      const nextIdentity = String(logs.log_identity || '')
      const shouldReplaceContent = Boolean(logs.reset)
        || logs.offset < currentOffset
        || Boolean(currentIdentity && nextIdentity && currentIdentity !== nextIdentity)
      logOffsetRef.current = logs.offset
      logIdentityRef.current = nextIdentity
      useMonitorStore.setState(state => (
        state.workspaceKey === requestedWorkspaceKey && state.selectedTaskName === activeTaskName
          ? {
              logContent: shouldReplaceContent
                ? logs.content
                : logs.content
                  ? appendMonitorLogContent(state.logContent, logs.content)
                  : state.logContent,
              logOffset: logs.offset,
              logIdentity: nextIdentity,
              availableLogs: logs.available_logs,
              selectedLog: state.selectedLog || logs.selected_log,
              logTailTruncated: shouldReplaceContent ? false : state.logTailTruncated,
              logTailLimitBytes: shouldReplaceContent ? 0 : state.logTailLimitBytes,
            }
          : state
      ))
    } catch {
      // Keep the monitor quiet; task polling still refreshes status.
    } finally {
      if (workspaceKeyRef.current === requestedWorkspaceKey) {
        livePollInFlightRef.current = false
      }
    }
  }, [canUseLogStream, monitorChunkSize])

  usePolling(pollLiveLog, 1500, Boolean(isLive), false)

  const filteredTasks = monitorTasks
  const pinnedTasks = useMemo(
    () => filteredTasks.filter(task => task.pinned),
    [filteredTasks],
  )
  const otherTasks = useMemo(
    () => filteredTasks.filter(task => !task.pinned),
    [filteredTasks],
  )
  const allExportSelected = useMemo(
    () => filteredTasks.length > 0 && filteredTasks.every(task => exportIds.has(task.name)),
    [exportIds, filteredTasks],
  )
  const selectedTaskOutsideList = Boolean(
    selectedTask && !filteredTasks.some(task => task.name === selectedTask.name),
  )

  const handleSidebarClick = (task: Task) => {
    if (exportMode) {
      toggleExport(task.name)
      return
    }
    void selectTask(task.name)
      .catch(err => notify({ tone: 'error', title: 'Could not load task logs', detail: errorMessage(err) }))
  }

  const handleTaskAction = useCallback(async (action: 'run' | 'cancel') => {
    if (!selectedTaskName || !selectedTask || taskActionPending) return

    const currentTaskName = selectedTaskName
    const requestedWorkspaceKey = workspaceKeyRef.current
    const actionRequestId = ++taskActionRequestRef.current
    setTaskActionPending(action)

    try {
      let task: Task | null = null
      if (action === 'run') {
        task = (await api.runTask(currentTaskName)).task
      } else {
        task = (await api.cancelTask(currentTaskName)).task
      }

      const stillSelected = workspaceKeyRef.current === requestedWorkspaceKey
        && selectedTaskNameRef.current === currentTaskName
      if (task && stillSelected) {
        upsertMonitorTask(task)
        selectedTaskSnapshotWorkspaceKeyRef.current = requestedWorkspaceKey
        setSelectedTaskSnapshot(task)
      }

      if (stillSelected) {
        await selectTask(currentTaskName).catch(err => {
          notify({ tone: 'error', title: 'Task changed, but its log could not refresh', detail: errorMessage(err) })
        })
      }
      await fetchMonitorTasks({ workspaceKey: requestedWorkspaceKey }).catch(() => {
        // The action response already updated the selected task; polling will retry the sidebar.
      })
      notify({
        tone: 'success',
        title: action === 'run' ? 'Task started' : 'Cancel requested',
        detail: currentTaskName,
      })
    } catch (err) {
      notify({
        tone: 'error',
        title: action === 'run' ? 'Could not start task' : 'Could not cancel task',
        detail: errorMessage(err),
      })
    } finally {
      if (taskActionRequestRef.current === actionRequestId) {
        setTaskActionPending(null)
      }
    }
  }, [selectedTaskName, selectedTask, taskActionPending, fetchMonitorTasks, selectTask, upsertMonitorTask, notify])

  const openDetailTask = useCallback((task: Task) => {
    const requestedWorkspaceKey = workspaceKeyRef.current
    detailWorkspaceKeyRef.current = requestedWorkspaceKey
    setDetailTask(task)
    void api.getTask(task.name).then(fullTask => {
      if (workspaceKeyRef.current !== requestedWorkspaceKey) {
        return
      }
      setDetailTask(current => (
        detailWorkspaceKeyRef.current === requestedWorkspaceKey && current?.name === task.name
          ? fullTask
          : current
      ))
    }).catch(err => {
      if (workspaceKeyRef.current === requestedWorkspaceKey) {
        notify({ tone: 'error', title: 'Could not load task details', detail: errorMessage(err) })
      }
    })
  }, [notify])

  const handleExport = useCallback(async () => {
    const names = [...exportIds]
    if (!names.length) return
    const requestedWorkspaceKey = workspaceKeyRef.current
    try {
      const blob = await api.exportTasksCsv(names)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `pyruns_export_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.csv`
      anchor.click()
      URL.revokeObjectURL(url)
      if (workspaceKeyRef.current === requestedWorkspaceKey) {
        setExportMode(false)
        clearExport()
      }
      notify({
        tone: 'success',
        title: 'CSV exported',
        detail: `${names.length} task${names.length === 1 ? '' : 's'} exported.`,
      })
    } catch (err) {
      notify({ tone: 'error', title: 'Could not export CSV', detail: errorMessage(err) })
    }
  }, [clearExport, exportIds, notify])

  const handleSelectLogFile = useCallback((logName: string) => {
    const taskName = selectedTaskNameRef.current
    const liveLog = liveLogNameRef.current
    const taskStatus = useTaskStore.getState().monitorTasks.find(task => task.name === taskName)?.status
      ?? selectedTaskSnapshot?.status
    const liveChoice = taskStatus === 'queued' ? QUEUE_LOG_NAME : liveLog
    const selectingHistory = Boolean(taskName && logName && logName !== liveChoice)
    manualHistoricalLogRef.current = selectingHistory
      ? { taskName, logName }
      : { taskName: null, logName: '' }
    if (selectingHistory) {
      queuedLiveLogTaskRef.current = { taskName: null, runLogName: '' }
    }
    void selectLogFile(logName)
      .catch(err => notify({ tone: 'error', title: 'Could not load log file', detail: errorMessage(err) }))
  }, [notify, selectLogFile, selectedTaskSnapshot?.status])

  const handleLoadMoreTasks = useCallback(() => {
    void fetchMonitorTasks({ query: sidebarQuery, loadMore: true, refresh: false, workspaceKey })
      .catch(err => notify({
        tone: 'error',
        title: 'Could not load more tasks',
        detail: errorMessage(err),
      }))
  }, [fetchMonitorTasks, notify, sidebarQuery, workspaceKey])

  return (
    <div ref={monitorShellRef} className={monitorShellClassName}>
      <aside
        aria-label="Task monitor sidebar"
        className={clsx(
          'flex flex-none flex-col overflow-hidden bg-surface-raised',
          compactMonitorLayout ? 'w-full max-w-full border-b border-border-subtle' : 'border-r border-border-subtle',
        )}
        style={compactMonitorLayout ? { height: COMPACT_MONITOR_SIDEBAR_HEIGHT } : { width: `${monitorSidebarWidthPct}%` }}
      >
        <div className="flex-none border-b border-border-subtle px-2.5 py-2">
          <div className="mb-2 flex items-center justify-between">
            <div>
              <div className="text-2xs uppercase tracking-[0.18em] text-txt-tertiary">Monitor</div>
              <div className="text-sm font-medium text-txt-primary">
                {monitorTotal.toLocaleString()} task{monitorTotal === 1 ? '' : 's'}
              </div>
            </div>
            <span
              role="status"
              aria-live="polite"
              aria-label={taskEventStatus === 'live'
                ? 'Task list updates live'
                : taskEventStatus === 'reconnecting'
                  ? 'Live task updates disconnected; fallback refresh is active'
                  : 'Connecting live task updates'}
              title={taskEventStatus === 'live'
                ? 'Task changes appear automatically'
                : 'Using fallback refresh while the live connection recovers'}
              className={clsx(
                'inline-flex items-center gap-1.5 text-2xs',
                taskEventStatus === 'live' ? 'text-txt-tertiary' : 'text-amber-700 dark:text-amber-300',
              )}
            >
              {taskEventStatus === 'live'
                ? <span aria-hidden="true" className="h-1.5 w-1.5 rounded-full bg-emerald-500" />
                : <LoaderCircle aria-hidden="true" className="h-3 w-3 motion-safe:animate-spin" />}
              {taskEventStatus === 'live'
                ? 'Live'
                : taskEventStatus === 'reconnecting'
                  ? 'Retrying'
                  : 'Connecting'}
            </span>
          </div>
          <SearchInput
            value={sidebarQuery}
            onChange={setSidebarQuery}
            placeholder="Search..."
            ariaLabel="Search monitor tasks"
            debounceMs={250}
          />
          {monitorError && (
            <div className="mt-2 flex items-start gap-1.5 rounded-md border border-rose-500/20 bg-rose-500/8 px-2 py-1.5 text-2xs text-rose-700 dark:text-rose-300" role="alert">
              <WifiOff className="mt-0.5 h-3 w-3 flex-none" />
              <span className="min-w-0 flex-1">Task updates are delayed. Retrying automatically.</span>
            </div>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
          {selectedTaskOutsideList && selectedTask && (
            <CompactSection
              title="Current Task"
              icon={<Rows3 className="h-3.5 w-3.5 text-accent" />}
              className="mb-3 rounded-md border border-accent/20 bg-accent/5 p-2"
              bodyClassName="space-y-1 pt-0"
            >
              <SidebarItem
                task={selectedTask}
                active={!exportMode}
                exportMode={exportMode}
                exportSelected={exportIds.has(selectedTask.name)}
                onClick={() => handleSidebarClick(selectedTask)}
              />
            </CompactSection>
          )}
          {pinnedTasks.length > 0 && (
            <CompactSection
              title="Pinned Tasks"
              count={pinnedTasks.length}
              icon={<Pin className="h-3.5 w-3.5 text-accent" />}
              accent
              className="mb-3 rounded-md border border-accent/20 bg-accent/5 p-2"
              bodyClassName="space-y-1 pt-0"
            >
              {pinnedTasks.map(task => (
                <SidebarItem
                  key={task.name}
                  task={task}
                  active={!exportMode && task.name === selectedTaskName}
                  exportMode={exportMode}
                  exportSelected={exportIds.has(task.name)}
                  onClick={() => handleSidebarClick(task)}
                />
              ))}
            </CompactSection>
          )}

          <CompactSection
            title="Tasks"
            subtitle={`${otherTasks.length} task${otherTasks.length > 1 ? 's' : ''}`}
            icon={<Rows3 className="h-3.5 w-3.5 text-txt-tertiary" />}
            bodyClassName="space-y-1 p-1"
          >
            {otherTasks.length === 0 && pinnedTasks.length === 0 ? (
              <div className="px-2 py-5 text-center text-2xs text-txt-tertiary">
                {monitorLoading ? 'Loading tasks…' : sidebarQuery ? 'No matching tasks' : 'No tasks'}
              </div>
            ) : (
              otherTasks.map(task => (
                <SidebarItem
                  key={task.name}
                  task={task}
                  active={!exportMode && task.name === selectedTaskName}
                  exportMode={exportMode}
                  exportSelected={exportIds.has(task.name)}
                  onClick={() => handleSidebarClick(task)}
                />
              ))
            )}
          </CompactSection>

          {(monitorHasMore || monitorTasks.length > 0) && (
            <div className="mt-2 space-y-1 px-1 text-center">
              <div className="text-2xs text-txt-tertiary">
                Loaded {monitorTasks.length.toLocaleString()} of {monitorTotal.toLocaleString()}
              </div>
              {monitorHasMore && (
                <ActionButton
                  variant="ghost"
                  className="w-full border border-border-subtle"
                  icon={monitorLoading
                    ? <LoaderCircle className="h-3.5 w-3.5 motion-safe:animate-spin" />
                    : <RefreshCw className="h-3.5 w-3.5" />}
                  disabled={monitorLoading}
                  onClick={handleLoadMoreTasks}
                >
                  Load 200 more
                </ActionButton>
              )}
            </div>
          )}
        </div>

        <div className="flex-none border-t border-border-subtle px-2.5 py-2">
          {!exportMode ? (
            <ActionButton
              icon={<FileDown className="h-3.5 w-3.5" />}
              variant="primary"
              className="w-full"
              onClick={() => setExportMode(true)}
              disabled={filteredTasks.length === 0}
            >
              Export
            </ActionButton>
          ) : (
            <div className="space-y-2">
              <div className="flex items-center justify-between px-0.5 text-2xs">
                <button
                  type="button"
                  onClick={() => (allExportSelected ? clearExport() : selectAllExport(filteredTasks.map(task => task.name)))}
                  className="touch-target text-accent transition-colors hover:text-accent-hover"
                >
                  {allExportSelected ? 'Deselect loaded' : 'Select loaded'}
                </button>
                <span className="text-txt-tertiary">{exportIds.size} selected</span>
              </div>

              <div className="flex items-center gap-2">
                <ActionButton
                  icon={<Download className="h-3.5 w-3.5" />}
                  variant="primary"
                  className="flex-1"
                  onClick={handleExport}
                  disabled={exportIds.size === 0}
                >
                  Export
                </ActionButton>
                <ActionButton
                  variant="ghost"
                  onClick={() => {
                    setExportMode(false)
                    clearExport()
                  }}
                >
                  Cancel
                </ActionButton>
              </div>
            </div>
          )}
        </div>
      </aside>
      {!compactMonitorLayout && (
        <button
          type="button"
          role="separator"
          aria-label="Resize monitor sidebar"
          aria-orientation="vertical"
          aria-valuemin={MIN_MONITOR_SIDEBAR_WIDTH}
          aria-valuemax={MAX_MONITOR_SIDEBAR_WIDTH}
          aria-valuenow={Math.round(monitorSidebarWidthPct)}
          onPointerDown={startMonitorSidebarResize}
          onKeyDown={resizeMonitorSidebarByKeyboard}
          className={clsx(
            'h-full w-1 flex-none cursor-col-resize touch-none transition-colors focus:outline-none focus:ring-2 focus:ring-accent/35',
            resizingMonitorSidebar ? 'bg-accent/45' : 'bg-transparent hover:bg-accent/25',
          )}
        />
      )}

      <div className="flex min-h-0 min-w-0 max-w-full flex-1 flex-col" style={{ background: '#0A0A0B' }}>
        <div className="flex flex-wrap items-center gap-2.5 border-b border-border-subtle bg-surface-raised px-3 py-2">
          {selectedTask ? (
            <>
              <StatusBadge status={selectedTask.status as TaskStatus} />
              <div className="min-w-0 flex-1 basis-[12rem]">
                <div className="truncate text-sm font-medium text-txt-primary" title={selectedTask.name}>
                  {selectedTask.name}
                </div>
                <div className="truncate text-2xs text-txt-tertiary" title={selectedLog || liveLogName || 'latest log'}>
                  {selectedLog || liveLogName || 'latest log'}
                </div>
              </div>

              {isLive && (
                <span
                  role="status"
                  aria-live="polite"
                  title={streamStatus === 'reconnecting' ? 'Live stream disconnected; incremental polling remains active while reconnecting.' : undefined}
                  className={clsx(
                    'inline-flex items-center gap-1 rounded-md px-2 py-1 text-2xs font-medium',
                    streamStatus === 'live'
                      ? 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
                      : 'bg-amber-500/10 text-amber-700 dark:text-amber-300',
                  )}
                >
                  {streamStatus === 'live'
                    ? <Wifi className="h-3 w-3" />
                    : <RefreshCw className="h-3 w-3 motion-safe:animate-spin" />}
                  {streamStatus === 'live'
                    ? 'Live'
                    : streamStatus === 'reconnecting'
                      ? 'Reconnecting'
                      : 'Connecting'}
                </span>
              )}

              {(selectedTask.status === 'pending'
                || selectedTask.status === 'failed'
                || selectedTask.status === 'completed'
                || selectedTask.status === 'cancelled') && (
                <ActionButton
                  icon={taskActionPending === 'run'
                    ? <LoaderCircle className="h-3.5 w-3.5 motion-safe:animate-spin" />
                    : <Play className="h-3.5 w-3.5" />}
                  variant="success"
                  onClick={() => void handleTaskAction('run')}
                  disabled={taskActionPending !== null}
                >
                  {taskActionPending === 'run' ? 'Starting' : 'Run'}
                </ActionButton>
              )}

              {(selectedTask.status === 'running' || selectedTask.status === 'queued') && (
                <ActionButton
                  icon={taskActionPending === 'cancel'
                    ? <LoaderCircle className="h-3.5 w-3.5 motion-safe:animate-spin" />
                    : <Square className="h-3.5 w-3.5" />}
                  variant="danger"
                  onClick={() => setStopConfirmTask(selectedTask.name)}
                  disabled={taskActionPending !== null}
                >
                  {taskActionPending === 'cancel' ? 'Stopping' : 'Stop'}
                </ActionButton>
              )}

              <ActionButton variant="accentTint" onClick={() => openDetailTask(selectedTask)}>
                View Details
              </ActionButton>

              {availableLogs.length > 1 && (
                <div className="relative">
                  <select
                    value={selectedLog}
                    onChange={event => handleSelectLogFile(event.target.value)}
                    title="Select log file"
                    aria-label="Select task log file"
                    disabled={loading}
                    className="touch-input appearance-none rounded-md border border-border-subtle bg-surface-overlay px-2 py-1.5 pr-6 text-2xs text-txt-primary outline-none transition-colors focus:border-border"
                  >
                    {availableLogs.map(log => (
                      <option key={log} value={log}>{log}</option>
                    ))}
                  </select>
                  <ChevronDown className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
                </div>
              )}
            </>
          ) : (
            <span className="text-xs text-txt-tertiary">Select a task to view logs</span>
          )}
        </div>

        {selectedTask?.status === 'queued' && (
          <GPUWaitPanel wait={selectedTask.gpu_wait ?? null} />
        )}

        {logTailTruncated && (
          <div
            role="status"
            className="flex flex-wrap items-center gap-x-2 gap-y-1 border-b border-amber-500/20 bg-amber-500/10 px-3 py-1.5 text-2xs text-amber-800 dark:text-amber-200"
          >
            <AlertTriangle className="h-3.5 w-3.5 flex-none" />
            <span>
              Showing the latest {formatBytes(logTailLimitBytes)} of this log to keep the monitor responsive.
            </span>
            <span className="text-amber-700/80 dark:text-amber-300/80">New output continues live.</span>
          </div>
        )}

        <div className="relative flex-1 overflow-hidden" aria-busy={loading}>
          {selectedTaskName ? (
            <>
              <div
                ref={termContainerRef}
                className="monitor-terminal-shell h-full w-full"
                role="region"
                aria-label={`Read-only logs for ${selectedTaskName}`}
              />
              {loading && (
                <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center bg-[#0A0A0B]/70 text-xs text-[#cbd5e1]" role="status">
                  <span className="inline-flex items-center gap-2 rounded-md border border-[#303136] bg-[#18181b] px-3 py-2">
                    <LoaderCircle className="h-4 w-4 motion-safe:animate-spin" />
                    Loading log…
                  </span>
                </div>
              )}
              {terminalSearchOpen && (
                <form
                  role="search"
                  className="absolute right-3 top-3 z-20 flex w-[calc(100%-1.5rem)] max-w-[26rem] items-center gap-1 rounded-md border border-[#454545] bg-[#252526] px-1.5 py-1 text-[#cccccc] shadow-[0_2px_10px_rgba(0,0,0,0.45)]"
                  onSubmit={event => {
                    event.preventDefault()
                    runTerminalSearch('next')
                  }}
                >
                  <Search className="h-3.5 w-3.5 flex-none text-[#8b949e]" />
                  <input
                    ref={terminalSearchInputRef}
                    value={terminalSearchQuery}
                    onChange={event => setTerminalSearchQuery(event.target.value)}
                    onKeyDown={event => {
                      if (event.key === 'Enter') {
                        event.preventDefault()
                        runTerminalSearch(event.shiftKey ? 'previous' : 'next')
                      }
                      if (event.key === 'Escape') {
                        event.preventDefault()
                        closeTerminalSearch()
                      }
                    }}
                    placeholder="Search logs"
                    aria-label="Search terminal logs"
                    className="touch-input min-w-0 flex-1 bg-transparent px-1 py-0.5 text-xs text-[#cccccc] caret-[#cccccc] outline-none selection:bg-[#264f78] selection:text-white placeholder:text-[#6f7787]"
                  />
                  <span
                    role="status"
                    aria-live="polite"
                    aria-atomic="true"
                    className={clsx(
                      'min-w-[3.25rem] text-right text-2xs',
                      terminalSearchStatus === 'No match' ? 'text-[#f48771]' : 'text-[#a0a6b1]',
                    )}
                  >
                    {terminalSearchStatus}
                  </span>
                  <button
                    type="button"
                    onClick={() => runTerminalSearch('previous')}
                    className="touch-target rounded-md p-1 text-[#a0a6b1] transition-colors hover:bg-[#2a2d2e] hover:text-[#f0f0f0] focus:outline-none focus:ring-2 focus:ring-[#007acc]/40"
                    title="Previous match"
                    aria-label="Previous match"
                  >
                    <ChevronUp className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => runTerminalSearch('next')}
                    className="touch-target rounded-md p-1 text-[#a0a6b1] transition-colors hover:bg-[#2a2d2e] hover:text-[#f0f0f0] focus:outline-none focus:ring-2 focus:ring-[#007acc]/40"
                    title="Next match"
                    aria-label="Next match"
                  >
                    <ChevronDown className="h-3.5 w-3.5" />
                  </button>
                  <button
                    type="button"
                    onClick={() => closeTerminalSearch()}
                    className="touch-target rounded-md p-1 text-[#a0a6b1] transition-colors hover:bg-[#2a2d2e] hover:text-[#f0f0f0] focus:outline-none focus:ring-2 focus:ring-[#007acc]/40"
                    title="Close search"
                    aria-label="Close terminal search"
                  >
                    <X className="h-3.5 w-3.5" />
                  </button>
                </form>
              )}
            </>
          ) : (
            <div className="flex h-full min-w-0 items-center justify-center px-4">
              <EmptyState title="No task selected" description="Select a task from the sidebar to inspect logs" />
            </div>
          )}
        </div>
      </div>

      {detailTask && detailWorkspaceKeyRef.current === workspaceKey && (
        <TaskDetailPanel
          task={detailTask}
          onClose={() => {
            detailWorkspaceKeyRef.current = ''
            setDetailTask(null)
          }}
          onTaskUpdated={updatedTask => {
            upsertMonitorTask(updatedTask)
            setDetailTask(current => current?.name === updatedTask.name ? updatedTask : current)
          }}
          onRefresh={() => {
            void fetchMonitorTasks()
          }}
        />
      )}
      <ConfirmDialog
        open={Boolean(stopConfirmTask)}
        title="Stop active task?"
        description={stopConfirmTask ? `Request cancellation for '${stopConfirmTask}'? Its process tree will be stopped.` : ''}
        confirmLabel="Stop Task"
        confirmVariant="danger"
        onConfirm={async () => {
          if (!stopConfirmTask || selectedTaskName !== stopConfirmTask) {
            setStopConfirmTask('')
            return
          }
          try {
            await handleTaskAction('cancel')
          } finally {
            setStopConfirmTask('')
          }
        }}
        onCancel={() => setStopConfirmTask('')}
      />
    </div>
  )
}

function GPUWaitPanel({ wait }: { wait: GPUWaitStatus | null }) {
  const requested = Math.max(1, Number(wait?.requested_gpu_count || 1))
  const eligible = Number.isFinite(Number(wait?.eligible_gpu_count))
    ? Math.max(0, Number(wait?.eligible_gpu_count))
    : null
  const waitedSeconds = Math.max(0, Number(wait?.waited_seconds || 0))
  const maxWaitSeconds = Math.max(0, Number(wait?.max_wait_seconds || 0))
  const serverRemainingSeconds = Number(wait?.remaining_seconds)
  const remainingSeconds = Number.isFinite(serverRemainingSeconds)
    ? Math.max(0, serverRemainingSeconds)
    : maxWaitSeconds > 0
      ? Math.max(0, maxWaitSeconds - waitedSeconds)
      : null
  const devices = Array.isArray(wait?.devices) ? wait.devices : []

  return (
    <section
      aria-labelledby="gpu-wait-heading"
      className="border-b border-border-subtle bg-surface-raised px-3 py-2.5"
    >
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex min-w-0 flex-1 basis-[20rem] items-start gap-2.5">
          <span className="mt-0.5 inline-flex h-8 w-8 flex-none items-center justify-center rounded-md bg-amber-500/10 text-amber-700 dark:text-amber-300">
            <Cpu className="h-4 w-4" />
          </span>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <h2 id="gpu-wait-heading" className="text-xs font-semibold text-txt-primary">Waiting for GPU capacity</h2>
              <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-2xs font-medium text-amber-700 dark:text-amber-300">
                {eligible === null ? 'Checking eligibility' : `${eligible}/${requested} eligible`}
              </span>
            </div>
            <p className="mt-0.5 text-2xs leading-5 text-txt-secondary">
              {wait?.reason || 'The scheduler is checking memory, utilization, reservations, and the stability window.'}
            </p>
          </div>
        </div>

        <dl className="grid min-w-[15rem] flex-none grid-cols-2 gap-x-4 gap-y-1 text-2xs sm:grid-cols-3">
          <div>
            <dt className="text-txt-tertiary">Waited</dt>
            <dd className="font-medium tabular-nums text-txt-primary">{formatDuration(waitedSeconds)}</dd>
          </div>
          <div>
            <dt className="text-txt-tertiary">Time left</dt>
            <dd className="font-medium tabular-nums text-txt-primary">
              {remainingSeconds === null ? 'No limit' : formatDuration(remainingSeconds)}
            </dd>
          </div>
          <div className="col-span-2 sm:col-span-1">
            <dt className="text-txt-tertiary">Visible GPUs</dt>
            <dd className="font-medium tabular-nums text-txt-primary">
              {wait?.total_gpu_count ?? (devices.length || '—')}
            </dd>
          </div>
        </dl>
      </div>

      {devices.length > 0 && (
        <details className="group mt-2 rounded-md border border-border-subtle bg-surface-overlay/60">
          <summary className="flex min-h-8 cursor-pointer list-none items-center justify-between gap-2 px-2.5 py-1.5 text-2xs font-medium text-txt-secondary focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/35">
            <span>Why each GPU is waiting</span>
            <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
          </summary>
          <ul className="grid gap-1 border-t border-border-subtle p-2 sm:grid-cols-2" aria-label="GPU eligibility details">
            {devices.map(device => (
              <li key={device.uuid || device.index} className="flex min-w-0 items-start gap-2 rounded-md bg-surface-raised px-2 py-1.5 text-2xs">
                <span className={clsx(
                  'mt-1 h-2 w-2 flex-none rounded-full',
                  device.eligible ? 'bg-emerald-500' : 'bg-amber-500',
                )} />
                <span className="min-w-0">
                  <span className="block truncate font-medium text-txt-primary">
                    GPU {device.index}{device.name ? ` · ${device.name}` : ''}
                  </span>
                  <span className="block truncate text-txt-tertiary" title={device.reason || undefined}>
                    {device.eligible ? 'Passes current thresholds' : device.reason || 'Waiting for current thresholds'}
                  </span>
                  <span className="block truncate text-txt-tertiary/80">
                    {formatGPUDeviceMetrics(device)}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}

function formatGPUDeviceMetrics(device: NonNullable<GPUWaitStatus['devices']>[number]) {
  const parts: string[] = []
  if (device.free_memory_gb != null && Number.isFinite(Number(device.free_memory_gb))) {
    parts.push(`${Number(device.free_memory_gb).toFixed(1)} GiB free`)
  }
  if (device.memory_used_pct != null && Number.isFinite(Number(device.memory_used_pct))) {
    parts.push(`${Math.round(Number(device.memory_used_pct))}% memory`)
  }
  if (device.compute_util_pct != null && Number.isFinite(Number(device.compute_util_pct))) {
    parts.push(`${Math.round(Number(device.compute_util_pct))}% compute`)
  }
  return parts.join(' · ') || 'Metrics unavailable'
}

function formatDuration(totalSeconds: number) {
  const seconds = Math.max(0, Math.floor(totalSeconds))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remainder = seconds % 60
  if (hours > 0) {
    return `${hours}h ${minutes}m`
  }
  if (minutes > 0) {
    return `${minutes}m ${remainder}s`
  }
  return `${remainder}s`
}

function formatBytes(bytes: number) {
  const value = Math.max(0, Number(bytes || 0))
  if (value <= 0) {
    return 'bounded tail'
  }
  if (value >= 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(value % (1024 * 1024) === 0 ? 0 : 1)} MB`
  }
  return `${Math.max(1, Math.round(value / 1024))} KB`
}

function SidebarItem({
  task,
  active,
  exportMode,
  exportSelected,
  onClick,
}: {
  task: Task
  active: boolean
  exportMode: boolean
  exportSelected: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-current={!exportMode && active ? 'true' : undefined}
      aria-pressed={exportMode ? exportSelected : undefined}
      aria-label={`${exportMode ? (exportSelected ? 'Deselect' : 'Select') : 'View'} ${task.name}, ${task.status}`}
      className={clsx(
        'flex min-h-9 w-full items-center gap-1.5 rounded-md border px-2 py-1.5 text-left transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/35',
        exportMode && exportSelected && 'border-accent/25 bg-accent/10',
        !exportMode && active
          ? 'border-accent/25 bg-accent/10'
          : 'border-transparent hover:border-border-subtle hover:bg-surface-overlay'
      )}
      title={task.name}
      style={{ contentVisibility: 'auto', containIntrinsicSize: '44px' }}
    >
      {exportMode && <SelectionIndicator selected={exportSelected} />}
      <StatusDot status={task.status as TaskStatus} />
      <span className={clsx(
        'min-w-0 flex-1 break-all text-xs leading-4 truncate-2',
        active && !exportMode ? 'font-medium text-txt-primary' : 'text-txt-secondary'
      )}>
        {task.name}
      </span>
      {task.status === 'running' && <span className="h-1.5 w-1.5 flex-none rounded-full bg-amber-500 motion-safe:animate-pulse" aria-hidden="true" />}
    </button>
  )
}

function StatusDot({ status }: { status: TaskStatus }) {
  const colors: Record<TaskStatus, string> = {
    pending: 'bg-gray-500',
    queued: 'bg-blue-500',
    running: 'bg-amber-500',
    completed: 'bg-emerald-500',
    failed: 'bg-rose-500',
    cancelled: 'bg-slate-500',
  }

  return <span className={clsx('h-2 w-2 flex-none rounded-full', colors[status])} aria-hidden="true" />
}
