import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle, ChevronDown, MousePointer2, Pin, Play, RotateCcw, Rows3, Square, Terminal, Trash2,
} from 'lucide-react'
import clsx from 'clsx'
import { useMonitorStore, useTaskStore } from '@/store'
import { usePolling } from '@/hooks/usePolling'
import StatusBadge from '@/components/shared/StatusBadge'
import SearchInput from '@/components/shared/SearchInput'
import SelectionIndicator from '@/components/shared/SelectionIndicator'
import Pagination from '@/components/shared/Pagination'
import EmptyState from '@/components/shared/EmptyState'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import ActionButton from '@/components/shared/ActionButton'
import CompactSection from '@/components/shared/CompactSection'
import InlineMetric from '@/components/shared/InlineMetric'
import TaskDetailPanel from './TaskDetailPanel'
import type { Task } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import { ALL_STATUSES, STATUS_LABELS } from '@/theme/tokens'
import * as api from '@/api'

const STATUS_OPTIONS = ['All', ...ALL_STATUSES]

export default function ManagerPage() {
  const {
    tasks, total, offset, limit, query, statusFilter, selectedIds, columns,
    setQuery, setStatusFilter, setOffset, setColumns, fetchTasks,
    toggleSelect, selectAll, clearSelection,
  } = useTaskStore()

  const [executionMode, setExecutionMode] = useState('thread')
  const [maxWorkersInput, setMaxWorkersInput] = useState('2')
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const [deleteTask, setDeleteTask] = useState<Task | null>(null)
  const [detailTask, setDetailTask] = useState<Task | null>(null)
  const [selectMode, setSelectMode] = useState(false)
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const focusTaskName = searchParams.get('task')

  const hasActive = tasks.some(task => task.status === 'running' || task.status === 'queued')
  usePolling(fetchTasks, hasActive ? 3000 : 10000, true, false)

  useEffect(() => {
    void fetchTasks()
  }, [query, statusFilter, offset, fetchTasks])

  useEffect(() => {
    if (!focusTaskName) {
      return
    }

    let cancelled = false
    void api.getTask(focusTaskName).then(task => {
      if (!cancelled) {
        setDetailTask(task)
      }
    }).catch(() => {
      if (!cancelled) {
        const next = new URLSearchParams(searchParams)
        next.delete('task')
        setSearchParams(next, { replace: true })
      }
    })

    return () => {
      cancelled = true
    }
  }, [focusTaskName, searchParams, setSearchParams])

  const allSelected = tasks.length > 0 && tasks.every(task => selectedIds.has(task.name))
  const pinnedTasks = useMemo(() => tasks.filter(task => task.pinned), [tasks])
  const otherTasks = useMemo(() => tasks.filter(task => !task.pinned), [tasks])
  const summary = useMemo(() => ({
    total,
    active: tasks.filter(task => task.status === 'running' || task.status === 'queued').length,
    completed: tasks.filter(task => task.status === 'completed').length,
    failed: tasks.filter(task => task.status === 'failed').length,
  }), [tasks, total])

  const normalizeWorkerInput = useCallback((value: string) => {
    const trimmed = value.trim()
    if (!trimmed) {
      return 1
    }
    const parsed = Number.parseInt(trimmed, 10)
    if (!Number.isFinite(parsed)) {
      return 1
    }
    return Math.min(32, Math.max(1, parsed))
  }, [])

  const handleRunSelected = useCallback(async () => {
    const names = [...selectedIds]
    if (!names.length) return
    const maxWorkers = normalizeWorkerInput(maxWorkersInput)
    setMaxWorkersInput(String(maxWorkers))
    await api.batchRunTasks(names, executionMode, maxWorkers)
    clearSelection()
    setSelectMode(false)
    await fetchTasks()
  }, [selectedIds, executionMode, maxWorkersInput, normalizeWorkerInput, clearSelection, fetchTasks])

  const handleDeleteSelected = useCallback(async () => {
    const names = [...selectedIds]
    if (!names.length) return
    await api.batchDeleteTasks(names)
    clearSelection()
    setDeleteConfirm(false)
    setSelectMode(false)
    await fetchTasks()
  }, [selectedIds, clearSelection, fetchTasks])

  const handleDeleteTask = useCallback(async () => {
    if (!deleteTask) return
    await api.batchDeleteTasks([deleteTask.name])
    if (detailTask?.name === deleteTask.name) {
      setDetailTask(null)
    }
    setDeleteTask(null)
    await fetchTasks()
  }, [deleteTask, detailTask, fetchTasks])

  const handleTaskAction = useCallback(async (task: Task, action: 'run' | 'cancel' | 'rerun') => {
    if (action === 'run' || action === 'rerun') {
      await api.runTask(task.name, executionMode)
    } else {
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

  const closeDetailPanel = useCallback(() => {
    setDetailTask(null)
    if (!searchParams.get('task')) {
      return
    }
    const next = new URLSearchParams(searchParams)
    next.delete('task')
    setSearchParams(next, { replace: true })
  }, [searchParams, setSearchParams])

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-border-subtle bg-surface-raised px-3 py-2">
        <div className="relative">
          <select
            value={statusFilter}
            onChange={event => setStatusFilter(event.target.value)}
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

        <div className="w-64 max-w-full">
          <SearchInput value={query} onChange={setQuery} placeholder="Search tasks..." />
        </div>

        <div className="relative">
          <select
            value={columns}
            onChange={event => setColumns(Number(event.target.value))}
            title="Cards per row"
            className="appearance-none rounded-md border border-border-subtle bg-surface-overlay px-2.5 py-1.5 pr-6 text-xs text-txt-primary outline-none transition-colors focus:border-border"
          >
            {[1, 2, 3, 4, 5, 6, 7, 8].map(count => (
              <option key={count} value={count}>{count} col{count > 1 ? 's' : ''}</option>
            ))}
          </select>
          <ChevronDown className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
        </div>

        <div className="flex-1" />

        <div className="flex flex-wrap items-center gap-1.5">
          <InlineMetric label="Total" value={summary.total} />
          <InlineMetric label="Active" value={summary.active} tone="amber" />
          <InlineMetric label="Done" value={summary.completed} tone="emerald" />
          <InlineMetric label="Failed" value={summary.failed} tone="rose" />
        </div>

        {!selectMode ? (
          <ActionButton
            icon={<MousePointer2 className="h-4 w-4" />}
            variant="primary"
            size="md"
            onClick={() => setSelectMode(true)}
          >
            Select
          </ActionButton>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <ActionButton
              icon={<SelectionIndicator selected={allSelected} />}
              variant="accentTint"
              onClick={() => (allSelected ? clearSelection() : selectAll())}
            >
              {allSelected ? 'Deselect All' : 'Select All'}
            </ActionButton>

            <label className="flex items-center gap-1.5 text-2xs text-txt-secondary">
              Workers
              <input
                type="number"
                min={1}
                max={32}
                value={maxWorkersInput}
                onChange={event => setMaxWorkersInput(event.target.value)}
                onBlur={() => setMaxWorkersInput(current => String(normalizeWorkerInput(current)))}
                title="Max workers"
                className="w-12 rounded-md border border-border-subtle bg-surface-overlay px-1.5 py-1 text-xs tabular-nums text-txt-primary outline-none transition-colors focus:border-border"
              />
            </label>

            <div className="relative">
              <select
                value={executionMode}
                onChange={event => setExecutionMode(event.target.value)}
                title="Execution mode"
                className="appearance-none rounded-md border border-border-subtle bg-surface-overlay px-2.5 py-1.5 pr-6 text-xs text-txt-primary outline-none transition-colors focus:border-border"
              >
                <option value="thread">Thread</option>
                <option value="process">Process</option>
              </select>
              <ChevronDown className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
            </div>

            <ActionButton
              icon={<Play className="h-3.5 w-3.5" />}
              variant="success"
              onClick={handleRunSelected}
              disabled={selectedIds.size === 0}
            >
              Run ({selectedIds.size})
            </ActionButton>

            <ActionButton
              icon={<Trash2 className="h-3.5 w-3.5" />}
              variant="danger"
              onClick={() => selectedIds.size > 0 && setDeleteConfirm(true)}
              disabled={selectedIds.size === 0}
            >
              Delete
            </ActionButton>

            <ActionButton variant="ghost" onClick={exitSelectMode}>
              Cancel
            </ActionButton>
          </div>
        )}
      </div>

      <div className="flex-1 overflow-y-auto p-3">
        {tasks.length === 0 ? (
          <EmptyState
            title="No tasks found"
            description={query ? 'Try a different search' : 'Generate some tasks to get started'}
          />
        ) : (
          <div className="space-y-3">
            {pinnedTasks.length > 0 && (
              <CompactSection
                title="Pinned"
                subtitle={`${pinnedTasks.length} pinned task${pinnedTasks.length > 1 ? 's' : ''}`}
                icon={<Pin className="h-3.5 w-3.5 text-accent" />}
                accent
                bodyClassName="p-2"
              >
                <TaskGrid
                  tasks={pinnedTasks}
                  columns={columns}
                  selectedIds={selectedIds}
                  selectMode={selectMode}
                  onCardClick={handleCardClick}
                  onTaskAction={handleTaskAction}
                  onPin={handlePin}
                  onDelete={setDeleteTask}
                  onMonitor={task => {
                    void useMonitorStore.getState().selectTask(task.name)
                    navigate('/monitor')
                  }}
                />
              </CompactSection>
            )}

            <CompactSection
              title="Tasks"
              subtitle={`${otherTasks.length} task${otherTasks.length > 1 ? 's' : ''}`}
              icon={<Rows3 className="h-3.5 w-3.5 text-txt-tertiary" />}
              bodyClassName="p-2"
            >
              <TaskGrid
                tasks={otherTasks}
                columns={columns}
                selectedIds={selectedIds}
                selectMode={selectMode}
                onCardClick={handleCardClick}
                onTaskAction={handleTaskAction}
                onPin={handlePin}
                onDelete={setDeleteTask}
                onMonitor={task => {
                  void useMonitorStore.getState().selectTask(task.name)
                  navigate('/monitor')
                }}
              />
            </CompactSection>
          </div>
        )}
      </div>

      <div className="flex items-center justify-between border-t border-border-subtle bg-surface-raised px-3 py-1.5">
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

      <ConfirmDialog
        open={Boolean(deleteTask)}
        title="Delete Task"
        description={deleteTask ? `Move '${deleteTask.name}' to trash?` : ''}
        confirmLabel="Delete"
        confirmVariant="danger"
        onConfirm={handleDeleteTask}
        onCancel={() => setDeleteTask(null)}
      />

      {detailTask && (
        <TaskDetailPanel task={detailTask} onClose={closeDetailPanel} onRefresh={fetchTasks} />
      )}
    </div>
  )
}

function TaskGrid({
  tasks,
  columns,
  selectedIds,
  selectMode,
  onCardClick,
  onTaskAction,
  onPin,
  onDelete,
  onMonitor,
}: {
  tasks: Task[]
  columns: number
  selectedIds: Set<string>
  selectMode: boolean
  onCardClick: (task: Task) => void
  onTaskAction: (task: Task, action: 'run' | 'cancel' | 'rerun') => void | Promise<void>
  onPin: (task: Task) => void | Promise<void>
  onDelete: (task: Task) => void
  onMonitor: (task: Task) => void
}) {
  if (tasks.length === 0) {
    return <div className="px-2 py-5 text-center text-2xs text-txt-tertiary">No tasks in this section</div>
  }

  return (
    <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
      {tasks.map(task => (
        <TaskCard
          key={task.name}
          task={task}
          selected={selectedIds.has(task.name)}
          selectMode={selectMode}
          onClick={() => onCardClick(task)}
          onAction={action => void onTaskAction(task, action)}
          onPin={() => void onPin(task)}
          onDelete={() => onDelete(task)}
          onMonitor={() => onMonitor(task)}
        />
      ))}
    </div>
  )
}

function TaskCard({
  task,
  selected,
  selectMode,
  onClick,
  onAction,
  onPin,
  onDelete,
  onMonitor,
}: {
  task: Task
  selected: boolean
  selectMode: boolean
  onClick: () => void
  onAction: (action: 'run' | 'cancel' | 'rerun') => void
  onPin: () => void
  onDelete: () => void
  onMonitor: () => void
}) {
  const actionBtn = getActionButton(task)
  const folderName = task.dir.split(/[\\/]/).pop() || task.dir
  const taskKindLabel = (task.config_mode || task.task_kind) === 'shell' ? 'shell' : 'config'
  const cardDescription = task._load_error || task.preview_text || 'No preview available.'

  return (
    <div
      className={clsx(
        'group relative cursor-pointer rounded-md border bg-surface-raised px-3 py-2.5 transition-colors',
        selected ? 'border-accent bg-accent/8 ring-1 ring-accent/20' : 'border-border-subtle hover:border-border',
        task.pinned && !selected && 'border-accent/20',
      )}
      onClick={onClick}
    >
      {selectMode ? (
        <div className="absolute right-2.5 top-2.5">
          <SelectionIndicator selected={selected} />
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
            'absolute right-2.5 top-2.5 rounded-md p-1 transition-colors hover:bg-surface-overlay',
            task.pinned ? 'text-accent' : 'text-txt-tertiary hover:text-accent'
          )}
        >
          <Pin className="h-3.5 w-3.5" />
        </button>
      )}

      <div className="flex items-center gap-2 pr-7">
        <StatusBadge status={task.status as TaskStatus} />
        <span className="text-2xs uppercase tracking-[0.16em] text-txt-tertiary">
          {taskKindLabel}
        </span>
      </div>

      <div className="mt-1.5 pr-7 text-sm font-medium text-txt-primary" title={task.name}>
        {task.name}
      </div>

      <div className="mt-1 min-h-[30px] text-2xs leading-5 text-txt-secondary" title={cardDescription}>
        <div className={clsx('truncate-2', task._load_error && 'text-rose-400')}>
          {cardDescription}
        </div>
      </div>

      <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-txt-tertiary">
        <span title={task.created_at}>{task.created_at}</span>
        <span title={`Run #${Math.max(task.run_index || 1, 1)}`}>Run #{Math.max(task.run_index || 1, 1)}</span>
        <span className="truncate" title={folderName}>{folderName}</span>
      </div>

      {task._load_error && (
        <div className="mt-2 inline-flex max-w-full items-center gap-1 rounded-md bg-rose-500/10 px-2 py-1 text-2xs text-rose-400" title={task._load_error}>
          <AlertTriangle className="h-3 w-3" />
          <span className="truncate">Task load error</span>
        </div>
      )}

      {!selectMode && (
        <div className="mt-2.5 flex items-center justify-end gap-1 border-t border-border-subtle pt-1.5">
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
          <button
            type="button"
            onClick={event => {
              event.stopPropagation()
              onDelete()
            }}
            title="Delete task"
            className="rounded-md p-1.5 text-rose-400 transition-colors hover:bg-rose-500/10 hover:text-rose-300"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
    </div>
  )
}

function getActionButton(task: Task) {
  if (task._load_error) {
    return null
  }

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
