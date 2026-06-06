import { useCallback, useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { Terminal as XTerminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { ChevronDown, Download, FileDown, Pin, Play, Rows3, Square } from 'lucide-react'
import clsx from 'clsx'
import { appendMonitorLogContent, useMonitorStore, useTaskStore, useToastStore, useWorkspaceStore } from '@/store'
import { useLogStream } from '@/hooks/useWebSocket'
import { usePolling } from '@/hooks/usePolling'
import SearchInput from '@/components/shared/SearchInput'
import StatusBadge from '@/components/shared/StatusBadge'
import SelectionIndicator from '@/components/shared/SelectionIndicator'
import EmptyState from '@/components/shared/EmptyState'
import ActionButton from '@/components/shared/ActionButton'
import CompactSection from '@/components/shared/CompactSection'
import TaskDetailPanel from '@/components/manager/TaskDetailPanel'
import type { LogStreamMessage, Task } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import { errorMessage } from '@/utils/errors'
import * as api from '@/api'
import {
  DEFAULT_MONITOR_SCROLLBACK,
  resolveMonitorChunkSize,
  resolveMonitorScrollback,
} from '@/utils/monitorSettings'

const MONITOR_SIDEBAR_WIDTH_STORAGE_KEY = 'pyruns.monitorSidebarWidthPct'
const DEFAULT_MONITOR_SIDEBAR_WIDTH = 14
const MIN_MONITOR_SIDEBAR_WIDTH = 10
const MAX_MONITOR_SIDEBAR_WIDTH = 35
const COMPACT_MONITOR_SIDEBAR_HEIGHT = 260
// Coalesce tiny stdout chunks so carriage-return progress bars paint as one frame.
const LOG_STREAM_FLUSH_MS = 50

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

export default function MonitorPage() {
  const { monitorTasks, fetchMonitorTasks, upsertMonitorTask } = useTaskStore()
  const workspace = useWorkspaceStore(state => state.workspace)
  const {
    selectedTaskName, logContent, availableLogs, selectedLog, loading, exportIds,
    selectTask, selectLogFile, appendLog, toggleExport, selectAllExport, clearExport,
  } = useMonitorStore()

  const [sidebarQuery, setSidebarQuery] = useState('')
  const [exportMode, setExportMode] = useState(false)
  const [detailTask, setDetailTask] = useState<Task | null>(null)
  const monitorShellRef = useRef<HTMLDivElement>(null)
  const termContainerRef = useRef<HTMLDivElement>(null)
  const xtermRef = useRef<XTerminal | null>(null)
  const fitAddonRef = useRef<FitAddon | null>(null)
  const observerRef = useRef<ResizeObserver | null>(null)
  const renderedLogRef = useRef<{ key: string; content: string } | null>(null)
  const selectedTaskNameRef = useRef<string | null>(selectedTaskName)
  const selectedLogRef = useRef(selectedLog)
  const liveLogNameRef = useRef('')
  const livePollingKeyRef = useRef('')
  const livePollInFlightRef = useRef(false)
  const wsStreamActiveRef = useRef(false)
  const pendingLiveLogChunkRef = useRef({ key: '', content: '' })
  const liveLogFlushTimerRef = useRef<number | null>(null)

  const selectedTask = useMemo(
    () => monitorTasks.find(task => task.name === selectedTaskName),
    [monitorTasks, selectedTaskName],
  )
  const liveLogName = selectedTask ? `run${Math.max(selectedTask.run_index || 1, 1)}.log` : ''
  const isLive = selectedTask?.status === 'running' && (!selectedLog || selectedLog === liveLogName)
  const hasActive = useMemo(
    () => monitorTasks.some(task => task.status === 'running' || task.status === 'queued'),
    [monitorTasks],
  )
  const monitorChunkSize = resolveMonitorChunkSize(workspace?.settings)
  const monitorScrollback = resolveMonitorScrollback(workspace?.settings)
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
    'flex h-full min-w-0 overflow-hidden',
    compactMonitorLayout ? 'flex-col' : 'flex-row',
  )
  usePolling(fetchMonitorTasks, hasActive ? 3000 : 10000, true, false)

  const startMonitorSidebarResize = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault()
    if (compactMonitorLayout) {
      return
    }
    setResizingMonitorSidebar(true)
  }, [compactMonitorLayout])

  useEffect(() => {
    void fetchMonitorTasks()
  }, [fetchMonitorTasks])

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
      scrollback: DEFAULT_MONITOR_SCROLLBACK,
      fontSize: 13,
      fontFamily: "'JetBrains Mono', 'Cascadia Code', Consolas, monospace",
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
    term.loadAddon(fitAddon)

    xtermRef.current = term
    fitAddonRef.current = fitAddon

    return () => {
      observerRef.current?.disconnect()
      term.dispose()
      xtermRef.current = null
      fitAddonRef.current = null
    }
  }, [notify])

  useEffect(() => {
    if (xtermRef.current) {
      xtermRef.current.options.scrollback = monitorScrollback
    }
  }, [monitorScrollback])

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

  const renderKey = `${selectedTaskName ?? ''}::${selectedLog || ''}`
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
      renderedLogRef.current = { key: renderKey, content: logContent }
      return
    }

    if (logContent === previous.content) {
      return
    }

    if (logContent.startsWith(previous.content)) {
      const nextChunk = logContent.slice(previous.content.length)
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

    renderedLogRef.current = { key: renderKey, content: logContent }
  }, [renderKey, selectedTaskName, logContent, shouldShowNoLogPlaceholder])

  useEffect(() => {
    if (monitorTasks.length === 0 || !selectedTaskName) return
    const stillExists = monitorTasks.some(task => task.name === selectedTaskName)
    if (stillExists) return
    useMonitorStore.setState({
      selectedTaskName: null,
      logContent: '',
      logOffset: 0,
      availableLogs: [],
      selectedLog: '',
    })
  }, [monitorTasks, selectedTaskName])

  useEffect(() => {
    if (!detailTask) {
      return
    }

    const refreshed = monitorTasks.find(task => task.name === detailTask.name)
    if (!refreshed) {
      setDetailTask(null)
      return
    }

    if (refreshed !== detailTask) {
      setDetailTask(current => current?.name === refreshed.name
        ? {
            ...refreshed,
            config: current.config,
            config_text: current.config_text,
            records: current.records,
            tracks: current.tracks,
          }
        : current)
    }
  }, [detailTask, monitorTasks])

  selectedTaskNameRef.current = selectedTaskName
  selectedLogRef.current = selectedLog
  liveLogNameRef.current = liveLogName

  const flushLiveLogChunkBuffer = useCallback(() => {
    if (liveLogFlushTimerRef.current !== null) {
      window.clearTimeout(liveLogFlushTimerRef.current)
      liveLogFlushTimerRef.current = null
    }

    const buffer = pendingLiveLogChunkRef.current
    if (!buffer.content) {
      return
    }

    pendingLiveLogChunkRef.current = { key: '', content: '' }

    const activeTaskName = selectedTaskNameRef.current
    const activeLog = selectedLogRef.current || liveLogNameRef.current
    const activeKey = activeTaskName ? `${activeTaskName}::${activeLog}` : ''
    if (buffer.key === activeKey) {
      appendLog(buffer.content)
    }
  }, [appendLog])

  useEffect(() => {
    return () => {
      if (liveLogFlushTimerRef.current !== null) {
        window.clearTimeout(liveLogFlushTimerRef.current)
        liveLogFlushTimerRef.current = null
      }
      pendingLiveLogChunkRef.current = { key: '', content: '' }
    }
  }, [])

  useEffect(() => {
    const key = `${selectedTaskName ?? ''}::${liveLogName}`
    if (livePollingKeyRef.current === key) {
      return
    }
    livePollingKeyRef.current = key
    livePollInFlightRef.current = false
    wsStreamActiveRef.current = false
  }, [liveLogName, selectedTaskName])

  const handleChunk = useCallback((message: LogStreamMessage) => {
    const activeTaskName = selectedTaskNameRef.current
    if (!activeTaskName || message.task_name !== activeTaskName) {
      return
    }

    const activeLog = selectedLogRef.current
    if (activeLog && activeLog !== liveLogNameRef.current) {
      return
    }

    wsStreamActiveRef.current = true
    const key = `${activeTaskName}::${activeLog || liveLogNameRef.current}`
    const buffer = pendingLiveLogChunkRef.current
    if (buffer.key === key) {
      pendingLiveLogChunkRef.current = { key, content: buffer.content + message.content }
    } else {
      pendingLiveLogChunkRef.current = { key, content: message.content }
    }
    if (liveLogFlushTimerRef.current === null) {
      liveLogFlushTimerRef.current = window.setTimeout(flushLiveLogChunkBuffer, LOG_STREAM_FLUSH_MS)
    }
  }, [flushLiveLogChunkBuffer])

  useLogStream({ taskName: selectedTaskName, onChunk: handleChunk, enabled: isLive })

  const pollLiveLog = useCallback(async () => {
    const activeTaskName = selectedTaskNameRef.current
    const liveLog = liveLogNameRef.current
    if (!activeTaskName || !liveLog || wsStreamActiveRef.current || livePollInFlightRef.current) {
      return
    }

    livePollInFlightRef.current = true
    const requestedLog = selectedLogRef.current || liveLog
    const currentOffset = useMonitorStore.getState().logOffset
    try {
      const logs = await api.getTaskLogs(activeTaskName, {
        logFileName: requestedLog,
        offset: currentOffset,
        chunkSize: monitorChunkSize,
      })
      if (selectedTaskNameRef.current !== activeTaskName) {
        return
      }
      const stillViewingLog = selectedLogRef.current || liveLogNameRef.current
      if (logs.selected_log && stillViewingLog && logs.selected_log !== stillViewingLog) {
        return
      }

      useMonitorStore.setState(state => ({
        logContent: logs.content ? appendMonitorLogContent(state.logContent, logs.content) : state.logContent,
        logOffset: logs.offset,
        availableLogs: logs.available_logs,
        selectedLog: state.selectedLog || logs.selected_log,
      }))
    } catch {
      // Keep the monitor quiet; task polling still refreshes status.
    } finally {
      livePollInFlightRef.current = false
    }
  }, [monitorChunkSize])

  usePolling(pollLiveLog, 1000, Boolean(isLive), false)

  const filteredTasks = useMemo(
    () => sidebarQuery
      ? monitorTasks.filter(task => matchesTaskQuery(task, sidebarQuery))
      : monitorTasks,
    [monitorTasks, sidebarQuery],
  )
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

  const handleSidebarClick = (task: Task) => {
    if (exportMode) {
      toggleExport(task.name)
      return
    }
    void selectTask(task.name)
      .catch(err => notify({ tone: 'error', title: 'Could not load task logs', detail: errorMessage(err) }))
  }

  const handleTaskAction = useCallback(async (action: 'run' | 'cancel') => {
    if (!selectedTaskName || !selectedTask) return

    const currentTaskName = selectedTaskName

    try {
      let task: Task | null = null
      if (action === 'run') {
        task = (await api.runTask(currentTaskName)).task
      } else {
        task = (await api.cancelTask(currentTaskName)).task
      }

      if (task) {
        upsertMonitorTask(task)
      }

      await fetchMonitorTasks()

      const refreshedTasks = useTaskStore.getState().monitorTasks
      if (refreshedTasks.some(task => task.name === currentTaskName)) {
        await selectTask(currentTaskName)
      }
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
    }
  }, [selectedTaskName, selectedTask, fetchMonitorTasks, selectTask, upsertMonitorTask, notify])

  const openDetailTask = useCallback((task: Task) => {
    setDetailTask(task)
    void api.getTask(task.name).then(fullTask => {
      setDetailTask(current => current?.name === task.name ? fullTask : current)
    }).catch(err => {
      notify({ tone: 'error', title: 'Could not load task details', detail: errorMessage(err) })
    })
  }, [notify])

  const handleExport = useCallback(async () => {
    const names = [...exportIds]
    if (!names.length) return
    try {
      const blob = await api.exportTasksCsv(names)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = `pyruns_export_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.csv`
      anchor.click()
      URL.revokeObjectURL(url)
      setExportMode(false)
      clearExport()
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
    void selectLogFile(logName)
      .catch(err => notify({ tone: 'error', title: 'Could not load log file', detail: errorMessage(err) }))
  }, [notify, selectLogFile])

  return (
    <div ref={monitorShellRef} className={monitorShellClassName}>
      <aside
        className={clsx(
          'flex flex-none flex-col overflow-hidden bg-surface-raised',
          compactMonitorLayout ? 'border-b border-border-subtle' : 'border-r border-border-subtle',
        )}
        style={compactMonitorLayout ? { height: COMPACT_MONITOR_SIDEBAR_HEIGHT } : { width: `${monitorSidebarWidthPct}%` }}
      >
        <div className="border-b border-border-subtle px-2.5 py-2">
          <div className="mb-2 flex items-center justify-between">
            <div>
              <div className="text-2xs uppercase tracking-[0.18em] text-txt-tertiary">Monitor</div>
              <div className="text-sm font-medium text-txt-primary">{filteredTasks.length} task views</div>
            </div>
            <span className="rounded-md bg-surface-overlay px-2 py-1 text-2xs text-txt-secondary">
              {hasActive ? 'Live polling' : 'Idle polling'}
            </span>
          </div>
          <SearchInput value={sidebarQuery} onChange={setSidebarQuery} placeholder="Filter tasks..." debounceMs={150} />
        </div>

        <div className="flex-1 overflow-y-auto px-2 py-2">
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
              <div className="px-2 py-5 text-center text-2xs text-txt-tertiary">No tasks</div>
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
        </div>

        <div className="border-t border-border-subtle px-2.5 py-2">
          {!exportMode ? (
            <ActionButton
              icon={<FileDown className="h-3.5 w-3.5" />}
              variant="primary"
              className="w-full"
              onClick={() => setExportMode(true)}
            >
              Export
            </ActionButton>
          ) : (
            <div className="space-y-2">
              <div className="flex items-center justify-between px-0.5 text-2xs">
                <button
                  type="button"
                  onClick={() => (allExportSelected ? clearExport() : selectAllExport(filteredTasks.map(task => task.name)))}
                  className="text-accent transition-colors hover:text-accent-hover"
                >
                  {allExportSelected ? 'Deselect All' : 'Select All'}
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
          aria-label="Resize monitor sidebar"
          aria-orientation="vertical"
          onPointerDown={startMonitorSidebarResize}
          className={clsx(
            'h-full w-1 flex-none cursor-col-resize touch-none transition-colors focus:outline-none focus:ring-2 focus:ring-accent/35',
            resizingMonitorSidebar ? 'bg-accent/45' : 'bg-transparent hover:bg-accent/25',
          )}
        />
      )}

      <div className="flex min-h-0 min-w-0 flex-1 flex-col" style={{ background: '#0A0A0B' }}>
        <div className="flex flex-wrap items-center gap-2.5 border-b border-border-subtle bg-surface-raised px-3 py-2">
          {selectedTask ? (
            <>
              <StatusBadge status={selectedTask.status as TaskStatus} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-txt-primary" title={selectedTask.name}>
                  {selectedTask.name}
                </div>
                <div className="truncate text-2xs text-txt-tertiary" title={selectedLog || liveLogName || 'latest log'}>
                  {selectedLog || liveLogName || 'latest log'}
                </div>
              </div>

              {isLive && (
                <span className="inline-flex items-center gap-1 rounded-md bg-emerald-500/10 px-2 py-1 text-2xs text-emerald-400">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
                  Live
                </span>
              )}

              {(selectedTask.status === 'pending' || selectedTask.status === 'failed' || selectedTask.status === 'completed') && (
                <ActionButton
                  icon={<Play className="h-3.5 w-3.5" />}
                  variant="success"
                  onClick={() => void handleTaskAction('run')}
                >
                  Run
                </ActionButton>
              )}

              {(selectedTask.status === 'running' || selectedTask.status === 'queued') && (
                <ActionButton
                  icon={<Square className="h-3.5 w-3.5" />}
                  variant="danger"
                  onClick={() => void handleTaskAction('cancel')}
                >
                  Stop
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
                    className="appearance-none rounded-md border border-border-subtle bg-surface-overlay px-2 py-1.5 pr-6 text-2xs text-txt-primary outline-none transition-colors focus:border-border"
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

        <div className="flex-1 overflow-hidden">
          {selectedTaskName ? (
            <div ref={termContainerRef} className="monitor-terminal-shell h-full w-full" />
          ) : (
            <div className="flex h-full items-center justify-center">
              <EmptyState title="No task selected" description="Select a task from the sidebar to inspect logs" />
            </div>
          )}
        </div>
      </div>

      {detailTask && (
        <TaskDetailPanel
          task={detailTask}
          onClose={() => setDetailTask(null)}
          onRefresh={() => {
            void fetchMonitorTasks()
          }}
        />
      )}
    </div>
  )
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
      className={clsx(
        'flex w-full items-center gap-1.5 rounded-md border px-2 py-1 text-left transition-colors',
        exportMode && exportSelected && 'border-accent/25 bg-accent/10',
        !exportMode && active
          ? 'border-accent/25 bg-accent/10'
          : 'border-transparent hover:border-border-subtle hover:bg-surface-overlay'
      )}
      title={task.name}
    >
      {exportMode && <SelectionIndicator selected={exportSelected} />}
      <StatusDot status={task.status as TaskStatus} />
      <span className={clsx(
        'flex-1 truncate text-xs',
        active && !exportMode ? 'font-medium text-txt-primary' : 'text-txt-secondary'
      )}>
        {task.name}
      </span>
      {task.status === 'running' && <span className="h-1.5 w-1.5 flex-none animate-pulse rounded-full bg-amber-400" />}
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
  }

  return <span className={clsx('h-2 w-2 flex-none rounded-full', colors[status])} />
}

function matchesTaskQuery(task: Task, query: string) {
  const haystack = [
    task.name,
    task.notes,
    task.preview_text,
    task.search_text,
    task.config_text,
  ]
    .filter(Boolean)
    .join('\n')
    .toLowerCase()

  const terms = query
    .split(/\r?\n/)
    .map(item => item.trim().toLowerCase())
    .filter(Boolean)

  if (terms.length === 0) {
    return true
  }

  return terms.every(term => haystack.includes(term))
}
