import {
  memo,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle, ArrowDown, ArrowUp, ArrowUpDown, ChevronDown, Cpu, GripVertical, Loader2, MousePointer2, Pin, Play,
  RefreshCw, RotateCcw, Rows3, Search, Square, Terminal, Trash2,
} from 'lucide-react'
import clsx from 'clsx'
import { useMonitorStore, useTaskStore, useToastStore, useWorkspaceStore } from '@/store'
import { usePolling } from '@/hooks/usePolling'
import SearchInput from '@/components/shared/SearchInput'
import SelectionIndicator from '@/components/shared/SelectionIndicator'
import Pagination from '@/components/shared/Pagination'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import ActionButton from '@/components/shared/ActionButton'
import CompactSection from '@/components/shared/CompactSection'
import TaskDetailPanel from './TaskDetailPanel'
import type { Task, TaskSortMode } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import { ALL_STATUSES, STATUS_LABELS } from '@/theme/tokens'
import { errorMessage } from '@/utils/errors'
import * as api from '@/api'

const STATUS_OPTIONS = ['All', ...ALL_STATUSES]
const TASK_SORT_OPTIONS: { value: TaskSortMode; label: string }[] = [
  { value: 'priority', label: 'Smart priority' },
  { value: 'manual', label: 'Manual order' },
  { value: 'activity_desc', label: 'Recent activity' },
  { value: 'activity_asc', label: 'Oldest activity' },
  { value: 'name_asc', label: 'Name A-Z' },
  { value: 'name_desc', label: 'Name Z-A' },
]
type DragTarget = 'pinned' | 'tasks'
type DragPlacement = 'before' | 'after'
type PendingTaskAction = 'run' | 'rerun' | 'cancel' | 'pin' | 'move' | 'delete'
type ManagerMetricTone = 'neutral' | 'amber' | 'emerald' | 'rose'
const DRAG_START_DISTANCE = 8
const REORDER_TASK_LIMIT = 10_000

function isOrderMutation(action: PendingTaskAction) {
  return action === 'pin' || action === 'move'
}

function hasPendingOrderMutation(actions: Map<string, PendingTaskAction>) {
  return [...actions.values()].some(isOrderMutation)
}

const MANAGER_STATUS_STYLES: Record<TaskStatus, string> = {
  pending: 'border-slate-200 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-800/70 dark:text-slate-200',
  queued: 'border-blue-200 bg-blue-50 text-blue-700 dark:border-blue-800 dark:bg-blue-950/45 dark:text-blue-300',
  running: 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950/45 dark:text-amber-300',
  completed: 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/45 dark:text-emerald-300',
  failed: 'border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-800 dark:bg-rose-950/45 dark:text-rose-300',
  cancelled: 'border-slate-200 bg-slate-50 text-slate-700 dark:border-slate-700 dark:bg-slate-900/50 dark:text-slate-300',
}

const MANAGER_STATUS_DOTS: Record<TaskStatus, string> = {
  pending: 'bg-slate-500',
  queued: 'bg-blue-600 dark:bg-blue-400',
  running: 'bg-amber-600 dark:bg-amber-400',
  completed: 'bg-emerald-600 dark:bg-emerald-400',
  failed: 'bg-rose-600 dark:bg-rose-400',
  cancelled: 'bg-slate-500 dark:bg-slate-400',
}

const MANAGER_METRIC_STYLES: Record<ManagerMetricTone, string> = {
  neutral: 'border-border-subtle bg-surface-overlay text-txt-primary',
  amber: 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-800 dark:bg-amber-950/45 dark:text-amber-300',
  emerald: 'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/45 dark:text-emerald-300',
  rose: 'border-rose-200 bg-rose-50 text-rose-700 dark:border-rose-800 dark:bg-rose-950/45 dark:text-rose-300',
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

function getDropTargetFromElement(element: HTMLElement | null): DragTarget | null {
  const target = element?.closest<HTMLElement>('[data-task-drop-target]')
  const value = target?.dataset.taskDropTarget
  return value === 'pinned' || value === 'tasks' ? value : null
}

function getCardDropPlacement(card: HTMLElement, clientX: number, clientY: number): Pick<DropIntent, 'placement' | 'axis'> {
  const rect = card.getBoundingClientRect()
  const grid = card.closest<HTMLElement>('[data-task-grid]')
  const renderedColumnCount = grid
    ? window.getComputedStyle(grid).gridTemplateColumns
        .split(/\s+/)
        .filter(track => Number.parseFloat(track) > 0).length
    : 0
  const configuredColumnCount = Number.parseInt(grid?.dataset.taskGridColumns || '1', 10)
  const columnCount = renderedColumnCount || configuredColumnCount
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

function hasReorderChanges(tasks: Task[], items: { name: string; pinned: boolean }[]) {
  return tasks.length !== items.length || tasks.some((task, index) => (
    task.name !== items[index]?.name || Boolean(task.pinned) !== items[index]?.pinned
  ))
}

export default function ManagerPage() {
  const {
    tasks, total, statusCounts, offset, limit, query, statusFilter, sortMode, selectedIds, loading, error, columns,
    setQuery, setStatusFilter, setSortMode, setOffset, setColumns, fetchTasks,
    toggleSelect, selectAll, clearSelection,
  } = useTaskStore()

  const [maxWorkersInput, setMaxWorkersInput] = useState('2')
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const [deleteTask, setDeleteTask] = useState<Task | null>(null)
  const [cancelTask, setCancelTask] = useState<Task | null>(null)
  const [detailTask, setDetailTask] = useState<Task | null>(null)
  const [selectMode, setSelectMode] = useState(false)
  const [draggedTaskName, setDraggedTaskName] = useState('')
  const [dragOverTarget, setDragOverTarget] = useState<DragTarget | null>(null)
  const [dropIntent, setDropIntent] = useState<DropIntent | null>(null)
  const [taskActionMessage, setTaskActionMessage] = useState('')
  const [pendingTaskActions, setPendingTaskActions] = useState<Map<string, PendingTaskAction>>(() => new Map())
  const [bulkAction, setBulkAction] = useState<'run' | 'delete' | null>(null)
  const dragCandidateRef = useRef<DragCandidate | null>(null)
  const draggedTaskNameRef = useRef('')
  const dragOverTargetRef = useRef<DragTarget | null>(null)
  const dropIntentRef = useRef<DropIntent | null>(null)
  const pendingDragPointRef = useRef<{ clientX: number; clientY: number } | null>(null)
  const dragFrameRef = useRef<number | null>(null)
  const pendingTaskActionsRef = useRef<Map<string, PendingTaskAction>>(new Map())
  const bulkActionRef = useRef<'run' | 'delete' | null>(null)
  const detailRequestSeqRef = useRef(0)
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const focusTaskName = searchParams.get('task')
  const notify = useToastStore(state => state.notify)
  const workspaceEpoch = useWorkspaceStore(state => state.workspaceEpoch)
  const rejectIncompleteReorder = useCallback((page: { has_more: boolean; total: number }) => {
    if (!page.has_more) {
      return false
    }
    notify({
      tone: 'error',
      title: 'Too many tasks to reorder safely',
      detail: `This workspace has ${page.total.toLocaleString()} tasks. Reordering is disabled when the complete order cannot be loaded.`,
    })
    return true
  }, [notify])

  const hasActive = statusCounts
    ? statusCounts.running + statusCounts.queued > 0
    : tasks.some(task => task.status === 'running' || task.status === 'queued')
  usePolling(fetchTasks, hasActive ? 3000 : 10000, true, false)

  useEffect(() => {
    void fetchTasks()
  }, [query, statusFilter, sortMode, offset, fetchTasks, workspaceEpoch])

  useEffect(() => {
    detailRequestSeqRef.current += 1
    setDeleteConfirm(false)
    setDeleteTask(null)
    setCancelTask(null)
    setDetailTask(null)
    setSelectMode(false)
    setTaskActionMessage('')
    pendingTaskActionsRef.current = new Map()
    setPendingTaskActions(new Map())
    bulkActionRef.current = null
    setBulkAction(null)
  }, [workspaceEpoch])

  useEffect(() => {
    if (!focusTaskName) {
      return
    }

    let cancelled = false
    const requestWorkspaceEpoch = workspaceEpoch
    const requestIsCurrent = () => (
      !cancelled
      && requestWorkspaceEpoch === useWorkspaceStore.getState().workspaceEpoch
    )
    void api.getTask(focusTaskName).then(task => {
      if (requestIsCurrent()) {
        setDetailTask(task)
      }
    }).catch(() => {
      if (requestIsCurrent()) {
        const next = new URLSearchParams(searchParams)
        next.delete('task')
        setSearchParams(next, { replace: true })
      }
    })

    return () => {
      cancelled = true
    }
  }, [focusTaskName, searchParams, setSearchParams, workspaceEpoch])

  const visibleSelectedCount = tasks.reduce(
    (count, task) => count + (selectedIds.has(task.name) ? 1 : 0),
    0,
  )
  const anyTaskActionPending = pendingTaskActions.size > 0
  const orderMutationPending = hasPendingOrderMutation(pendingTaskActions)
  const activeSelectedCount = tasks.reduce(
    (count, task) => count + (
      selectedIds.has(task.name) && (task.status === 'running' || task.status === 'queued') ? 1 : 0
    ),
    0,
  )
  const allPageSelected = tasks.length > 0 && visibleSelectedCount === tasks.length
  const pinnedTasks = useMemo(() => tasks.filter(task => task.pinned), [tasks])
  const otherTasks = useMemo(() => tasks.filter(task => !task.pinned), [tasks])
  const draggedTask = useMemo(
    () => tasks.find(task => task.name === draggedTaskName) || null,
    [draggedTaskName, tasks],
  )
  const showPinnedSection = pinnedTasks.length > 0 || Boolean(draggedTask && !draggedTask.pinned)
  const summary = useMemo(() => statusCounts ? {
    total: Object.values(statusCounts).reduce((count, value) => count + value, 0),
    active: statusCounts.running + statusCounts.queued,
    completed: statusCounts.completed,
    failed: statusCounts.failed,
  } : null, [statusCounts])

  draggedTaskNameRef.current = draggedTaskName
  dragOverTargetRef.current = dragOverTarget
  dropIntentRef.current = dropIntent

  useEffect(() => {
    if (!taskActionMessage) {
      return
    }
    const timeout = window.setTimeout(() => setTaskActionMessage(''), 4200)
    return () => window.clearTimeout(timeout)
  }, [taskActionMessage])

  useEffect(() => {
    if (deleteConfirm && visibleSelectedCount === 0 && !bulkAction) {
      setDeleteConfirm(false)
    }
  }, [bulkAction, deleteConfirm, visibleSelectedCount])

  useEffect(() => {
    setDetailTask(current => {
      if (!current) {
        return current
      }

      const refreshed = tasks.find(task => task.name === current.name)
      if (!refreshed || refreshed === current) {
        return current
      }

      return {
        ...refreshed,
        config: current.config,
        config_text: current.config_text,
        records: current.records,
        tracks: current.tracks,
      }
    })
  }, [tasks])

  const beginTaskAction = useCallback((taskName: string, action: PendingTaskAction) => {
    if (
      bulkActionRef.current
      || pendingTaskActionsRef.current.has(taskName)
      || (isOrderMutation(action) && hasPendingOrderMutation(pendingTaskActionsRef.current))
    ) {
      return false
    }
    const next = new Map(pendingTaskActionsRef.current)
    next.set(taskName, action)
    pendingTaskActionsRef.current = next
    setPendingTaskActions(next)
    return true
  }, [])

  const finishTaskAction = useCallback((taskName: string) => {
    if (!pendingTaskActionsRef.current.has(taskName)) {
      return
    }
    const next = new Map(pendingTaskActionsRef.current)
    next.delete(taskName)
    pendingTaskActionsRef.current = next
    setPendingTaskActions(next)
  }, [])

  const beginBulkAction = useCallback((action: 'run' | 'delete') => {
    if (bulkActionRef.current || pendingTaskActionsRef.current.size > 0) {
      return false
    }
    bulkActionRef.current = action
    setBulkAction(action)
    return true
  }, [])

  const finishBulkAction = useCallback(() => {
    bulkActionRef.current = null
    setBulkAction(null)
  }, [])

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
    const names = tasks.filter(task => selectedIds.has(task.name)).map(task => task.name)
    if (!names.length || !beginBulkAction('run')) return
    const maxWorkers = normalizeWorkerInput(maxWorkersInput)
    setMaxWorkersInput(String(maxWorkers))
    try {
      const result = await api.batchRunTasks(names, maxWorkers)
      clearSelection()
      setSelectMode(false)
      await fetchTasks()
      const skipped = result.skipped || []
      notify({
        tone: skipped.length ? 'info' : 'success',
        title: skipped.length ? 'Some tasks skipped' : 'Tasks queued',
        detail: skipped.length
          ? `${result.count} queued; skipped ${skipped.join(', ')}.`
          : `${result.count} task${result.count === 1 ? '' : 's'} scheduled with ${maxWorkers} worker${maxWorkers === 1 ? '' : 's'}.`,
      })
    } catch (err) {
      notify({ tone: 'error', title: 'Could not start tasks', detail: errorMessage(err) })
    } finally {
      finishBulkAction()
    }
  }, [tasks, selectedIds, beginBulkAction, normalizeWorkerInput, maxWorkersInput, clearSelection, fetchTasks, notify, finishBulkAction])

  const handleDeleteSelected = useCallback(async () => {
    const names = tasks.filter(task => selectedIds.has(task.name)).map(task => task.name)
    if (!names.length || !beginBulkAction('delete')) return
    try {
      const result = await api.batchDeleteTasks(names)
      clearSelection()
      setDeleteConfirm(false)
      setSelectMode(false)
      await fetchTasks()
      const deleted = new Set(result.deleted || [])
      const skipped = names.filter(name => !deleted.has(name))
      notify({
        tone: skipped.length ? 'info' : 'success',
        title: skipped.length ? 'Some tasks could not be deleted' : 'Tasks moved to trash',
        detail: skipped.length
          ? `${result.count} deleted; skipped ${skipped.join(', ')}.`
          : `${result.count} task${result.count === 1 ? '' : 's'} deleted.`,
      })
    } catch (err) {
      notify({ tone: 'error', title: 'Could not delete tasks', detail: errorMessage(err) })
    } finally {
      finishBulkAction()
    }
  }, [tasks, selectedIds, beginBulkAction, clearSelection, fetchTasks, notify, finishBulkAction])

  const handleDeleteTask = useCallback(async () => {
    if (!deleteTask) return
    const target = deleteTask
    if (!beginTaskAction(target.name, 'delete')) return
    try {
      await api.batchDeleteTasks([target.name])
      if (detailTask?.name === target.name) {
        setDetailTask(null)
      }
      setDeleteTask(null)
      await fetchTasks()
      notify({ tone: 'success', title: 'Task moved to trash', detail: target.name })
    } catch (err) {
      notify({ tone: 'error', title: 'Could not delete task', detail: errorMessage(err) })
    } finally {
      finishTaskAction(target.name)
    }
  }, [deleteTask, beginTaskAction, detailTask, fetchTasks, notify, finishTaskAction])

  const handleTaskAction = useCallback(async (task: Task, action: 'run' | 'cancel' | 'rerun') => {
    if (!beginTaskAction(task.name, action)) return
    try {
      let updatedTask: Task | null = null
      if (action === 'run' || action === 'rerun') {
        updatedTask = (await api.runTask(task.name)).task
      } else {
        updatedTask = (await api.cancelTask(task.name)).task
      }
      await fetchTasks()
      const waitingForGpu = updatedTask?.status === 'queued' && Boolean(updatedTask.gpu_wait)
      const queued = updatedTask?.status === 'queued'
      notify({
        tone: 'success',
        title: action === 'cancel'
          ? 'Cancel requested'
          : waitingForGpu
            ? 'Waiting for GPU capacity'
            : queued
              ? 'Task queued'
              : 'Task started',
        detail: task.name,
      })
    } catch (err) {
      notify({
        tone: 'error',
        title: action === 'cancel' ? 'Could not cancel task' : 'Could not start task',
        detail: errorMessage(err),
      })
    } finally {
      finishTaskAction(task.name)
    }
  }, [beginTaskAction, fetchTasks, notify, finishTaskAction])

  const requestTaskAction = useCallback((task: Task, action: 'run' | 'cancel' | 'rerun') => {
    if (action === 'cancel') {
      setCancelTask(task)
      return
    }
    void handleTaskAction(task, action)
  }, [handleTaskAction])

  const openTaskLogs = useCallback((task: Task) => {
    void useMonitorStore.getState().selectTask(task.name)
      .catch(err => notify({ tone: 'error', title: 'Could not load task logs', detail: errorMessage(err) }))
    navigate('/monitor')
  }, [navigate, notify])

  const handlePin = useCallback(async (task: Task) => {
    if (!beginTaskAction(task.name, 'pin')) return
    try {
      await api.pinTask(task.name, !task.pinned)
      setTaskActionMessage(task.pinned ? `Moved ${task.name} back to Tasks.` : `Pinned ${task.name}.`)
      await fetchTasks()
    } catch (err) {
      notify({ tone: 'error', title: 'Could not update pin', detail: errorMessage(err) })
    } finally {
      finishTaskAction(task.name)
    }
  }, [beginTaskAction, fetchTasks, notify, finishTaskAction])

  const handleTaskPointerDown = useCallback((task: Task, event: ReactPointerEvent<HTMLElement>) => {
    if (
      selectMode
      || Boolean(bulkActionRef.current)
      || pendingTaskActionsRef.current.has(task.name)
      || hasPendingOrderMutation(pendingTaskActionsRef.current)
      || event.button !== 0
    ) {
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

  const refreshManualOrder = useCallback(async () => {
    if (useTaskStore.getState().sortMode === 'manual') {
      await fetchTasks()
      return
    }
    setSortMode('manual')
  }, [fetchTasks, setSortMode])

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
    if (intent.targetName === task.name && intent.target === (task.pinned ? 'pinned' : 'tasks')) {
      setTaskActionMessage(`${task.name} is already in this position.`)
      return
    }
    if (!beginTaskAction(task.name, 'move')) {
      return
    }

    try {
      const allTasks = await api.getTasks({
        limit: REORDER_TASK_LIMIT,
        refresh: false,
        compact: true,
        sort: sortMode,
      })
      if (rejectIncompleteReorder(allTasks)) {
        return
      }
      const items = buildReorderedItems(allTasks.items, task.name, intent)
      const movedItem = items.find(item => item.name === task.name)
      if (!items.length || !movedItem) {
        return
      }
      if (!hasReorderChanges(allTasks.items, items)) {
        setTaskActionMessage(`${task.name} is already in this position.`)
        return
      }

      await api.reorderTasks(items)
      if (movedItem.pinned !== task.pinned) {
        setTaskActionMessage(movedItem.pinned ? `Pinned ${task.name}.` : `Moved ${task.name} back to Tasks.`)
      } else {
        setTaskActionMessage(`Moved ${task.name}.`)
      }
      await refreshManualOrder()
    } catch (err) {
      notify({ tone: 'error', title: 'Could not move task', detail: errorMessage(err) })
    } finally {
      finishTaskAction(task.name)
    }
  }, [beginTaskAction, notify, refreshManualOrder, rejectIncompleteReorder, sortMode, tasks, finishTaskAction])

  const handleMoveTask = useCallback(async (task: Task, direction: -1 | 1) => {
    if (!beginTaskAction(task.name, 'move')) return
    try {
      const visibleSectionTasks = tasks.filter(item => Boolean(item.pinned) === Boolean(task.pinned))
      const currentIndex = visibleSectionTasks.findIndex(item => item.name === task.name)
      const targetTask = visibleSectionTasks[currentIndex + direction]
      if (currentIndex < 0 || !targetTask) {
        setTaskActionMessage(
          direction < 0
            ? `${task.name} is already first among the visible tasks.`
            : `${task.name} is already last among the visible tasks.`,
        )
        return
      }

      const allTasks = await api.getTasks({
        limit: REORDER_TASK_LIMIT,
        refresh: false,
        compact: true,
        sort: sortMode,
      })
      if (rejectIncompleteReorder(allTasks)) {
        return
      }

      const intent: DropIntent = {
        target: task.pinned ? 'pinned' : 'tasks',
        targetName: targetTask.name,
        placement: direction < 0 ? 'before' : 'after',
        axis: 'vertical',
      }
      const items = buildReorderedItems(allTasks.items, task.name, intent)
      if (!items.length) {
        return
      }
      await api.reorderTasks(items)
      setTaskActionMessage(`Moved ${task.name} ${direction < 0 ? 'earlier' : 'later'}.`)
      await refreshManualOrder()
    } catch (err) {
      notify({ tone: 'error', title: 'Could not move task', detail: errorMessage(err) })
    } finally {
      finishTaskAction(task.name)
    }
  }, [beginTaskAction, notify, refreshManualOrder, rejectIncompleteReorder, sortMode, tasks, finishTaskAction])

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
    if (selectMode) {
      toggleSelect(task.name)
      return
    }
    setDetailTask(task)
    const requestId = ++detailRequestSeqRef.current
    const requestWorkspaceEpoch = workspaceEpoch
    const requestIsCurrent = () => (
      requestId === detailRequestSeqRef.current
      && requestWorkspaceEpoch === useWorkspaceStore.getState().workspaceEpoch
    )
    void api.getTask(task.name).then(fullTask => {
      if (requestIsCurrent()) {
        setDetailTask(current => current?.name === task.name ? fullTask : current)
      }
    }).catch(err => {
      if (requestIsCurrent()) {
        notify({ tone: 'error', title: 'Could not load task details', detail: errorMessage(err) })
      }
    })
  }, [selectMode, toggleSelect, notify, workspaceEpoch])

  const closeDetailPanel = useCallback(() => {
    detailRequestSeqRef.current += 1
    setDetailTask(null)
    if (!searchParams.get('task')) {
      return
    }
    const next = new URLSearchParams(searchParams)
    next.delete('task')
    setSearchParams(next, { replace: true })
  }, [searchParams, setSearchParams])

  return (
    <div className="flex h-full flex-col overflow-hidden bg-surface-base">
      <header className="flex-none border-b border-border-subtle bg-surface-raised px-3 py-3 sm:px-4">
        <div className="flex flex-col gap-3 xl:flex-row xl:items-start xl:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-semibold tracking-tight text-txt-primary">Task Manager</h1>
              {loading && tasks.length > 0 && (
                <Loader2 className="h-3.5 w-3.5 animate-spin text-txt-tertiary" aria-label="Refreshing tasks" />
              )}
            </div>
            <p className="mt-0.5 text-xs text-txt-secondary">
              Review, organize, and run workspace tasks from one queue.
            </p>
          </div>

          <div
            className="grid grid-cols-2 gap-1.5 sm:flex sm:flex-wrap sm:justify-end"
            aria-label="Workspace task totals"
            title="Workspace totals are independent of the current search, status filter, and page."
          >
            <ManagerMetric label="All tasks" value={summary?.total ?? '—'} />
            <ManagerMetric label="Active" value={summary?.active ?? '—'} tone="amber" />
            <ManagerMetric label="Done" value={summary?.completed ?? '—'} tone="emerald" />
            <ManagerMetric label="Failed" value={summary?.failed ?? '—'} tone="rose" />
          </div>
        </div>

        <div className="mt-3 flex flex-col gap-2 lg:flex-row lg:items-start">
          <div className="w-full min-w-0 flex-none lg:w-auto lg:flex-[1_1_22rem]">
            <SearchInput
              value={query}
              onChange={setQuery}
              placeholder="Search tasks..."
              ariaLabel="Search tasks"
            />
          </div>

          <div className="flex flex-wrap items-center gap-2">
            <div className="relative flex-1 sm:flex-none">
              <select
                value={statusFilter}
                onChange={event => setStatusFilter(event.target.value)}
                aria-label="Filter tasks by status"
                title="Filter by status"
                className="touch-target min-h-11 w-full appearance-none rounded-md border border-border-subtle bg-surface-overlay px-3 py-1.5 pr-8 text-xs text-txt-primary outline-none transition-colors focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/20 sm:min-h-9 sm:w-auto"
              >
                {STATUS_OPTIONS.map(option => (
                  <option key={option} value={option}>
                    {option === 'All' ? 'All' : STATUS_LABELS[option as TaskStatus]}
                  </option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
            </div>

            <div className="relative flex-1 sm:flex-none">
              <ArrowUpDown className="pointer-events-none absolute left-2.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
              <select
                value={sortMode}
                onChange={event => setSortMode(event.target.value as TaskSortMode)}
                aria-label="Sort task cards"
                title={sortMode === 'priority'
                  ? 'Pinned first, then active and recently added tasks'
                  : 'Sort task cards'}
                className="touch-target min-h-11 w-full appearance-none rounded-md border border-border-subtle bg-surface-overlay py-1.5 pl-8 pr-8 text-xs text-txt-primary outline-none transition-colors focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/20 sm:min-h-9 sm:w-auto"
              >
                {TASK_SORT_OPTIONS.map(option => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
            </div>

            <div className="relative flex-1 sm:flex-none">
              <select
                value={columns}
                onChange={event => setColumns(Number(event.target.value))}
                aria-label="Maximum cards per row"
                title="Maximum cards per row; narrow windows reduce the count automatically"
                className="touch-target min-h-11 w-full appearance-none rounded-md border border-border-subtle bg-surface-overlay px-3 py-1.5 pr-8 text-xs text-txt-primary outline-none transition-colors focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/20 sm:min-h-9 sm:w-auto"
              >
                {[1, 2, 3, 4, 5, 6, 7, 8].map(count => (
                  <option key={count} value={count}>{count} col{count > 1 ? 's' : ''}</option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-2.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
            </div>

            {!selectMode && (
              <ActionButton
                icon={<MousePointer2 className="h-4 w-4" />}
                variant="primary"
                size="sm"
                className="h-11 sm:h-auto"
                disabled={tasks.length === 0 || anyTaskActionPending || Boolean(bulkAction)}
                title={anyTaskActionPending ? 'Wait for the current task action to finish' : undefined}
                onClick={() => setSelectMode(true)}
              >
                Select tasks
              </ActionButton>
            )}
          </div>
        </div>

        {taskActionMessage && (
          <div
            role="status"
            aria-live="polite"
            className="mt-2 rounded-md border border-accent/20 bg-accent/8 px-3 py-2 text-xs font-medium text-accent"
            title={taskActionMessage}
          >
            {taskActionMessage}
          </div>
        )}

        {selectMode && (
          <div
            className="mt-3 flex flex-col gap-2 rounded-md border border-accent/20 bg-accent/5 p-2.5 xl:flex-row xl:items-center"
            role="region"
            aria-label="Current page selection actions"
          >
            <div className="min-w-0 flex-1">
              <div className="text-xs font-semibold text-txt-primary" aria-live="polite">
                {visibleSelectedCount} of {tasks.length} selected on this page
              </div>
              <div className="mt-0.5 text-2xs text-txt-secondary">
                Selection is page-scoped and resets when you change the page, search, or status filter.
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <ActionButton
                icon={<SelectionIndicator selected={allPageSelected} />}
                variant="accentTint"
                className="min-h-11 sm:min-h-9"
                disabled={Boolean(bulkAction)}
                onClick={() => (allPageSelected ? clearSelection() : selectAll())}
              >
                {allPageSelected ? 'Clear page' : 'Select page'}
              </ActionButton>

              <label className="touch-target flex min-h-11 items-center gap-1.5 rounded-md border border-border-subtle bg-surface-raised px-2.5 text-2xs text-txt-secondary sm:min-h-9">
                Workers
                <input
                  type="number"
                  min={1}
                  max={32}
                  value={maxWorkersInput}
                  disabled={Boolean(bulkAction)}
                  onChange={event => setMaxWorkersInput(event.target.value)}
                  onBlur={() => setMaxWorkersInput(current => String(normalizeWorkerInput(current)))}
                  aria-label="Maximum parallel workers"
                  className="w-10 bg-transparent text-xs tabular-nums text-txt-primary outline-none disabled:opacity-50"
                />
              </label>

              <ActionButton
                icon={bulkAction === 'run' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                variant="success"
                className="min-h-11 sm:min-h-9"
                onClick={handleRunSelected}
                disabled={visibleSelectedCount === 0 || Boolean(bulkAction)}
                aria-busy={bulkAction === 'run' || undefined}
              >
                Run ({visibleSelectedCount})
              </ActionButton>

              <ActionButton
                icon={<Trash2 className="h-3.5 w-3.5" />}
                variant="danger"
                className="min-h-11 sm:min-h-9"
                onClick={() => visibleSelectedCount > 0 && setDeleteConfirm(true)}
                disabled={visibleSelectedCount === 0 || Boolean(bulkAction)}
              >
                Delete
              </ActionButton>

              <ActionButton className="min-h-11 sm:min-h-9" variant="ghost" disabled={Boolean(bulkAction)} onClick={exitSelectMode}>
                Cancel
              </ActionButton>
            </div>
          </div>
        )}
      </header>

      <section
        className="flex-1 overflow-y-auto p-3 sm:p-4"
        aria-label="Task list"
        aria-busy={loading || undefined}
      >
        {error && (
          <div
            role="alert"
            className="mb-3 flex flex-col gap-2 rounded-md border border-rose-200 bg-rose-50 px-3 py-2.5 text-rose-800 dark:border-rose-800 dark:bg-rose-950/45 dark:text-rose-300 sm:flex-row sm:items-center"
          >
            <AlertTriangle className="h-4 w-4 flex-none" />
            <div className="min-w-0 flex-1 text-xs">
              <span className="font-semibold">Tasks could not be refreshed.</span>{' '}
              <span className="break-words">{error}</span>
            </div>
            <ActionButton
              icon={<RefreshCw className="h-3.5 w-3.5" />}
              variant="ghost"
              className="min-h-11 sm:min-h-9"
              disabled={loading}
              onClick={() => void fetchTasks()}
            >
              Retry
            </ActionButton>
          </div>
        )}

        {loading && tasks.length === 0 ? (
          <ManagerLoadingState columns={columns} />
        ) : tasks.length === 0 ? (
          error ? null : (
            <ManagerEmptyState
              filtered={Boolean(query) || statusFilter !== 'All'}
              onClear={() => {
                setQuery('')
                setStatusFilter('All')
              }}
            />
          )
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
                      columns={columns}
                      query={query}
                      draggedTaskName={draggedTaskName}
                      dropIntent={dropIntent}
                      selectedIds={selectedIds}
                      selectMode={selectMode}
                      orderMutationPending={orderMutationPending}
                      pendingTaskActions={pendingTaskActions}
                      onCardClick={handleCardClick}
                      onTaskAction={requestTaskAction}
                      onPin={handlePin}
                      onMove={handleMoveTask}
                      onDelete={setDeleteTask}
                      onMonitor={openTaskLogs}
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
              subtitle={dragOverTarget === 'tasks' ? 'Drop to reorder' : `${otherTasks.length} on this page · ${total} matching`}
              icon={<Rows3 className="h-3.5 w-3.5 text-txt-tertiary" />}
              bodyClassName="p-2"
            >
              <TaskGrid
                tasks={otherTasks}
                columns={columns}
                query={query}
                draggedTaskName={draggedTaskName}
                dropIntent={dropIntent}
                selectedIds={selectedIds}
                selectMode={selectMode}
                orderMutationPending={orderMutationPending}
                pendingTaskActions={pendingTaskActions}
                onCardClick={handleCardClick}
                onTaskAction={requestTaskAction}
                onPin={handlePin}
                onMove={handleMoveTask}
                onDelete={setDeleteTask}
                onMonitor={openTaskLogs}
                onPointerDown={handleTaskPointerDown}
              />
            </CompactSection>
            </div>
          </div>
        )}
      </section>

      <footer className="flex flex-none flex-wrap items-center justify-between gap-2 border-t border-border-subtle bg-surface-raised px-3 py-2 sm:px-4">
        <span className="text-2xs text-txt-secondary">
          {selectMode && visibleSelectedCount > 0 ? `${visibleSelectedCount} selected on page · ` : ''}
          {tasks.length > 0 ? `Showing ${offset + 1}–${offset + tasks.length} of ` : ''}
          {total} matching task{total !== 1 ? 's' : ''}
        </span>
        <Pagination total={total} offset={offset} limit={limit} onOffsetChange={setOffset} />
      </footer>

      <ConfirmDialog
        open={deleteConfirm}
        title="Delete Tasks"
        description={activeSelectedCount > 0
          ? `Stop ${activeSelectedCount} active task${activeSelectedCount === 1 ? '' : 's'} and move all ${visibleSelectedCount} selected tasks from this page to trash?`
          : `Move ${visibleSelectedCount} selected task(s) from this page to trash?`}
        confirmLabel="Delete"
        confirmVariant="danger"
        onConfirm={handleDeleteSelected}
        onCancel={() => setDeleteConfirm(false)}
      />

      <ConfirmDialog
        open={Boolean(deleteTask)}
        title={deleteTask && (deleteTask.status === 'running' || deleteTask.status === 'queued') ? 'Stop and Delete Task' : 'Delete Task'}
        description={deleteTask
          ? deleteTask.status === 'running' || deleteTask.status === 'queued'
            ? `Stop '${deleteTask.name}' and move it to trash?`
            : `Move '${deleteTask.name}' to trash?`
          : ''}
        confirmLabel="Delete"
        confirmVariant="danger"
        onConfirm={handleDeleteTask}
        onCancel={() => setDeleteTask(null)}
      />

      <ConfirmDialog
        open={Boolean(cancelTask)}
        title="Stop active task?"
        description={cancelTask ? `Request cancellation for '${cancelTask.name}'? Its process tree will be stopped.` : ''}
        confirmLabel="Stop Task"
        confirmVariant="danger"
        onConfirm={async () => {
          if (!cancelTask) return
          const target = cancelTask
          try {
            await handleTaskAction(target, 'cancel')
          } finally {
            setCancelTask(null)
          }
        }}
        onCancel={() => setCancelTask(null)}
      />

      {detailTask && (
        <TaskDetailPanel
          task={detailTask}
          onClose={closeDetailPanel}
          onTaskUpdated={updatedTask => {
            setDetailTask(current => current?.name === updatedTask.name ? updatedTask : current)
          }}
          onRefresh={fetchTasks}
        />
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
  orderMutationPending,
  pendingTaskActions,
  onCardClick,
  onTaskAction,
  onPin,
  onMove,
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
  orderMutationPending: boolean
  pendingTaskActions: Map<string, PendingTaskAction>
  onCardClick: (task: Task) => void
  onTaskAction: (task: Task, action: 'run' | 'cancel' | 'rerun') => void | Promise<void>
  onPin: (task: Task) => void | Promise<void>
  onMove: (task: Task, direction: -1 | 1) => void | Promise<void>
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
      style={{
        gridTemplateColumns: `repeat(auto-fill, minmax(min(100%, max(15rem, calc((100% - ${(columns - 1) * 8}px) / ${columns}))), 1fr))`,
      }}
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
          reorderDisabled={orderMutationPending}
          pendingAction={pendingTaskActions.get(task.name) || null}
          onCardClick={onCardClick}
          onTaskAction={onTaskAction}
          onPin={onPin}
          onMove={onMove}
          onDelete={onDelete}
          onMonitor={onMonitor}
          onPointerDown={onPointerDown}
        />
      ))}
    </div>
  )
}

const TaskCard = memo(function TaskCard({
  task,
  query,
  dragging,
  dropPlacement,
  dropAxis,
  selected,
  selectMode,
  reorderDisabled,
  pendingAction,
  onCardClick,
  onTaskAction,
  onPin,
  onMove,
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
  reorderDisabled: boolean
  pendingAction: PendingTaskAction | null
  onCardClick: (task: Task) => void
  onTaskAction: (task: Task, action: 'run' | 'cancel' | 'rerun') => void | Promise<void>
  onPin: (task: Task) => void | Promise<void>
  onMove: (task: Task, direction: -1 | 1) => void | Promise<void>
  onDelete: (task: Task) => void
  onMonitor: (task: Task) => void
  onPointerDown: (task: Task, event: ReactPointerEvent<HTMLElement>) => void
}) {
  const actionBtn = getActionButton(task)
  const folderName = task.dir.split(/[\\/]/).pop() || task.dir
  const taskKindLabel = task.task_kind === 'shell' ? 'shell' : 'python'
  const cardDescription = task._load_error || task.preview_text || 'No preview available.'
  const searchMatches = useMemo(() => getTaskSearchMatches(task, query), [task, query])
  const dropIndicator = dropPlacement ? <DropIndicator placement={dropPlacement} axis={dropAxis || 'horizontal'} /> : null
  const taskPending = Boolean(pendingAction)
  const gpuWait = task.status === 'queued' ? task.gpu_wait : null
  const gpuCapacity = gpuWait
    && typeof gpuWait.eligible_gpu_count === 'number'
    && typeof gpuWait.requested_gpu_count === 'number'
    ? `${gpuWait.eligible_gpu_count}/${gpuWait.requested_gpu_count} eligible`
    : ''

  return (
    <article
      data-task-card={task.name}
      data-task-card-pinned={task.pinned ? 'true' : 'false'}
      aria-busy={taskPending || undefined}
      className={clsx(
        'group relative rounded-md border bg-surface-raised [contain-intrinsic-size:220px] [content-visibility:auto] transition-[border-color,box-shadow,background-color,opacity,transform] duration-150 ease-out focus-within:border-accent',
        selected ? 'border-accent bg-accent/8 ring-1 ring-accent/20' : 'border-border-subtle hover:border-border',
        task.pinned && !selected && 'border-accent/20',
        !selected && !dragging && 'hover:-translate-y-0.5 hover:shadow-[0_8px_22px_rgba(15,23,42,0.07)]',
        dragging && 'scale-[0.985] border-accent/35 bg-accent/5 opacity-70 shadow-[0_10px_30px_rgba(15,23,42,0.14)] ring-1 ring-accent/35',
        dropPlacement && !dragging && 'border-accent/35 bg-accent/5 ring-1 ring-accent/25',
      )}
    >
      {dropIndicator}
      {!selectMode && (
        <span
          data-task-drag-handle="true"
          aria-hidden="true"
          onPointerDown={event => onPointerDown(task, event)}
          className={clsx(
            'touch-target absolute left-0.5 top-0.5 z-10 flex h-11 w-11 cursor-grab touch-none items-center justify-center rounded-md text-txt-tertiary transition-[background-color,color,opacity] active:cursor-grabbing sm:left-2 sm:top-2 sm:h-7 sm:w-7',
            'group-hover:bg-surface-overlay group-hover:text-txt-secondary',
            dragging && 'bg-accent/10 text-accent',
            (taskPending || reorderDisabled) && 'pointer-events-none opacity-40',
          )}
          title="Drag to set manual order; keyboard users can use Move earlier and Move later"
        >
          <GripVertical className="h-3.5 w-3.5" />
        </span>
      )}
      {selectMode ? (
        <div className="pointer-events-none absolute right-2.5 top-2.5 z-10">
          <SelectionIndicator selected={selected} />
        </div>
      ) : (
        <button
          type="button"
          onClick={() => void onPin(task)}
          disabled={taskPending || reorderDisabled}
          title={task.pinned ? 'Unpin' : 'Pin'}
          aria-label={task.pinned ? `Unpin ${task.name}` : `Pin ${task.name}`}
          aria-busy={pendingAction === 'pin' || undefined}
          className={clsx(
            'touch-target absolute right-0.5 top-0.5 z-10 inline-flex h-11 w-11 items-center justify-center rounded-md p-1 transition-colors hover:bg-surface-overlay focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/35 disabled:cursor-wait disabled:opacity-50 sm:right-2 sm:top-1.5 sm:h-9 sm:w-9',
            task.pinned ? 'text-accent' : 'text-txt-tertiary hover:text-accent'
          )}
        >
          {pendingAction === 'pin'
            ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
            : <Pin className="h-3.5 w-3.5" />}
        </button>
      )}

      <div
        role="button"
        tabIndex={0}
        onClick={() => onCardClick(task)}
        onKeyDown={event => {
          if (!event.repeat && (event.key === 'Enter' || event.key === ' ')) {
            event.preventDefault()
            onCardClick(task)
          }
        }}
        aria-label={selectMode
          ? `${selected ? 'Deselect' : 'Select'} ${task.name}`
          : `Open details for ${task.name}`}
        aria-pressed={selectMode ? selected : undefined}
        className="block w-full rounded-md px-3 pb-2 pt-2.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/35"
      >
        <div className="flex items-center gap-2 pl-11 pr-11 sm:pl-5 sm:pr-7">
          <ManagerStatusBadge status={task.status as TaskStatus} />
          <span className="text-2xs uppercase tracking-[0.16em] text-txt-tertiary">
            {taskKindLabel}
          </span>
          {taskPending && pendingAction !== 'pin' && (
            <span className="ml-auto inline-flex items-center gap-1 text-2xs font-medium text-txt-secondary" role="status">
              <Loader2 className="h-3 w-3 animate-spin" />
              {pendingAction === 'move' ? 'Moving' : pendingAction === 'delete' ? 'Deleting' : 'Updating'}
            </span>
          )}
        </div>

        <div className="mt-1.5 pr-11 text-sm font-semibold text-txt-primary sm:pr-7" title={task.name}>
          {task.name}
        </div>

        <div className="mt-1 min-h-[30px] text-2xs leading-5 text-txt-secondary" title={cardDescription}>
          <div className={clsx('truncate-2', task._load_error && 'text-rose-700 dark:text-rose-300')}>
            {cardDescription}
          </div>
        </div>

        {gpuWait && (
          <div
            className="mt-2 inline-flex max-w-full items-center gap-1.5 rounded-md border border-blue-200 bg-blue-50 px-2 py-1 text-2xs font-medium text-blue-700 dark:border-blue-800 dark:bg-blue-950/45 dark:text-blue-300"
            title={gpuWait.reason || 'Waiting for GPU capacity'}
          >
            <Cpu className="h-3 w-3 flex-none" aria-hidden="true" />
            <span>Waiting for GPU{gpuCapacity ? ` · ${gpuCapacity}` : ''}</span>
          </div>
        )}

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
          <div className="mt-2 inline-flex max-w-full items-center gap-1 rounded-md bg-rose-500/10 px-2 py-1 text-2xs font-medium text-rose-700 dark:text-rose-300" title={task._load_error}>
            <AlertTriangle className="h-3 w-3" />
            <span className="truncate">Task load error</span>
          </div>
        )}
      </div>

      {!selectMode && (
        <div className="mx-3 flex items-center justify-between gap-2 border-t border-border-subtle pb-2 pt-1.5">
          <div className="flex items-center gap-0.5" aria-label={`Reorder ${task.name}`}>
            <TaskIconButton
              label={`Move ${task.name} earlier`}
              title="Move earlier"
              disabled={taskPending || reorderDisabled}
              onClick={() => void onMove(task, -1)}
            >
              <ArrowUp className="h-3.5 w-3.5" />
            </TaskIconButton>
            <TaskIconButton
              label={`Move ${task.name} later`}
              title="Move later"
              disabled={taskPending || reorderDisabled}
              onClick={() => void onMove(task, 1)}
            >
              <ArrowDown className="h-3.5 w-3.5" />
            </TaskIconButton>
          </div>

          <div className="flex items-center gap-0.5">
            {actionBtn && (
              <TaskIconButton
                label={`${actionBtn.label} ${task.name}`}
                title={actionBtn.label}
                disabled={taskPending}
                busy={pendingAction === actionBtn.action}
                className={actionBtn.className}
                onClick={() => void onTaskAction(task, actionBtn.action)}
              >
                {pendingAction === actionBtn.action
                  ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
                  : <actionBtn.icon className="h-3.5 w-3.5" />}
              </TaskIconButton>
            )}
            <TaskIconButton
              label={`View logs for ${task.name}`}
              title="View logs"
              onClick={() => onMonitor(task)}
              className="text-txt-tertiary hover:bg-surface-overlay hover:text-txt-primary"
            >
              <Terminal className="h-3.5 w-3.5" />
            </TaskIconButton>
            <TaskIconButton
              label={`Delete ${task.name}`}
              title="Delete task"
              disabled={taskPending}
              onClick={() => onDelete(task)}
              className="text-rose-700 hover:bg-rose-500/10 dark:text-rose-300"
            >
              <Trash2 className="h-3.5 w-3.5" />
            </TaskIconButton>
          </div>
        </div>
      )}
    </article>
  )
})

function ManagerMetric({
  label,
  value,
  tone = 'neutral',
}: {
  label: string
  value: number | string
  tone?: ManagerMetricTone
}) {
  return (
    <div className={clsx(
      'flex min-w-[7.25rem] items-center justify-between gap-3 rounded-md border px-2.5 py-1.5 text-2xs sm:min-w-0',
      MANAGER_METRIC_STYLES[tone],
    )}>
      <span className="font-medium uppercase tracking-[0.12em] opacity-80">{label}</span>
      <span className="font-semibold tabular-nums">{value}</span>
    </div>
  )
}

function ManagerStatusBadge({ status }: { status: TaskStatus }) {
  return (
    <span className={clsx(
      'inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 text-2xs font-semibold',
      MANAGER_STATUS_STYLES[status],
    )}>
      <span className={clsx('h-1.5 w-1.5 rounded-full', MANAGER_STATUS_DOTS[status])} aria-hidden="true" />
      {STATUS_LABELS[status]}
    </span>
  )
}

function TaskIconButton({
  children,
  label,
  title,
  className,
  disabled = false,
  busy = false,
  onClick,
}: {
  children: ReactNode
  label: string
  title: string
  className?: string
  disabled?: boolean
  busy?: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      aria-busy={busy || undefined}
      title={title}
      className={clsx(
        'touch-target inline-flex h-11 w-11 items-center justify-center rounded-md transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/35 disabled:cursor-not-allowed disabled:opacity-40 sm:h-9 sm:w-9',
        className || 'text-txt-tertiary hover:bg-surface-overlay hover:text-txt-primary',
      )}
    >
      {children}
    </button>
  )
}

function ManagerLoadingState({ columns }: { columns: number }) {
  const count = Math.min(8, Math.max(2, columns * 2))
  return (
    <div role="status" aria-live="polite" aria-label="Loading tasks">
      <div
        className="grid gap-2"
        style={{
          gridTemplateColumns: `repeat(auto-fill, minmax(min(100%, max(15rem, calc((100% - ${(columns - 1) * 8}px) / ${columns}))), 1fr))`,
        }}
      >
        {Array.from({ length: count }, (_, index) => (
          <div key={index} className="animate-pulse rounded-md border border-border-subtle bg-surface-raised p-3">
            <div className="h-5 w-24 rounded bg-surface-overlay" />
            <div className="mt-3 h-4 w-2/3 rounded bg-surface-overlay" />
            <div className="mt-2 h-3 w-full rounded bg-surface-overlay" />
            <div className="mt-1.5 h-3 w-4/5 rounded bg-surface-overlay" />
            <div className="mt-4 h-8 rounded border-t border-border-subtle bg-surface-overlay/60" />
          </div>
        ))}
      </div>
      <span className="sr-only">Loading tasks…</span>
    </div>
  )
}

function ManagerEmptyState({ filtered, onClear }: { filtered: boolean; onClear: () => void }) {
  return (
    <div className="mx-auto flex min-h-72 max-w-md flex-col items-center justify-center rounded-md border border-dashed border-border bg-surface-raised px-6 py-12 text-center">
      <div className="flex h-11 w-11 items-center justify-center rounded-md border border-border-subtle bg-surface-overlay text-txt-secondary">
        <Rows3 className="h-5 w-5" />
      </div>
      <h2 className="mt-4 text-sm font-semibold text-txt-primary">
        {filtered ? 'No matching tasks' : 'No tasks yet'}
      </h2>
      <p className="mt-1.5 max-w-sm text-xs leading-5 text-txt-secondary">
        {filtered
          ? 'Try a broader search or clear the status filter to see more tasks.'
          : 'Create tasks in Generator and they will appear here ready to organize and run.'}
      </p>
      {filtered && (
        <ActionButton className="mt-4 min-h-11 sm:min-h-9" variant="accentTint" onClick={onClear}>
          Clear filters
        </ActionButton>
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
    case 'cancelled':
      return {
        action: 'run' as const,
        icon: Play,
        label: 'Run',
        className: 'text-emerald-700 hover:bg-emerald-500/10 dark:text-emerald-300',
      }
    case 'running':
    case 'queued':
      return {
        action: 'cancel' as const,
        icon: Square,
        label: 'Stop',
        className: 'text-rose-700 hover:bg-rose-500/10 dark:text-rose-300',
      }
    case 'completed':
      return {
        action: 'rerun' as const,
        icon: RotateCcw,
        label: 'Rerun',
        className: 'text-blue-700 hover:bg-blue-500/10 dark:text-blue-300',
      }
    default:
      return null
  }
}
