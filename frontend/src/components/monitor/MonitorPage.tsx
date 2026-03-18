import { useEffect, useRef, useCallback, useState } from 'react'
import { Terminal as XTerminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { ChevronDown, Download, FileDown, Play, Square } from 'lucide-react'
import clsx from 'clsx'
import { useMonitorStore, useTaskStore } from '@/store'
import { useLogStream } from '@/hooks/useWebSocket'
import { usePolling } from '@/hooks/usePolling'
import SearchInput from '@/components/shared/SearchInput'
import StatusBadge from '@/components/shared/StatusBadge'
import EmptyState from '@/components/shared/EmptyState'
import type { Task } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import * as api from '@/api'

export default function MonitorPage() {
  const { tasks, fetchTasks } = useTaskStore()
  const {
    selectedTaskName, logContent, availableLogs, selectedLog,
    exportIds,
    selectTask, selectLogFile,
    toggleExport, selectAllExport, clearExport,
  } = useMonitorStore()

  const [sidebarQuery, setSidebarQuery] = useState('')
  const [exportMode, setExportMode] = useState(false)
  const termContainerRef = useRef<HTMLDivElement>(null)
  const xtermRef = useRef<XTerminal | null>(null)
  const fitAddonRef = useRef<FitAddon | null>(null)
  const observerRef = useRef<ResizeObserver | null>(null)
  const renderedLogRef = useRef<{ key: string; content: string } | null>(null)
  const initialSelectionResolvedRef = useRef(false)

  const hasActive = tasks.some(t => t.status === 'running' || t.status === 'queued')
  usePolling(fetchTasks, hasActive ? 3000 : 10000, true, false)

  useEffect(() => { fetchTasks() }, [fetchTasks])

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
    const term = xtermRef.current
    const fitAddon = fitAddonRef.current
    const container = termContainerRef.current
    if (!term || !fitAddon || !container) return

    if (!term.element) {
      term.open(container)
    } else if (term.element.parentElement !== container) {
      container.appendChild(term.element)
    }

    const rafId = requestAnimationFrame(() => {
      try { fitAddon.fit() } catch {}
    })

    observerRef.current?.disconnect()
    const observer = new ResizeObserver(() => {
      try { fitAddon.fit() } catch {}
    })
    observer.observe(container)
    observerRef.current = observer

    return () => { cancelAnimationFrame(rafId) }
  }, [selectedTaskName])

  const renderKey = `${selectedTaskName ?? ''}::${selectedLog || ''}`

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
      }
    }

    renderedLogRef.current = { key: renderKey, content: logContent }
  }, [renderKey, selectedTaskName, logContent])

  const getPreferredTask = useCallback((items: Task[]) => {
    const activeTasks = items
      .filter(task => task.status === 'running' || task.status === 'queued')
      .sort((a, b) => {
        const aTime = a.start_times?.[a.start_times.length - 1] || a.created_at || ''
        const bTime = b.start_times?.[b.start_times.length - 1] || b.created_at || ''
        return String(bTime).localeCompare(String(aTime))
      })

    if (activeTasks.length > 0) {
      return activeTasks[0]
    }

    if (selectedTaskName) {
      const existing = items.find(task => task.name === selectedTaskName)
      if (existing) {
        return existing
      }
    }

    return items[0] || null
  }, [selectedTaskName])

  useEffect(() => {
    if (initialSelectionResolvedRef.current || tasks.length === 0) return
    const preferred = getPreferredTask(tasks)
    if (!preferred) return
    initialSelectionResolvedRef.current = true
    void selectTask(preferred.name)
  }, [tasks, getPreferredTask, selectTask])

  useEffect(() => {
    if (tasks.length === 0 || !selectedTaskName) return
    const stillExists = tasks.some(task => task.name === selectedTaskName)
    if (stillExists) return
    const fallback = getPreferredTask(tasks)
    if (fallback) {
      void selectTask(fallback.name)
    }
  }, [tasks, selectedTaskName, getPreferredTask, selectTask])

  const handleChunk = useCallback((text: string) => {
    xtermRef.current?.write(text)
    const currentKey = `${selectedTaskName ?? ''}::${selectedLog || ''}`
    const previous = renderedLogRef.current
    renderedLogRef.current = {
      key: currentKey,
      content: previous?.key === currentKey ? `${previous.content}${text}` : text,
    }
  }, [selectedTaskName, selectedLog])

  const selectedTask = tasks.find(t => t.name === selectedTaskName)
  const liveLogName = selectedTask ? `run${Math.max(selectedTask.run_index || 1, 1)}.log` : ''
  const isLive = selectedTask?.status === 'running' && (!selectedLog || selectedLog === liveLogName)

  useLogStream({ taskName: selectedTaskName, onChunk: handleChunk, enabled: isLive })

  const filteredTasks = sidebarQuery
    ? tasks.filter(t => t.name.toLowerCase().includes(sidebarQuery.toLowerCase()))
    : tasks
  const pinnedTasks = filteredTasks.filter(t => t.pinned)
  const otherTasks = filteredTasks.filter(t => !t.pinned)

  const handleExport = useCallback(async () => {
    const names = [...exportIds]
    if (!names.length) return
    const lines: string[] = []
    for (const name of names) {
      try {
        const logs = await api.getTaskLogs(name, undefined, undefined, 50000)
        lines.push(`${'='.repeat(60)}`)
        lines.push(`TASK: ${name}`)
        lines.push(`${'='.repeat(60)}`)
        lines.push(logs.content || '(no logs)')
        lines.push('')
      } catch {
        lines.push(`TASK: ${name} (failed to load logs)`)
        lines.push('')
      }
    }
    const blob = new Blob([lines.join('\n')], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `pyruns_export_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.txt`
    a.click()
    URL.revokeObjectURL(url)
  }, [exportIds])

  const handleSidebarClick = (task: Task) => {
    if (exportMode) {
      toggleExport(task.name)
    } else {
      void selectTask(task.name)
    }
  }

  const handleTaskAction = useCallback(async (action: 'run' | 'cancel') => {
    if (!selectedTaskName || !selectedTask) return
    if (action === 'run') {
      await api.runTask(selectedTaskName)
    } else {
      await api.cancelTask(selectedTaskName)
    }
    await fetchTasks()
    await selectTask(selectedTaskName)
  }, [selectedTaskName, selectedTask, fetchTasks, selectTask])

  const allExportSelected = filteredTasks.length > 0 && filteredTasks.every(t => exportIds.has(t.name))

  return (
    <div className="flex h-full overflow-hidden">
      <div className="flex w-[25%] min-w-[220px] max-w-[340px] flex-col overflow-hidden border-r border-border-subtle bg-surface-raised">
        <div className="border-b border-border-subtle px-3 py-3">
          <div className="mb-2 flex items-center justify-between">
            <div>
              <div className="text-2xs uppercase tracking-[0.18em] text-txt-tertiary">Monitor</div>
              <div className="text-sm font-semibold text-txt-primary">{filteredTasks.length} task views</div>
            </div>
            <span className="rounded-full border border-border-subtle bg-surface-overlay px-2 py-1 text-2xs text-txt-secondary">
              {hasActive ? 'Live polling' : 'Idle polling'}
            </span>
          </div>
          <SearchInput value={sidebarQuery} onChange={setSidebarQuery} placeholder="Filter tasks..." debounceMs={150} />
        </div>

        <div className="flex-1 overflow-y-auto">
          {pinnedTasks.length > 0 && (
            <div className="border-b border-border-subtle px-2 py-2">
              <div className="px-2 py-1.5 text-2xs uppercase tracking-[0.18em] text-txt-tertiary">Pinned</div>
              <div className="space-y-1">
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
              </div>
            </div>
          )}

          <div className="space-y-1 px-2 py-2">
            {otherTasks.length === 0 && pinnedTasks.length === 0 ? (
              <div className="px-3 py-8 text-center text-2xs text-txt-tertiary">No tasks</div>
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
          </div>
        </div>

        <div className="border-t border-border-subtle px-3 py-2.5">
          {!exportMode ? (
            <button
              type="button"
              onClick={() => setExportMode(true)}
              className="flex w-full items-center justify-center gap-2 rounded-xl border border-border px-3 py-2 text-xs text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
              title="Export logs"
            >
              <FileDown className="h-3.5 w-3.5" /> Export Logs
            </button>
          ) : (
            <div className="flex flex-col gap-2">
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => allExportSelected ? clearExport() : selectAllExport(filteredTasks.map(t => t.name))}
                  className="text-2xs text-accent transition-colors hover:text-accent-hover"
                >
                  {allExportSelected ? 'Deselect All' : 'Select All'}
                </button>
                <span className="flex-1 text-right text-2xs text-txt-tertiary">
                  {exportIds.size} selected
                </span>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={handleExport}
                  disabled={exportIds.size === 0}
                  className="flex flex-1 items-center justify-center gap-1.5 rounded-xl bg-accent/15 px-3 py-2 text-xs font-medium text-accent transition-colors hover:bg-accent/25 disabled:opacity-30"
                >
                  <Download className="h-3.5 w-3.5" /> Download
                </button>
                <button
                  type="button"
                  onClick={() => { setExportMode(false); clearExport() }}
                  className="rounded-xl px-3 py-2 text-xs text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col" style={{ background: '#0A0A0B' }}>
        <div className="flex items-center gap-3 border-b border-border-subtle bg-surface-raised px-4 py-2.5">
          {selectedTask ? (
            <>
              <StatusBadge status={selectedTask.status as TaskStatus} />
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-semibold text-txt-primary">{selectedTask.name}</div>
                <div className="text-2xs text-txt-tertiary">
                  {selectedLog || liveLogName || 'latest log'}
                </div>
              </div>
              {isLive && (
                <span className="flex items-center gap-1 rounded-full border border-emerald-500/20 bg-emerald-500/10 px-2 py-1 text-2xs text-emerald-400">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
                  Live
                </span>
              )}
              {(selectedTask.status === 'pending' || selectedTask.status === 'failed' || selectedTask.status === 'completed') && (
                <button
                  type="button"
                  onClick={() => void handleTaskAction('run')}
                  className="flex items-center gap-1.5 rounded-md border border-border px-2.5 py-1 text-2xs text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
                >
                  <Play className="h-3.5 w-3.5" /> Run
                </button>
              )}
              {(selectedTask.status === 'running' || selectedTask.status === 'queued') && (
                <button
                  type="button"
                  onClick={() => void handleTaskAction('cancel')}
                  className="flex items-center gap-1.5 rounded-md border border-rose-500/20 px-2.5 py-1 text-2xs text-rose-400 transition-colors hover:bg-rose-500/10"
                >
                  <Square className="h-3.5 w-3.5" /> Stop
                </button>
              )}
              {availableLogs.length > 1 && (
                <div className="relative">
                  <select
                    value={selectedLog}
                    onChange={e => void selectLogFile(e.target.value)}
                    title="Select log file"
                    className="appearance-none rounded-md border border-border bg-surface-overlay px-2 py-1 pr-6 text-2xs text-txt-primary outline-none transition-colors focus:border-accent/50"
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
              <EmptyState title="No task selected" description="Click a task in the sidebar to view its logs" />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function SidebarItem({ task, active, exportMode, exportSelected, onClick }: {
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
        'flex w-full items-center gap-2 rounded-lg border px-3 py-2 text-left transition-colors',
        exportMode && exportSelected && 'border-accent/20 bg-accent/10',
        !exportMode && active
          ? 'border-accent/25 bg-accent/10'
          : 'border-transparent hover:border-border-subtle hover:bg-surface-overlay'
      )}
    >
      {exportMode && (
        <span className={clsx(
          'flex h-4 w-4 flex-none items-center justify-center rounded border transition-colors',
          exportSelected ? 'border-accent bg-accent' : 'border-border-strong bg-surface-overlay'
        )}>
          {exportSelected && <span className="text-[10px] font-bold text-white">x</span>}
        </span>
      )}
      <StatusDot status={task.status as TaskStatus} />
      <span className={clsx(
        'flex-1 truncate text-xs',
        active && !exportMode ? 'font-medium text-txt-primary' : 'text-txt-secondary'
      )}>
        {task.name}
      </span>
      {task.status === 'running' && (
        <span className="h-1.5 w-1.5 flex-none animate-pulse rounded-full bg-amber-400" />
      )}
    </button>
  )
}

function StatusDot({ status }: { status: TaskStatus }) {
  const colors: Record<string, string> = {
    pending: 'bg-gray-500',
    queued: 'bg-blue-500',
    running: 'bg-amber-500',
    completed: 'bg-emerald-500',
    failed: 'bg-rose-500',
  }
  return <span className={clsx('h-2 w-2 flex-none rounded-full', colors[status] || 'bg-gray-500')} />
}
