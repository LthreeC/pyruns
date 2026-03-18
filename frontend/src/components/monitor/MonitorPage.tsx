import { useEffect, useRef, useCallback, useState } from 'react'
import { Terminal as XTerminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import '@xterm/xterm/css/xterm.css'
import { ChevronDown, Download, FileDown } from 'lucide-react'
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
    selectTask, selectLogFile, appendLog,
    toggleExport, selectAllExport, clearExport,
  } = useMonitorStore()

  const [sidebarQuery, setSidebarQuery] = useState('')
  const [showExportMode, setShowExportMode] = useState(false)
  const termContainerRef = useRef<HTMLDivElement>(null)
  const xtermRef = useRef<XTerminal | null>(null)
  const fitAddonRef = useRef<FitAddon | null>(null)
  const observerRef = useRef<ResizeObserver | null>(null)

  const hasActive = tasks.some(t => t.status === 'running' || t.status === 'queued')
  usePolling(fetchTasks, hasActive ? 3000 : 10000)

  useEffect(() => { fetchTasks() }, [])

  // Create xterm instance once, persist across task switches
  useEffect(() => {
    const term = new XTerminal({
      cursorBlink: false,
      disableStdin: true,
      scrollback: 100000,
      fontSize: 13,
      fontFamily: "'JetBrains Mono', 'Fira Code', Consolas, monospace",
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

    // Ctrl+C copy support
    term.attachCustomKeyEventHandler((ev) => {
      if (ev.type === 'keydown' && ev.ctrlKey && ev.key === 'c') {
        const sel = term.getSelection()
        if (sel) {
          navigator.clipboard.writeText(sel).catch(() => {})
          term.clearSelection()
          return false // prevent default
        }
      }
      // Ctrl+A select all
      if (ev.type === 'keydown' && ev.ctrlKey && ev.key === 'a') {
        term.selectAll()
        return false
      }
      return true
    })

    xtermRef.current = term
    fitAddonRef.current = fitAddon

    return () => {
      observerRef.current?.disconnect()
      term.dispose()
      xtermRef.current = null
      fitAddonRef.current = null
    }
  }, [])

  // Attach/detach terminal to container when selectedTaskName changes
  useEffect(() => {
    const term = xtermRef.current
    const fitAddon = fitAddonRef.current
    const container = termContainerRef.current
    if (!term || !fitAddon || !container) return

    // If terminal is not yet opened, open it
    if (!term.element) {
      term.open(container)
    } else if (term.element.parentElement !== container) {
      // Move terminal element to new container
      container.appendChild(term.element)
    }

    // Fit after a frame to ensure container is sized
    const rafId = requestAnimationFrame(() => {
      try { fitAddon.fit() } catch {}
    })

    // Observe resize
    observerRef.current?.disconnect()
    const observer = new ResizeObserver(() => {
      try { fitAddon.fit() } catch {}
    })
    observer.observe(container)
    observerRef.current = observer

    return () => {
      cancelAnimationFrame(rafId)
    }
  }, [selectedTaskName])

  // Write historical log content when it changes
  useEffect(() => {
    const term = xtermRef.current
    if (!term) return
    term.clear()
    term.reset()
    if (logContent) {
      term.write(logContent)
    }
  }, [logContent])

  // Live log streaming
  const handleChunk = useCallback((text: string) => {
    const term = xtermRef.current
    if (term) {
      term.write(text)
    }
    appendLog(text)
  }, [])

  const selectedTask = tasks.find(t => t.name === selectedTaskName)
  const isLive = selectedTask?.status === 'running'

  useLogStream({
    taskName: selectedTaskName,
    onChunk: handleChunk,
    enabled: isLive,
  })

  // Filter sidebar tasks
  const filteredTasks = sidebarQuery
    ? tasks.filter(t => t.name.toLowerCase().includes(sidebarQuery.toLowerCase()))
    : tasks

  const pinnedTasks = filteredTasks.filter(t => t.pinned)
  const otherTasks = filteredTasks.filter(t => !t.pinned)

  // Export handler
  const handleExport = useCallback(async () => {
    const names = [...exportIds]
    if (!names.length) return
    // Collect logs for all selected tasks
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
        lines.push(`TASK: ${name} — (failed to load logs)`)
        lines.push('')
      }
    }
    // Download as text file
    const blob = new Blob([lines.join('\n')], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `pyruns_export_${new Date().toISOString().slice(0, 19).replace(/:/g, '-')}.txt`
    a.click()
    URL.revokeObjectURL(url)
  }, [exportIds])

  const allExportSelected = filteredTasks.length > 0 && filteredTasks.every(t => exportIds.has(t.name))

  return (
    <div className="h-full flex overflow-hidden">
      {/* Left sidebar */}
      <div className="w-60 flex-none border-r border-border-subtle bg-surface-raised flex flex-col overflow-hidden">
        {/* Sidebar header */}
        <div className="px-3 py-2.5 border-b border-border-subtle flex-none space-y-2">
          <SearchInput value={sidebarQuery} onChange={setSidebarQuery} placeholder="Filter tasks..." debounceMs={150} />
          {/* Export toggle */}
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => { setShowExportMode(!showExportMode); if (showExportMode) clearExport() }}
              className={clsx(
                'flex items-center gap-1 px-2 py-1 rounded text-2xs transition-colors',
                showExportMode ? 'bg-accent/15 text-accent font-medium' : 'text-txt-tertiary hover:text-txt-secondary'
              )}
              title="Toggle export mode"
            >
              <FileDown className="w-3 h-3" /> Export
            </button>
            {showExportMode && (
              <>
                <button
                  type="button"
                  onClick={() => allExportSelected ? clearExport() : selectAllExport(filteredTasks.map(t => t.name))}
                  className="text-2xs text-txt-tertiary hover:text-txt-secondary transition-colors"
                >
                  {allExportSelected ? 'None' : 'All'}
                </button>
                <div className="flex-1" />
                <button
                  type="button"
                  onClick={handleExport}
                  disabled={exportIds.size === 0}
                  className="flex items-center gap-1 px-2 py-1 rounded text-2xs bg-accent/15 text-accent hover:bg-accent/25 transition-colors disabled:opacity-30"
                  title="Download logs"
                >
                  <Download className="w-3 h-3" /> {exportIds.size > 0 ? `(${exportIds.size})` : ''}
                </button>
              </>
            )}
          </div>
        </div>

        {/* Task list */}
        <div className="flex-1 overflow-y-auto">
          {pinnedTasks.length > 0 && (
            <div className="border-b border-border-subtle">
              <div className="px-3 py-1.5 text-2xs text-txt-tertiary uppercase tracking-wider">Pinned</div>
              {pinnedTasks.map(task => (
                <SidebarItem
                  key={task.name}
                  task={task}
                  active={task.name === selectedTaskName}
                  showExport={showExportMode}
                  exportSelected={exportIds.has(task.name)}
                  onToggleExport={() => toggleExport(task.name)}
                  onClick={() => selectTask(task.name)}
                />
              ))}
            </div>
          )}
          {otherTasks.length === 0 && pinnedTasks.length === 0 ? (
            <div className="px-3 py-8 text-center text-2xs text-txt-tertiary">No tasks</div>
          ) : (
            otherTasks.map(task => (
              <SidebarItem
                key={task.name}
                task={task}
                active={task.name === selectedTaskName}
                showExport={showExportMode}
                exportSelected={exportIds.has(task.name)}
                onToggleExport={() => toggleExport(task.name)}
                onClick={() => selectTask(task.name)}
              />
            ))
          )}
        </div>
      </div>

      {/* Right terminal panel */}
      <div className="flex-1 flex flex-col min-w-0" style={{ background: '#0A0A0B' }}>
        {/* Terminal header */}
        <div className="flex items-center gap-3 px-4 py-2 border-b border-border-subtle flex-none bg-surface-raised">
          {selectedTask ? (
            <>
              <StatusBadge status={selectedTask.status as TaskStatus} />
              <span className="text-sm text-txt-primary truncate">{selectedTask.name}</span>
              {isLive && (
                <span className="flex items-center gap-1 text-2xs text-emerald-400">
                  <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />
                  Live
                </span>
              )}
              <div className="flex-1" />
              {availableLogs.length > 1 && (
                <div className="relative">
                  <select
                    value={selectedLog}
                    onChange={e => selectLogFile(e.target.value)}
                    title="Select log file"
                    className="appearance-none bg-surface-overlay border border-border rounded px-2 py-1 text-2xs text-txt-primary outline-none focus:border-accent/50 cursor-pointer pr-6"
                  >
                    {availableLogs.map(log => (
                      <option key={log} value={log}>{log}</option>
                    ))}
                  </select>
                  <ChevronDown className="absolute right-1.5 top-1/2 -translate-y-1/2 w-3 h-3 text-txt-tertiary pointer-events-none" />
                </div>
              )}
              <span className="text-2xs text-txt-tertiary">Ctrl+C to copy</span>
            </>
          ) : (
            <span className="text-xs text-txt-tertiary">Select a task to view logs</span>
          )}
        </div>

        {/* Terminal */}
        <div className="flex-1 overflow-hidden">
          {selectedTaskName ? (
            <div ref={termContainerRef} className="w-full h-full" />
          ) : (
            <div className="flex items-center justify-center h-full">
              <EmptyState title="No task selected" description="Click a task in the sidebar to view its logs" />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/* ── Sidebar Item ── */
function SidebarItem({ task, active, showExport, exportSelected, onToggleExport, onClick }: {
  task: Task; active: boolean
  showExport: boolean; exportSelected: boolean; onToggleExport: () => void
  onClick: () => void
}) {
  return (
    <div
      className={clsx(
        'flex items-center gap-2 px-3 py-2 transition-colors cursor-pointer',
        active
          ? 'bg-accent/10 border-l-2 border-accent'
          : 'border-l-2 border-transparent hover:bg-surface-overlay'
      )}
    >
      {showExport && (
        <input
          type="checkbox"
          checked={exportSelected}
          onChange={e => { e.stopPropagation(); onToggleExport() }}
          onClick={e => e.stopPropagation()}
          title={`Select ${task.name} for export`}
          className="w-3 h-3 rounded border-border accent-accent cursor-pointer flex-none"
        />
      )}
      <button
        type="button"
        onClick={onClick}
        className="flex items-center gap-2 flex-1 min-w-0 text-left"
      >
        <StatusDot status={task.status as TaskStatus} />
        <span className={clsx(
          'text-xs truncate flex-1',
          active ? 'text-txt-primary font-medium' : 'text-txt-secondary'
        )}>
          {task.name}
        </span>
        {task.status === 'running' && (
          <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse flex-none" />
        )}
      </button>
    </div>
  )
}

function StatusDot({ status }: { status: TaskStatus }) {
  const colors: Record<string, string> = {
    pending: 'bg-gray-500', queued: 'bg-blue-500', running: 'bg-amber-500',
    completed: 'bg-emerald-500', failed: 'bg-rose-500',
  }
  return <span className={clsx('w-2 h-2 rounded-full flex-none', colors[status] || 'bg-gray-500')} />
}
