import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle, ChevronDown, GripVertical, MousePointer2, Pin, Play, RotateCcw, Rows3, Search, Square, Terminal, Trash2,
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
type DragTarget = 'pinned' | 'tasks'
type DragPlacement = 'before' | 'after'
const DRAG_START_DISTANCE = 8

function readCompactTaskGrid() {
  if (typeof window === 'undefined') {
    return false
  }
  return window.matchMedia('(max-width: 700px)').matches
}

interface TaskSearchMatch {
  label: string
  detail: string
}

interface DropIntent {
  target: DragTarget
  targetName: string
  placement: DragPlacement
  axis: 'horizontal' | 'vertical'
}

interface DragCandidate {
  taskName: string
  startX: number
  startY: number
}

function isInteractiveDragTarget(target: EventTarget | null) {
  return target instanceof HTMLElement
    && Boolean(target.closest('button, a, input, textarea, select, [role="button"]'))
}

function getDropTargetFromElement(element: HTMLElement | null): DragTarget | null {
  const target = element?.closest<HTMLElement>('[data-task-drop-target]')
  const value = target?.dataset.taskDropTarget
  return value === 'pinned' || value === 'tasks' ? value : null
}

function getCardDropPlacement(card: HTMLElement, clientX: number, clientY: number): Pick<DropIntent, 'placement' | 'axis'> {
  const rect = card.getBoundingClientRect()
  const grid = card.closest<HTMLElement>('[data-task-grid]')
  const columnCount = Number.parseInt(grid?.dataset.taskGridColumns || '1', 10)
  const axis = columnCount <= 1 ? 'vertical' : 'horizontal'
  const placement = axis === 'vertical'
    ? (clientY < rect.top + rect.height / 2 ? 'before' : 'after')
    : (clientX < rect.left + rect.width / 2 ? 'before' : 'after')
  return { placement, axis }
}

function getPointerDropIntent(clientX: number, clientY: number): DropIntent | null {
  const element = document.elementFromPoint(clientX, clientY)
  if (!(element instanceof HTMLElement)) {
    return null
  }

  const card = element.closest<HTMLElement>('[data-task-card]')
  if (card) {
    const target = getDropTargetFromElement(card)
    if (target) {
      return {
        target,
        targetName: card.dataset.taskCard || '',
        ...getCardDropPlacement(card, clientX, clientY),
      }
    }
  }

  const target = getDropTargetFromElement(element)
  return target ? { target, targetName: '', placement: 'after', axis: 'vertical' } : null
}

function sameDropIntent(left: DropIntent | null, right: DropIntent | null) {
  return left?.target === right?.target
    && left?.targetName === right?.targetName
    && left?.placement === right?.placement
    && left?.axis === right?.axis
}

function buildReorderedItems(tasks: Task[], taskName: string, intent: DropIntent) {
  const dragged = tasks.find(task => task.name === taskName)
  if (!dragged) {
    return []
  }

  if (intent.targetName === taskName && intent.target === (dragged.pinned ? 'pinned' : 'tasks')) {
    return tasks.map(task => ({ name: task.name, pinned: Boolean(task.pinned) }))
  }

  const namesByTarget: Record<DragTarget, string[]> = {
    pinned: [],
    tasks: [],
  }

  tasks.forEach(task => {
    if (task.name === taskName) {
      return
    }
    namesByTarget[task.pinned ? 'pinned' : 'tasks'].push(task.name)
  })

  const targetNames = namesByTarget[intent.target]
  const targetIndex = intent.targetName ? targetNames.indexOf(intent.targetName) : -1
  const insertIndex = targetIndex >= 0
    ? targetIndex + (intent.placement === 'after' ? 1 : 0)
    : targetNames.length
  targetNames.splice(insertIndex, 0, taskName)

  return [
    ...namesByTarget.pinned.map(name => ({ name, pinned: true })),
    ...namesByTarget.tasks.map(name => ({ name, pinned: false })),
  ]
}

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
  const [draggedTaskName, setDraggedTaskName] = useState('')
  const [dragOverTarget, setDragOverTarget] = useState<DragTarget | null>(null)
  const [dropIntent, setDropIntent] = useState<DropIntent | null>(null)
  const [taskActionMessage, setTaskActionMessage] = useState('')
  const dragCandidateRef = useRef<DragCandidate | null>(null)
  const draggedTaskNameRef = useRef('')
  const dragOverTargetRef = useRef<DragTarget | null>(null)
  const dropIntentRef = useRef<DropIntent | null>(null)
  const pendingDragPointRef = useRef<{ clientX: number; clientY: number } | null>(null)
  const dragFrameRef = useRef<number | null>(null)
  const suppressCardClickRef = useRef('')
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const focusTaskName = searchParams.get('task')
  const [compactTaskGrid, setCompactTaskGrid] = useState(readCompactTaskGrid)

  const hasActive = tasks.some(task => task.status === 'running' || task.status === 'queued')
  const effectiveTaskColumns = compactTaskGrid ? 1 : columns
  usePolling(fetchTasks, hasActive ? 3000 : 10000, true, false)

  useEffect(() => {
    void fetchTasks()
  }, [query, statusFilter, offset, fetchTasks])

  useEffect(() => {
    if (typeof window === 'undefined') {
      return
    }

    const query = window.matchMedia('(max-width: 700px)')
    const handleChange = () => setCompactTaskGrid(query.matches)
    handleChange()
    query.addEventListener('change', handleChange)
    return () => query.removeEventListener('change', handleChange)
  }, [])

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
  const draggedTask = useMemo(
    () => tasks.find(task => task.name === draggedTaskName) || null,
    [draggedTaskName, tasks],
  )
  const showPinnedSection = pinnedTasks.length > 0 || Boolean(draggedTask && !draggedTask.pinned)
  const summary = useMemo(() => ({
    total,
    active: tasks.filter(task => task.status === 'running' || task.status === 'queued').length,
    completed: tasks.filter(task => task.status === 'completed').length,
    failed: tasks.filter(task => task.status === 'failed').length,
  }), [tasks, total])

  draggedTaskNameRef.current = draggedTaskName
  dragOverTargetRef.current = dragOverTarget
  dropIntentRef.current = dropIntent

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
    setTaskActionMessage(task.pinned ? `Moved ${task.name} back to Tasks.` : `Pinned ${task.name}.`)
    await fetchTasks()
  }, [fetchTasks])

  const handleTaskPointerDown = useCallback((task: Task, event: ReactPointerEvent<HTMLElement>) => {
    if (selectMode || event.button !== 0 || isInteractiveDragTarget(event.target)) {
      return
    }

    event.preventDefault()
    dragCandidateRef.current = {
      taskName: task.name,
      startX: event.clientX,
      startY: event.clientY,
    }
    setDropIntent(null)
    dropIntentRef.current = null
    setTaskActionMessage('')
  }, [selectMode])

  const handleTaskDrop = useCallback(async (intent: DropIntent, taskName = draggedTaskNameRef.current) => {
    const task = tasks.find(item => item.name === taskName)
    setDragOverTarget(null)
    setDropIntent(null)
    setDraggedTaskName('')
    draggedTaskNameRef.current = ''
    dropIntentRef.current = null

    if (!task) {
      return
    }

    const allTasks = await api.getTasks({ limit: 0, refresh: false, summary: true })
    const items = buildReorderedItems(allTasks.items, task.name, intent)
    const movedItem = items.find(item => item.name === task.name)
    if (!items.length || !movedItem) {
      return
    }

    await api.reorderTasks(items)
    if (movedItem.pinned !== task.pinned) {
      setTaskActionMessage(movedItem.pinned ? `Pinned ${task.name}.` : `Moved ${task.name} back to Tasks.`)
    } else {
      setTaskActionMessage(`Moved ${task.name}.`)
    }
    await fetchTasks()
  }, [fetchTasks, tasks])

  useEffect(() => {
    const applyDropIntent = (intent: DropIntent | null) => {
      const previousIntent = dropIntentRef.current
      dropIntentRef.current = intent
      dragOverTargetRef.current = intent?.target ?? null
      if (!sameDropIntent(previousIntent, intent)) {
        setDropIntent(intent)
        setDragOverTarget(intent?.target ?? null)
      }
    }

    const flushDragFrame = () => {
      dragFrameRef.current = null
      const candidate = dragCandidateRef.current
      const point = pendingDragPointRef.current
      if (!candidate || !point) {
        return
      }

      if (!draggedTaskNameRef.current) {
        draggedTaskNameRef.current = candidate.taskName
        setDraggedTaskName(candidate.taskName)
      }

      applyDropIntent(getPointerDropIntent(point.clientX, point.clientY))
    }

    const handleGlobalPointerMove = (event: PointerEvent) => {
      const candidate = dragCandidateRef.current
      if (!candidate) {
        return
      }

      const distance = Math.hypot(event.clientX - candidate.startX, event.clientY - candidate.startY)
      if (!draggedTaskNameRef.current && distance < DRAG_START_DISTANCE) {
        return
      }

      event.preventDefault()
      pendingDragPointRef.current = { clientX: event.clientX, clientY: event.clientY }
      if (dragFrameRef.current == null) {
        dragFrameRef.current = window.requestAnimationFrame(flushDragFrame)
      }
    }

    const finishPointerDrag = (event: PointerEvent) => {
      const candidate = dragCandidateRef.current
      if (!candidate) {
        return
      }

      const distance = Math.hypot(event.clientX - candidate.startX, event.clientY - candidate.startY)
      const wasDragging = Boolean(draggedTaskNameRef.current) || distance >= DRAG_START_DISTANCE
      const intent = getPointerDropIntent(event.clientX, event.clientY) || dropIntentRef.current

      dragCandidateRef.current = null
      pendingDragPointRef.current = null
      if (dragFrameRef.current != null) {
        window.cancelAnimationFrame(dragFrameRef.current)
        dragFrameRef.current = null
      }
      dropIntentRef.current = null
      dragOverTargetRef.current = null
      setDropIntent(null)
      setDragOverTarget(null)
      setDraggedTaskName('')

      if (!wasDragging) {
        return
      }

      suppressCardClickRef.current = candidate.taskName
      draggedTaskNameRef.current = candidate.taskName
      if (intent) {
        void handleTaskDrop(intent, candidate.taskName)
      } else {
        draggedTaskNameRef.current = ''
      }
    }

    const cancelPointerDrag = () => {
      dragCandidateRef.current = null
      draggedTaskNameRef.current = ''
      pendingDragPointRef.current = null
      if (dragFrameRef.current != null) {
        window.cancelAnimationFrame(dragFrameRef.current)
        dragFrameRef.current = null
      }
      dropIntentRef.current = null
      dragOverTargetRef.current = null
      setDropIntent(null)
      setDragOverTarget(null)
      setDraggedTaskName('')
    }

    window.addEventListener('pointermove', handleGlobalPointerMove)
    window.addEventListener('pointerup', finishPointerDrag)
    window.addEventListener('pointercancel', cancelPointerDrag)
    return () => {
      window.removeEventListener('pointermove', handleGlobalPointerMove)
      window.removeEventListener('pointerup', finishPointerDrag)
      window.removeEventListener('pointercancel', cancelPointerDrag)
      if (dragFrameRef.current != null) {
        window.cancelAnimationFrame(dragFrameRef.current)
        dragFrameRef.current = null
      }
    }
  }, [handleTaskDrop])

  useEffect(() => {
    if (!draggedTaskName) {
      return
    }

    const previousCursor = document.body.style.cursor
    const previousUserSelect = document.body.style.userSelect
    document.body.style.cursor = 'grabbing'
    document.body.style.userSelect = 'none'
    return () => {
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousUserSelect
    }
  }, [draggedTaskName])

  const exitSelectMode = () => {
    setSelectMode(false)
    clearSelection()
  }

  const handleCardClick = useCallback((task: Task) => {
    if (suppressCardClickRef.current === task.name) {
      suppressCardClickRef.current = ''
      return
    }

    if (selectMode) {
      toggleSelect(task.name)
      return
    }
    setDetailTask(task)
    void api.getTask(task.name).then(fullTask => {
      setDetailTask(current => current?.name === task.name ? fullTask : current)
    }).catch(() => {})
  }, [selectMode, toggleSelect])

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

        {taskActionMessage && (
          <span className="rounded-md bg-accent/8 px-2.5 py-1 text-xs font-medium text-accent" title={taskActionMessage}>
            {taskActionMessage}
          </span>
        )}

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
            {showPinnedSection && (
              <div
                data-task-drop-target="pinned"
                className={clsx(
                  'rounded-md transition-colors',
                  dragOverTarget === 'pinned' && 'bg-accent/8 ring-1 ring-accent/30',
                )}
              >
                <CompactSection
                  title="Pinned Tasks"
                  subtitle={dragOverTarget === 'pinned' ? 'Drop to pin or reorder' : undefined}
                  count={pinnedTasks.length}
                  icon={<Pin className="h-3.5 w-3.5 text-accent" />}
                  accent
                  className="rounded-md border border-accent/20 bg-accent/5 p-2"
                  bodyClassName="pt-0"
                >
                  {pinnedTasks.length === 0 ? (
                    <div className="px-2 py-5 text-center text-xs font-medium text-accent">
                      Drop here to pin
                    </div>
                  ) : (
                    <TaskGrid
                      tasks={pinnedTasks}
                      columns={effectiveTaskColumns}
                      query={query}
                      draggedTaskName={draggedTaskName}
                      dropIntent={dropIntent}
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
                      onPointerDown={handleTaskPointerDown}
                    />
                  )}
                </CompactSection>
              </div>
            )}

            <div
              data-task-drop-target="tasks"
              className={clsx(
                'rounded-md transition-colors',
                dragOverTarget === 'tasks' && 'bg-surface-overlay ring-1 ring-border',
              )}
            >
            <CompactSection
              title="Tasks"
              subtitle={dragOverTarget === 'tasks' ? 'Drop to reorder' : `${otherTasks.length} task${otherTasks.length > 1 ? 's' : ''}`}
              icon={<Rows3 className="h-3.5 w-3.5 text-txt-tertiary" />}
              bodyClassName="p-2"
            >
              <TaskGrid
                tasks={otherTasks}
                columns={effectiveTaskColumns}
                query={query}
                draggedTaskName={draggedTaskName}
                dropIntent={dropIntent}
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
                onPointerDown={handleTaskPointerDown}
              />
            </CompactSection>
            </div>
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
  query,
  draggedTaskName,
  dropIntent,
  selectedIds,
  selectMode,
  onCardClick,
  onTaskAction,
  onPin,
  onDelete,
  onMonitor,
  onPointerDown,
}: {
  tasks: Task[]
  columns: number
  query: string
  draggedTaskName: string
  dropIntent: DropIntent | null
  selectedIds: Set<string>
  selectMode: boolean
  onCardClick: (task: Task) => void
  onTaskAction: (task: Task, action: 'run' | 'cancel' | 'rerun') => void | Promise<void>
  onPin: (task: Task) => void | Promise<void>
  onDelete: (task: Task) => void
  onMonitor: (task: Task) => void
  onPointerDown: (task: Task, event: ReactPointerEvent<HTMLElement>) => void
}) {
  if (tasks.length === 0) {
    return <div className="px-2 py-5 text-center text-2xs text-txt-tertiary">No tasks in this section</div>
  }

  return (
    <div
      data-task-grid="true"
      data-task-grid-columns={columns}
      className="grid gap-2"
      style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}
    >
      {tasks.map(task => (
        <TaskCard
          key={task.name}
          task={task}
          query={query}
          dragging={draggedTaskName === task.name}
          dropPlacement={
            dropIntent?.targetName === task.name && dropIntent.target === (task.pinned ? 'pinned' : 'tasks')
              ? dropIntent.placement
              : null
          }
          dropAxis={dropIntent?.targetName === task.name ? dropIntent.axis : null}
          selected={selectedIds.has(task.name)}
          selectMode={selectMode}
          onClick={() => onCardClick(task)}
          onAction={action => void onTaskAction(task, action)}
          onPin={() => void onPin(task)}
          onDelete={() => onDelete(task)}
          onMonitor={() => onMonitor(task)}
          onPointerDown={event => onPointerDown(task, event)}
        />
      ))}
    </div>
  )
}

function TaskCard({
  task,
  query,
  dragging,
  dropPlacement,
  dropAxis,
  selected,
  selectMode,
  onClick,
  onAction,
  onPin,
  onDelete,
  onMonitor,
  onPointerDown,
}: {
  task: Task
  query: string
  dragging: boolean
  dropPlacement: DragPlacement | null
  dropAxis: 'horizontal' | 'vertical' | null
  selected: boolean
  selectMode: boolean
  onClick: () => void
  onAction: (action: 'run' | 'cancel' | 'rerun') => void
  onPin: () => void
  onDelete: () => void
  onMonitor: () => void
  onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void
}) {
  const actionBtn = getActionButton(task)
  const folderName = task.dir.split(/[\\/]/).pop() || task.dir
  const taskKindLabel = task.task_kind === 'shell' ? 'shell' : 'python'
  const cardDescription = task._load_error || task.preview_text || 'No preview available.'
  const searchMatches = getTaskSearchMatches(task, query)
  const dropIndicator = dropPlacement ? <DropIndicator placement={dropPlacement} axis={dropAxis || 'horizontal'} /> : null

  return (
    <div
      data-task-card={task.name}
      data-task-card-pinned={task.pinned ? 'true' : 'false'}
      onPointerDown={onPointerDown}
      className={clsx(
        'group relative cursor-grab rounded-md border bg-surface-raised px-3 py-2.5 transition-[border-color,box-shadow,background-color,opacity,transform] duration-150 ease-out active:cursor-grabbing',
        selected ? 'border-accent bg-accent/8 ring-1 ring-accent/20' : 'border-border-subtle hover:border-border',
        task.pinned && !selected && 'border-accent/20',
        !selected && !dragging && 'hover:-translate-y-0.5 hover:shadow-[0_8px_22px_rgba(15,23,42,0.07)]',
        dragging && 'scale-[0.985] border-accent/35 bg-accent/5 opacity-70 shadow-[0_10px_30px_rgba(15,23,42,0.14)] ring-1 ring-accent/35',
        dropPlacement && !dragging && 'border-accent/35 bg-accent/5 ring-1 ring-accent/25',
      )}
      onClick={onClick}
    >
      {dropIndicator}
      {!selectMode && (
        <div
          className={clsx(
            'absolute left-2 top-2 flex h-5 w-5 items-center justify-center rounded-md text-txt-tertiary transition-[background-color,color,opacity]',
            'group-hover:bg-surface-overlay group-hover:text-txt-secondary',
            dragging && 'bg-accent/10 text-accent',
          )}
          title="Drag to reorder or move"
        >
          <GripVertical className="h-3.5 w-3.5" />
        </div>
      )}
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
          aria-label={task.pinned ? `Unpin ${task.name}` : `Pin ${task.name}`}
          className={clsx(
            'absolute right-2.5 top-2.5 rounded-md p-1 transition-colors hover:bg-surface-overlay',
            task.pinned ? 'text-accent' : 'text-txt-tertiary hover:text-accent'
          )}
        >
          <Pin className="h-3.5 w-3.5" />
        </button>
      )}

      <div className="flex items-center gap-2 pl-5 pr-7">
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

      {searchMatches.length > 0 && (
        <div className="mt-2 flex min-w-0 items-center gap-1.5 text-2xs text-txt-secondary" title={searchMatches.map(match => `${match.label}: ${match.detail}`).join('\n')}>
          <Search className="h-3 w-3 flex-none text-accent" />
          <span className="flex-none text-txt-tertiary">Matched in</span>
          <div className="flex min-w-0 flex-wrap gap-1">
            {searchMatches.slice(0, 3).map(match => (
              <span key={match.label} className="rounded-md bg-accent/8 px-1.5 py-0.5 font-medium text-accent">
                {match.label}
              </span>
            ))}
          </div>
        </div>
      )}

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
              aria-label={`${actionBtn.label} ${task.name}`}
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
            aria-label={`View logs for ${task.name}`}
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
            aria-label={`Delete ${task.name}`}
            className="rounded-md p-1.5 text-rose-400 transition-colors hover:bg-rose-500/10 hover:text-rose-300"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
    </div>
  )
}

function DropIndicator({ placement, axis }: { placement: DragPlacement; axis: 'horizontal' | 'vertical' }) {
  const vertical = axis === 'vertical'

  return (
    <span
      aria-hidden="true"
      className={clsx(
        'pointer-events-none absolute z-20 rounded-full bg-accent shadow-[0_0_0_3px_rgba(20,184,166,0.16)]',
        vertical ? 'left-2 right-2 h-0.5' : 'top-2 bottom-2 w-0.5',
        vertical
          ? (placement === 'before' ? '-top-px' : '-bottom-px')
          : (placement === 'before' ? '-left-px' : '-right-px'),
      )}
    >
      <span
        className={clsx(
          'absolute h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_0_3px_rgba(20,184,166,0.16)]',
          vertical
            ? 'left-0 top-1/2 -translate-x-1/2 -translate-y-1/2'
            : 'left-1/2 top-0 -translate-x-1/2 -translate-y-1/2',
        )}
      />
    </span>
  )
}

function normalizeSearchValue(value: unknown) {
  return String(value ?? '').toLowerCase().replace(/\s*:\s*/g, ':')
}

function getSearchNeedles(query: string) {
  return query
    .split('\n')
    .map(line => normalizeSearchValue(line.trim()))
    .filter(Boolean)
}

function flattenTaskConfig(value: unknown, prefix = ''): string[] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return []
  }

  const rows: string[] = []
  for (const [key, childValue] of Object.entries(value as Record<string, unknown>)) {
    if (key.startsWith('_meta')) {
      continue
    }
    const fullKey = prefix ? `${prefix}.${key}` : key
    if (childValue && typeof childValue === 'object' && !Array.isArray(childValue)) {
      rows.push(...flattenTaskConfig(childValue, fullKey))
    } else {
      rows.push(`${fullKey}: ${String(childValue ?? '')}`)
      const shortKey = fullKey.split('.').pop()
      if (shortKey && shortKey !== fullKey) {
        rows.push(`${shortKey}: ${String(childValue ?? '')}`)
      }
    }
  }
  return rows
}

function fieldHasNeedle(text: string, needles: string[]) {
  const normalized = normalizeSearchValue(text)
  return needles.some(needle => normalized.includes(needle))
}

function getTaskSearchMatches(task: Task, query: string): TaskSearchMatch[] {
  const needles = getSearchNeedles(query)
  if (needles.length === 0) {
    return []
  }

  const envText = Object.entries(task.env || {})
    .map(([key, value]) => `${key}: ${value}`)
    .join('\n')
  const configText = task.task_kind === 'shell'
    ? task.config_text || task.preview_text || task.search_text || ''
    : flattenTaskConfig(task.config || {}).join('\n') || task.search_text || task.preview_text || ''

  const fields: TaskSearchMatch[] = [
    { label: 'Name', detail: task.name },
    { label: 'Notes', detail: task.notes || '' },
    { label: 'Env', detail: envText },
    {
      label: task.task_kind === 'shell' ? 'Script' : 'Config',
      detail: configText,
    },
  ]

  return fields.filter(field => field.detail && fieldHasNeedle(field.detail, needles))
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
