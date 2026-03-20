import { useCallback, useEffect, useRef, useState } from 'react'
import { Terminal as XTerminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { ChevronDown, Download, FileDown, Pin, Play, Rows3, Square } from 'lucide-react'
import clsx from 'clsx'
import { useMonitorStore, useTaskStore, useWorkspaceStore } from '@/store'
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
import * as api from '@/api'

export default function MonitorPage() {
  const { tasks, fetchTasks } = useTaskStore()
  const workspace = useWorkspaceStore(state => state.workspace)
  const {
    selectedTaskName, logContent, availableLogs, selectedLog, loading, exportIds,
    selectTask, selectLogFile, appendLog, toggleExport, selectAllExport, clearExport,
  } = useMonitorStore()

  const [sidebarQuery, setSidebarQuery] = useState('')
  const [exportMode, setExportMode] = useState(false)
  const [detailTask, setDetailTask] = useState<Task | null>(null)
  const termContainerRef = useRef<HTMLDivElement>(null)
  const xtermRef = useRef<XTerminal | null>(null)
  const fitAddonRef = useRef<FitAddon | null>(null)
  const observerRef = useRef<ResizeObserver | null>(null)
  const renderedLogRef = useRef<{ key: string; content: string } | null>(null)
  const selectedTaskNameRef = useRef<string | null>(selectedTaskName)
  const selectedLogRef = useRef(selectedLog)
  const liveLogNameRef = useRef('')

  const hasActive = tasks.some(task => task.status === 'running' || task.status === 'queued')
  const sidebarWidthRaw = Number(workspace?.settings?.monitor_sidebar_width_pct ?? 14)
  const sidebarWidthPct = Number.isFinite(sidebarWidthRaw) ? sidebarWidthRaw : 14
  const terminalVisible = Boolean(selectedTaskName)
  usePolling(fetchTasks, hasActive ? 3000 : 10000, true, false)

  useEffect(() => {
    void fetchTasks()
  }, [fetchTasks])

  useEffect(() => {
    const term = new XTerminal({
      cursorBlink: false,
      disableStdin: true,
      scrollback: 100000,
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
        void navigator.clipboard.writeText(selection).catch(() => {})
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
  }, [])

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
    if (tasks.length === 0 || !selectedTaskName) return
    const stillExists = tasks.some(task => task.name === selectedTaskName)
    if (stillExists) return
    useMonitorStore.setState({
      selectedTaskName: null,
      logContent: '',
      logOffset: 0,
      availableLogs: [],
      selectedLog: '',
    })
  }, [tasks, selectedTaskName])

  useEffect(() => {
    if (!detailTask) {
      return
    }

    const refreshed = tasks.find(task => task.name === detailTask.name)
    if (!refreshed) {
      setDetailTask(null)
      return
    }

    if (refreshed !== detailTask) {
      setDetailTask(refreshed)
    }
  }, [detailTask, tasks])

  const selectedTask = tasks.find(task => task.name === selectedTaskName)
  const liveLogName = selectedTask ? `run${Math.max(selectedTask.run_index || 1, 1)}.log` : ''
  const isLive = selectedTask?.status === 'running' && (!selectedLog || selectedLog === liveLogName)
  selectedTaskNameRef.current = selectedTaskName
  selectedLogRef.current = selectedLog
  liveLogNameRef.current = liveLogName

  const handleChunk = useCallback((message: LogStreamMessage) => {
    const activeTaskName = selectedTaskNameRef.current
    if (!activeTaskName || message.task_name !== activeTaskName) {
      return
    }

    const activeLog = selectedLogRef.current
    if (activeLog && activeLog !== liveLogNameRef.current) {
      return
    }

    appendLog(message.content)
  }, [appendLog])

  useLogStream({ taskName: selectedTaskName, onChunk: handleChunk, enabled: isLive })

  const filteredTasks = sidebarQuery
    ? tasks.filter(task => matchesTaskQuery(task, sidebarQuery))
    : tasks
  const pinnedTasks = filteredTasks.filter(task => task.pinned)
  const otherTasks = filteredTasks.filter(task => !task.pinned)
  const allExportSelected = filteredTasks.length > 0 && filteredTasks.every(task => exportIds.has(task.name))

  const handleSidebarClick = (task: Task) => {
    if (exportMode) {
      toggleExport(task.name)
      return
    }
    void selectTask(task.name)
  }

  const handleTaskAction = useCallback(async (action: 'run' | 'cancel') => {
    if (!selectedTaskName || !selectedTask) return

    const currentTaskName = selectedTaskName

    if (action === 'run') {
      await api.runTask(currentTaskName)
    } else {
      await api.cancelTask(currentTaskName)
    }

    await fetchTasks()

    const refreshedTasks = useTaskStore.getState().tasks
    if (refreshedTasks.some(task => task.name === currentTaskName)) {
      await selectTask(currentTaskName)
    }
  }, [selectedTaskName, selectedTask, fetchTasks, selectTask])

  const handleExport = useCallback(async () => {
    const names = [...exportIds]
    if (!names.length) return
    const blob = await api.exportTasksCsv(names)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `pyruns_export_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.csv`
    anchor.click()
    URL.revokeObjectURL(url)
  }, [exportIds])

  return (
    <div className="flex h-full overflow-hidden">
      <aside
        className="flex flex-col overflow-hidden border-r border-border-subtle bg-surface-raised"
        style={{ width: `${sidebarWidthPct}%` }}
      >
        <div className="border-b border-border-subtle px-2.5 py-2">
          <div className="mb-2 flex items-center justify-between">
            <div>
              <div className="text-2xs uppercase tracking-[0.18em] text-txt-tertiary">Monitor</div>
              <div className="text-sm font-medium text-txt-primary">{filteredTasks.length} task views</div>
            </div>
            <span className="rounded-full border border-border-subtle bg-surface-overlay px-2 py-1 text-2xs text-txt-secondary">
              {hasActive ? 'Live polling' : 'Idle polling'}
            </span>
          </div>
          <SearchInput value={sidebarQuery} onChange={setSidebarQuery} placeholder="Filter tasks..." debounceMs={150} />
        </div>

        <div className="flex-1 overflow-y-auto px-2 py-2">
          {pinnedTasks.length > 0 && (
            <CompactSection
              title="Pinned"
              subtitle={`${pinnedTasks.length} pinned`}
              icon={<Pin className="h-3.5 w-3.5 text-accent" />}
              accent
              className="mb-3"
              bodyClassName="space-y-1 p-1"
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

      <div className="flex min-w-0 flex-1 flex-col" style={{ background: '#0A0A0B' }}>
        <div className="flex items-center gap-2.5 border-b border-border-subtle bg-surface-raised px-3 py-2">
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
                <span className="inline-flex items-center gap-1 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-1 text-2xs text-emerald-400">
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

              <ActionButton variant="accentTint" onClick={() => setDetailTask(selectedTask)}>
                View Details
              </ActionButton>

              {availableLogs.length > 1 && (
                <div className="relative">
                  <select
                    value={selectedLog}
                    onChange={event => void selectLogFile(event.target.value)}
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
            void fetchTasks()
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
