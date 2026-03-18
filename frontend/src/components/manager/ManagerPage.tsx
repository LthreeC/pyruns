import { useEffect, useMemo, useState, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Play, Square, RotateCcw, Pin, Trash2, ChevronDown,
  Terminal, MousePointer2, CheckCheck, X,
} from 'lucide-react'
import clsx from 'clsx'
import { useMonitorStore, useTaskStore } from '@/store'
import { usePolling } from '@/hooks/usePolling'
import StatusBadge from '@/components/shared/StatusBadge'
import SearchInput from '@/components/shared/SearchInput'
import Pagination from '@/components/shared/Pagination'
import EmptyState from '@/components/shared/EmptyState'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import TaskDetailPanel from './TaskDetailPanel'
import type { Task } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import { ALL_STATUSES, STATUS_LABELS } from '@/theme/tokens'
import * as api from '@/api'

const STATUS_OPTIONS = ['All', ...ALL_STATUSES]

export default function ManagerPage() {
  const {
    tasks, total, offset, limit, query, statusFilter,
    selectedIds, columns,
    setQuery, setStatusFilter, setOffset, setColumns, fetchTasks,
    toggleSelect, selectAll, clearSelection,
  } = useTaskStore()

  const [executionMode, setExecutionMode] = useState('thread')
  const [maxWorkers, setMaxWorkers] = useState(2)
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const [detailTask, setDetailTask] = useState<Task | null>(null)
  const [selectMode, setSelectMode] = useState(false)
  const navigate = useNavigate()

  const hasActive = tasks.some(t => t.status === 'running' || t.status === 'queued')
  usePolling(fetchTasks, hasActive ? 3000 : 10000, true, false)

  useEffect(() => {
    void fetchTasks()
  }, [query, statusFilter, offset, fetchTasks])

  const allSelected = tasks.length > 0 && tasks.every(t => selectedIds.has(t.name))
  const summary = useMemo(() => ({
    total,
    active: tasks.filter(t => t.status === 'running' || t.status === 'queued').length,
    completed: tasks.filter(t => t.status === 'completed').length,
    failed: tasks.filter(t => t.status === 'failed').length,
    selected: selectedIds.size,
  }), [tasks, total, selectedIds])

  const handleRunSelected = useCallback(async () => {
    const names = [...selectedIds]
    if (!names.length) return
    await api.batchRunTasks(names, executionMode, maxWorkers)
    clearSelection()
    setSelectMode(false)
    await fetchTasks()
  }, [selectedIds, executionMode, maxWorkers, clearSelection, fetchTasks])

  const handleDeleteSelected = useCallback(async () => {
    const names = [...selectedIds]
    if (!names.length) return
    await api.batchDeleteTasks(names)
    clearSelection()
    setDeleteConfirm(false)
    setSelectMode(false)
    await fetchTasks()
  }, [selectedIds, clearSelection, fetchTasks])

  const handleTaskAction = useCallback(async (task: Task, action: 'run' | 'cancel' | 'rerun') => {
    if (action === 'run' || action === 'rerun') {
      await api.runTask(task.name, executionMode)
    } else if (action === 'cancel') {
      await api.cancelTask(task.name)
    }
    await fetchTasks()
  }, [executionMode, fetchTasks])

  const handlePin = useCallback(async (task: Task) => {
    await api.pinTask(task.name, !task.pinned)
    await fetchTasks()
  }, [fetchTasks])

  const exitSelectMode = () => {
    setSelectMode(false)
    clearSelection()
  }

  const handleCardClick = (task: Task) => {
    if (selectMode) {
      toggleSelect(task.name)
      return
    }
    setDetailTask(task)
  }

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex flex-wrap items-center gap-2.5 border-b border-border-subtle bg-surface-raised px-4 py-2.5">
        <div className="relative">
          <select
            value={statusFilter}
            onChange={e => setStatusFilter(e.target.value)}
            title="Filter by status"
            className="appearance-none rounded-md border border-border-subtle bg-surface-overlay px-3 py-1.5 pr-7 text-xs text-txt-primary outline-none transition-colors focus:border-border"
          >
            {STATUS_OPTIONS.map(option => (
              <option key={option} value={option}>
                {option === 'All' ? 'All Status' : STATUS_LABELS[option as TaskStatus]}
              </option>
            ))}
          </select>
          <ChevronDown className="pointer-events-none absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
        </div>

        <div className="w-48">
          <SearchInput value={query} onChange={setQuery} placeholder="Search tasks..." />
        </div>

        <div className="relative">
          <select
            value={columns}
            onChange={e => setColumns(Number(e.target.value))}
            title="Cards per row"
            className="appearance-none rounded-md border border-border-subtle bg-surface-overlay px-2.5 py-1.5 pr-6 text-xs text-txt-primary outline-none transition-colors focus:border-border"
          >
            {[1, 2, 3, 4, 5, 6, 7, 8, 9].map(count => (
              <option key={count} value={count}>{count} col{count > 1 ? 's' : ''}</option>
            ))}
          </select>
          <ChevronDown className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
        </div>

        <div className="flex-1" />

        {!selectMode ? (
          <button
            type="button"
            onClick={() => setSelectMode(true)}
            className="flex items-center gap-1.5 rounded-md border border-border-subtle px-3 py-1.5 text-xs text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
            title="Enter select mode"
          >
            <MousePointer2 className="h-3.5 w-3.5" />
            <span>Select</span>
          </button>
        ) : (
          <>
            <button
              type="button"
              onClick={() => (allSelected ? clearSelection() : selectAll())}
              className="flex items-center gap-1.5 rounded-md border border-border-subtle px-3 py-1.5 text-xs text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
              title={allSelected ? 'Deselect all' : 'Select all'}
            >
              <CheckCheck className="h-3.5 w-3.5" />
              <span>{allSelected ? 'Deselect All' : 'Select All'}</span>
            </button>

            <label className="flex items-center gap-1.5 text-2xs text-txt-secondary">
              Workers
              <input
                type="number"
                min={1}
                max={32}
                value={maxWorkers}
                onChange={e => setMaxWorkers(Math.max(1, +e.target.value))}
                title="Max workers"
                className="w-12 rounded-md border border-border-subtle bg-surface-overlay px-1.5 py-1 text-xs tabular-nums text-txt-primary outline-none focus:border-border"
              />
            </label>

            <div className="relative">
              <select
                value={executionMode}
                onChange={e => setExecutionMode(e.target.value)}
                title="Execution mode"
                className="appearance-none rounded-md border border-border-subtle bg-surface-overlay px-2.5 py-1.5 pr-6 text-xs text-txt-primary outline-none transition-colors focus:border-border"
              >
                <option value="thread">Thread</option>
                <option value="process">Process</option>
              </select>
              <ChevronDown className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
            </div>

            <button
              type="button"
              onClick={handleRunSelected}
              disabled={selectedIds.size === 0}
              className="flex items-center gap-1.5 rounded-md border border-emerald-500/20 px-3 py-1.5 text-xs font-medium text-emerald-400 transition-colors hover:bg-emerald-500/10 disabled:cursor-not-allowed disabled:opacity-30"
            >
              <Play className="h-3.5 w-3.5" />
              <span>Run{selectedIds.size > 0 ? ` (${selectedIds.size})` : ''}</span>
            </button>

            <button
              type="button"
              onClick={() => selectedIds.size > 0 && setDeleteConfirm(true)}
              disabled={selectedIds.size === 0}
              className="flex items-center gap-1.5 rounded-md border border-rose-500/20 px-3 py-1.5 text-xs font-medium text-rose-400 transition-colors hover:bg-rose-500/10 disabled:cursor-not-allowed disabled:opacity-30"
            >
              <Trash2 className="h-3.5 w-3.5" />
              <span>Delete</span>
            </button>

            <button
              type="button"
              onClick={exitSelectMode}
              className="rounded-md p-1.5 text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
              title="Exit select mode"
            >
              <X className="h-4 w-4" />
            </button>
          </>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border-subtle bg-surface-raised px-4 py-2 text-2xs text-txt-secondary">
        <InlineStat label="Total" value={summary.total} />
        <InlineStat label="Active" value={summary.active} />
        <InlineStat label="Completed" value={summary.completed} />
        <InlineStat label={selectMode ? 'Selected' : 'Failed'} value={selectMode ? summary.selected : summary.failed} />
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {tasks.length === 0 ? (
          <EmptyState
            title="No tasks found"
            description={query ? 'Try a different search' : 'Generate some tasks to get started'}
          />
        ) : (
          <div className="grid gap-3" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
            {tasks.map(task => (
              <TaskCard
                key={task.name}
                task={task}
                selected={selectedIds.has(task.name)}
                selectMode={selectMode}
                onClick={() => handleCardClick(task)}
                onAction={action => void handleTaskAction(task, action)}
                onPin={() => void handlePin(task)}
                onMonitor={() => {
                  void useMonitorStore.getState().selectTask(task.name)
                  navigate('/monitor')
                }}
              />
            ))}
          </div>
        )}
      </div>

      <div className="flex items-center justify-between border-t border-border-subtle bg-surface-raised px-4 py-2">
        <span className="text-2xs text-txt-tertiary">
          {selectMode && selectedIds.size > 0 ? `${selectedIds.size} selected | ` : ''}
          {total} task{total !== 1 ? 's' : ''}
        </span>
        <Pagination total={total} offset={offset} limit={limit} onOffsetChange={setOffset} />
      </div>

      <ConfirmDialog
        open={deleteConfirm}
        title="Delete Tasks"
        description={`Move ${selectedIds.size} task(s) to trash?`}
        confirmLabel="Delete"
        confirmVariant="danger"
        onConfirm={handleDeleteSelected}
        onCancel={() => setDeleteConfirm(false)}
      />

      {detailTask && (
        <TaskDetailPanel task={detailTask} onClose={() => setDetailTask(null)} onRefresh={fetchTasks} />
      )}
    </div>
  )
}

function InlineStat({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center gap-2">
      <span className="uppercase tracking-[0.16em] text-txt-tertiary">{label}</span>
      <span className="text-xs font-semibold tabular-nums text-txt-primary">{value}</span>
    </div>
  )
}

interface TaskCardProps {
  task: Task
  selected: boolean
  selectMode: boolean
  onClick: () => void
  onAction: (action: 'run' | 'cancel' | 'rerun') => void
  onPin: () => void
  onMonitor: () => void
}

function TaskCard({ task, selected, selectMode, onClick, onAction, onPin, onMonitor }: TaskCardProps) {
  const actionBtn = getActionButton(task)
  const runMode = task.run_mode || 'standard'
  const runCount = Math.max(task.run_index || 1, 1)
  const folderName = task.dir.split(/[\\/]/).pop() || task.dir

  return (
    <div
      className={clsx(
        'group relative cursor-pointer rounded-lg border bg-surface-raised px-4 py-3 transition-colors',
        selected ? 'border-accent bg-surface-overlay/70' : 'border-border-subtle hover:border-border',
        task.pinned && !selected && 'border-amber-500/20',
      )}
      onClick={onClick}
    >
      {selectMode ? (
        <div
          className={clsx(
            'absolute right-3 top-3 flex h-5 w-5 items-center justify-center rounded-full border text-[10px] font-semibold transition-colors',
            selected ? 'border-accent bg-accent text-white' : 'border-border bg-surface-overlay text-transparent'
          )}
        >
          x
        </div>
      ) : (
        <button
          type="button"
          onClick={event => {
            event.stopPropagation()
            onPin()
          }}
          title={task.pinned ? 'Unpin' : 'Pin'}
          className={clsx(
            'absolute right-3 top-3 rounded-md p-1 text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary',
            task.pinned && 'text-amber-500'
          )}
        >
          <Pin className="h-3.5 w-3.5" />
        </button>
      )}

      <div className="flex items-center gap-2 pr-8">
        <StatusBadge status={task.status as TaskStatus} />
        <span className="text-2xs uppercase tracking-[0.16em] text-txt-tertiary">{runMode}</span>
      </div>

      <div className="mt-2 pr-8 text-sm font-medium text-txt-primary" title={task.name}>
        {task.name}
      </div>

      <div className="mt-1 min-h-[34px] text-2xs leading-5 text-txt-secondary" title={task.preview_text || 'No preview available.'}>
        <div className="truncate-2">
          {task.preview_text || 'No preview available.'}
        </div>
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-txt-tertiary">
        <span>{task.created_at}</span>
        <span>Run #{runCount}</span>
        <span className="truncate" title={folderName}>{folderName}</span>
      </div>

      {!selectMode && (
        <div className="mt-3 flex items-center justify-end gap-1 border-t border-border-subtle pt-2">
          {actionBtn && (
            <button
              type="button"
              onClick={event => {
                event.stopPropagation()
                onAction(actionBtn.action)
              }}
              title={actionBtn.label}
              className={clsx('rounded-md p-1.5 transition-colors', actionBtn.className)}
            >
              <actionBtn.icon className="h-3.5 w-3.5" />
            </button>
          )}
          <button
            type="button"
            onClick={event => {
              event.stopPropagation()
              onMonitor()
            }}
            title="View logs"
            className="rounded-md p-1.5 text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
          >
            <Terminal className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
    </div>
  )
}

function getActionButton(task: Task) {
  switch (task.status) {
    case 'pending':
    case 'failed':
      return {
        action: 'run' as const,
        icon: Play,
        label: 'Run',
        className: 'text-emerald-400 hover:bg-emerald-500/10',
      }
    case 'running':
    case 'queued':
      return {
        action: 'cancel' as const,
        icon: Square,
        label: 'Stop',
        className: 'text-rose-400 hover:bg-rose-500/10',
      }
    case 'completed':
      return {
        action: 'rerun' as const,
        icon: RotateCcw,
        label: 'Rerun',
        className: 'text-blue-400 hover:bg-blue-500/10',
      }
    default:
      return null
  }
}
