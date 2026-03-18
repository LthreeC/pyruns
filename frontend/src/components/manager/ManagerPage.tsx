import { useEffect, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  RefreshCw, Play, Square, RotateCcw, Pin, Trash2, ChevronDown,
  MoreHorizontal, Terminal, CheckSquare, XSquare,
} from 'lucide-react'
import clsx from 'clsx'
import { useTaskStore } from '@/store'
import { usePolling } from '@/hooks/usePolling'
import StatusBadge from '@/components/shared/StatusBadge'
import SearchInput from '@/components/shared/SearchInput'
import Pagination from '@/components/shared/Pagination'
import EmptyState from '@/components/shared/EmptyState'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import TaskDetailPanel from './TaskDetailPanel'
import type { Task } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import { ALL_STATUSES, STATUS_LABELS, STATUS_COLORS } from '@/theme/tokens'
import * as api from '@/api'

const STATUS_OPTIONS = ['All', ...ALL_STATUSES]

export default function ManagerPage() {
  const {
    tasks, total, offset, limit, query, statusFilter,
    selectedIds, loading, columns,
    setQuery, setStatusFilter, setOffset, setColumns, fetchTasks,
    toggleSelect, selectAll, clearSelection,
  } = useTaskStore()

  const [executionMode, setExecutionMode] = useState('thread')
  const [maxWorkers, setMaxWorkers] = useState(2)
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const [detailTask, setDetailTask] = useState<Task | null>(null)
  const navigate = useNavigate()

  const hasActive = tasks.some(t => t.status === 'running' || t.status === 'queued')
  usePolling(fetchTasks, hasActive ? 3000 : 10000)

  useEffect(() => { fetchTasks() }, [query, statusFilter, offset])

  const allSelected = tasks.length > 0 && tasks.every(t => selectedIds.has(t.name))

  const handleRunSelected = useCallback(async () => {
    const names = [...selectedIds]
    if (!names.length) return
    await api.batchRunTasks(names, executionMode, maxWorkers)
    clearSelection()
    fetchTasks()
  }, [selectedIds, executionMode, maxWorkers])

  const handleDeleteSelected = useCallback(async () => {
    const names = [...selectedIds]
    if (!names.length) return
    await api.batchDeleteTasks(names)
    clearSelection()
    setDeleteConfirm(false)
    fetchTasks()
  }, [selectedIds])

  const handleTaskAction = useCallback(async (task: Task, action: 'run' | 'cancel' | 'rerun') => {
    if (action === 'run' || action === 'rerun') await api.runTask(task.name, executionMode)
    else if (action === 'cancel') await api.cancelTask(task.name)
    fetchTasks()
  }, [executionMode])

  const handlePin = useCallback(async (task: Task) => {
    await api.pinTask(task.name, !task.pinned)
    fetchTasks()
  }, [])

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Top bar: filters + batch actions */}
      <div className="flex items-center gap-3 px-4 py-2.5 border-b border-border-subtle flex-none bg-surface-raised flex-wrap">
        {/* Status filter */}
        <div className="relative">
          <select
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value)}
            title="Filter by status"
            className="appearance-none bg-surface-overlay border border-border rounded-md pl-3 pr-7 py-1.5 text-xs text-txt-primary outline-none focus:border-accent/50 cursor-pointer"
          >
            {STATUS_OPTIONS.map(s => (
              <option key={s} value={s}>{s === 'All' ? 'All Status' : STATUS_LABELS[s as TaskStatus]}</option>
            ))}
          </select>
          <ChevronDown className="absolute right-2 top-1/2 -translate-y-1/2 w-3 h-3 text-txt-tertiary pointer-events-none" />
        </div>

        {/* Search */}
        <div className="w-52">
          <SearchInput value={query} onChange={setQuery} placeholder="Search tasks..." />
        </div>

        {/* Column selector */}
        <div className="relative">
          <select
            value={columns}
            onChange={e => setColumns(Number(e.target.value))}
            title="Cards per row"
            className="appearance-none bg-surface-overlay border border-border rounded-md pl-2.5 pr-6 py-1.5 text-xs text-txt-primary outline-none focus:border-accent/50 cursor-pointer"
          >
            {[1,2,3,4,5,6,7,8,9].map(n => (
              <option key={n} value={n}>{n} col{n > 1 ? 's' : ''}</option>
            ))}
          </select>
          <ChevronDown className="absolute right-1.5 top-1/2 -translate-y-1/2 w-3 h-3 text-txt-tertiary pointer-events-none" />
        </div>

        <div className="flex-1" />

        {/* Batch controls — always visible */}
        <label className="flex items-center gap-1.5 text-2xs text-txt-secondary">
          Workers
          <input
            type="number" min={1} max={32} value={maxWorkers}
            onChange={e => setMaxWorkers(Math.max(1, +e.target.value))}
            title="Max workers"
            className="w-12 bg-surface-overlay border border-border rounded px-1.5 py-1 text-xs text-txt-primary outline-none focus:border-accent/50 tabular-nums"
          />
        </label>
        <div className="relative">
          <select
            value={executionMode}
            onChange={e => setExecutionMode(e.target.value)}
            title="Execution mode"
            className="appearance-none bg-surface-overlay border border-border rounded-md pl-2.5 pr-6 py-1.5 text-xs text-txt-primary outline-none focus:border-accent/50 cursor-pointer"
          >
            <option value="thread">Thread</option>
            <option value="process">Process</option>
          </select>
          <ChevronDown className="absolute right-1.5 top-1/2 -translate-y-1/2 w-3 h-3 text-txt-tertiary pointer-events-none" />
        </div>

        {/* Select all / clear */}
        <button
          type="button"
          onClick={() => allSelected ? clearSelection() : selectAll()}
          title={allSelected ? 'Deselect all' : 'Select all'}
          className="p-1.5 rounded-md text-txt-secondary hover:text-txt-primary hover:bg-surface-overlay transition-colors"
        >
          {allSelected ? <XSquare className="w-4 h-4" /> : <CheckSquare className="w-4 h-4" />}
        </button>

        {/* Batch run */}
        <button
          type="button"
          onClick={handleRunSelected}
          disabled={selectedIds.size === 0}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-emerald-500/15 text-emerald-400 text-xs font-medium hover:bg-emerald-500/25 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
        >
          <Play className="w-3 h-3" /> Run{selectedIds.size > 0 ? ` (${selectedIds.size})` : ''}
        </button>

        {/* Batch delete */}
        <button
          type="button"
          onClick={() => selectedIds.size > 0 && setDeleteConfirm(true)}
          disabled={selectedIds.size === 0}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-md bg-rose-500/15 text-rose-400 text-xs font-medium hover:bg-rose-500/25 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
        >
          <Trash2 className="w-3 h-3" /> Delete
        </button>

        {/* Refresh */}
        <button
          type="button"
          onClick={() => fetchTasks()}
          title="Refresh"
          className="p-1.5 rounded-md text-txt-secondary hover:text-txt-primary hover:bg-surface-overlay transition-colors"
        >
          <RefreshCw className={clsx('w-3.5 h-3.5', loading && 'animate-spin')} />
        </button>
      </div>

      {/* Card grid */}
      <div className="flex-1 overflow-y-auto p-4">
        {tasks.length === 0 ? (
          <EmptyState title="No tasks found" description={query ? 'Try a different search' : 'Generate some tasks to get started'} />
        ) : (
          <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${columns}, 1fr)` }}>
            {tasks.map(task => (
              <TaskCard
                key={task.name}
                task={task}
                selected={selectedIds.has(task.name)}
                onToggleSelect={() => toggleSelect(task.name)}
                onAction={action => handleTaskAction(task, action)}
                onPin={() => handlePin(task)}
                onDetail={() => setDetailTask(task)}
                onMonitor={() => navigate('/monitor')}
              />
            ))}
          </div>
        )}
      </div>

      {/* Pagination footer */}
      <div className="flex items-center justify-between px-4 py-2 border-t border-border-subtle flex-none bg-surface-raised">
        <span className="text-2xs text-txt-tertiary">
          {selectedIds.size > 0 && `${selectedIds.size} selected · `}
          {total} task{total !== 1 ? 's' : ''}
        </span>
        <Pagination total={total} offset={offset} limit={limit} onOffsetChange={setOffset} />
      </div>

      {/* Delete Confirm */}
      <ConfirmDialog
        open={deleteConfirm}
        title="Delete Tasks"
        description={`Move ${selectedIds.size} task(s) to trash?`}
        confirmLabel="Delete"
        confirmVariant="danger"
        onConfirm={handleDeleteSelected}
        onCancel={() => setDeleteConfirm(false)}
      />

      {/* Detail Panel */}
      {detailTask && (
        <TaskDetailPanel task={detailTask} onClose={() => setDetailTask(null)} onRefresh={fetchTasks} />
      )}
    </div>
  )
}

/* ── Task Card ── */
interface TaskCardProps {
  task: Task
  selected: boolean
  onToggleSelect: () => void
  onAction: (action: 'run' | 'cancel' | 'rerun') => void
  onPin: () => void
  onDetail: () => void
  onMonitor: () => void
}

function TaskCard({ task, selected, onToggleSelect, onAction, onPin, onDetail, onMonitor }: TaskCardProps) {
  const actionBtn = getActionButton(task)
  const sc = STATUS_COLORS[task.status as TaskStatus] || STATUS_COLORS.pending

  return (
    <div
      className={clsx(
        'group relative bg-surface-raised border rounded-lg p-3 transition-all cursor-pointer hover:shadow-md',
        selected ? 'border-accent/40 ring-1 ring-accent/20' : 'border-border-subtle hover:border-border',
        task.pinned && !selected && 'border-amber-500/20',
      )}
      onClick={onDetail}
    >
      {/* Top row: checkbox + status + pin */}
      <div className="flex items-center gap-2 mb-2">
        <input
          type="checkbox"
          checked={selected}
          onChange={e => { e.stopPropagation(); onToggleSelect() }}
          onClick={e => e.stopPropagation()}
          title={`Select ${task.name}`}
          className="w-3.5 h-3.5 rounded border-border accent-accent cursor-pointer flex-none"
        />
        <StatusBadge status={task.status as TaskStatus} />
        <div className="flex-1" />
        {task.pinned && <Pin className="w-3 h-3 text-amber-500 flex-none" />}
        {task.status === 'running' && (
          <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse flex-none" />
        )}
      </div>

      {/* Task name */}
      <div className="text-sm font-medium text-txt-primary truncate mb-1" title={task.name}>
        {task.name}
      </div>

      {/* Config preview */}
      <div className="text-2xs text-txt-secondary font-mono truncate-2 mb-2 min-h-[32px]">
        {task.preview_text || '—'}
      </div>

      {/* Bottom row: time + actions */}
      <div className="flex items-center gap-1">
        <span className="text-2xs text-txt-tertiary font-mono flex-1 truncate">{task.created_at}</span>

        {/* Action buttons — visible on hover */}
        <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity">
          <button
            type="button"
            onClick={e => { e.stopPropagation(); onPin() }}
            title={task.pinned ? 'Unpin' : 'Pin'}
            className={clsx('p-1 rounded hover:bg-surface-hover transition-colors', task.pinned ? 'text-amber-500' : 'text-txt-tertiary')}
          >
            <Pin className="w-3 h-3" />
          </button>
          {actionBtn && (
            <button
              type="button"
              onClick={e => { e.stopPropagation(); onAction(actionBtn.action) }}
              title={actionBtn.label}
              className={clsx('p-1 rounded transition-colors', actionBtn.className)}
            >
              <actionBtn.icon className="w-3.5 h-3.5" />
            </button>
          )}
          <button
            type="button"
            onClick={e => { e.stopPropagation(); onMonitor() }}
            title="View logs"
            className="p-1 rounded text-txt-tertiary hover:text-txt-primary hover:bg-surface-hover transition-colors"
          >
            <Terminal className="w-3 h-3" />
          </button>
          <button
            type="button"
            onClick={e => { e.stopPropagation(); onDetail() }}
            title="Details"
            className="p-1 rounded text-txt-tertiary hover:text-txt-primary hover:bg-surface-hover transition-colors"
          >
            <MoreHorizontal className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>

      {/* Status accent bar at top */}
      <div className={clsx('absolute top-0 left-3 right-3 h-0.5 rounded-b', sc.dot)} />
    </div>
  )
}

function getActionButton(task: Task) {
  switch (task.status) {
    case 'pending':
    case 'failed':
      return { action: 'run' as const, icon: Play, label: 'Run', className: 'text-emerald-500 hover:bg-emerald-500/15' }
    case 'running':
    case 'queued':
      return { action: 'cancel' as const, icon: Square, label: 'Stop', className: 'text-rose-500 hover:bg-rose-500/15' }
    case 'completed':
      return { action: 'rerun' as const, icon: RotateCcw, label: 'Rerun', className: 'text-blue-400 hover:bg-blue-500/15' }
    default:
      return null
  }
}
