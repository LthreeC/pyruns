import { t, useTranslation } from '@/i18n'
import {
  memo,
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle, ArrowDown, ArrowDownWideNarrow, ArrowUp, ChevronDown, Cpu, GripVertical, LayoutGrid, List, ListChecks, Loader2, Pin, Play,
  RefreshCw, RotateCcw, Rows3, Search, Square, Terminal, Trash2, X,
} from 'lucide-react'
import clsx from 'clsx'
import { useShallow } from 'zustand/react/shallow'
import { useMonitorStore, useSearchSettingsStore, useTaskStore, useToastStore, useWorkspaceStore } from '@/store'
import { useTaskRefresh } from '@/hooks/useTaskRefresh'
import TaskSearchInput from '@/components/shared/TaskSearchInput'
import TaskSourceFilter from '@/components/shared/TaskSourceFilter'
import TaskExecutionInfo from '@/components/shared/TaskExecutionInfo'
import TaskSearchMatches from '@/components/shared/TaskSearchMatches'
import SelectionIndicator from '@/components/shared/SelectionIndicator'
import Pagination from '@/components/shared/Pagination'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import ActionButton from '@/components/shared/ActionButton'
import CompactSection from '@/components/shared/CompactSection'
import TaskDetailPanel from './TaskDetailPanel'
import type { Task, TaskSortMode } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import { ALL_STATUSES, STATUS_LABELS, taskStatusLabel } from '@/theme/tokens'
import { errorMessage } from '@/utils/errors'
import { taskDetailRefreshKey } from '@/utils/taskSnapshots'
import * as api from '@/api'

const STATUS_OPTIONS = ['All', 'active', ...ALL_STATUSES]
const ACTIVITY_SORT_DESCRIPTION = 'Activity uses the last run’s finish time, its start time, or the task’s creation time.'
const TASK_SORT_OPTIONS: { value: TaskSortMode; label: string; description: string }[] = [
  { value: 'priority', label: 'Smart priority', description: 'Running and queued tasks lead each group. Tasks without a manual position show by recent activity, followed by manually ordered tasks.' },
  { value: 'manual', label: 'Manual order', description: 'Tasks with a saved position come first in each group. Move or drag a task to save its position.' },
  { value: 'activity_desc', label: 'Recent activity', description: ACTIVITY_SORT_DESCRIPTION },
  { value: 'activity_asc', label: 'Oldest activity', description: ACTIVITY_SORT_DESCRIPTION },
  { value: 'name_asc', label: 'Name A-Z', description: 'Sorts numbers in names numerically, so run2 comes before run10.' },
  { value: 'name_desc', label: 'Name Z-A', description: 'Reverses name order, so run10 comes before run2.' },
]
type DragTarget = 'pinned' | 'tasks'
type DragPlacement = 'before' | 'after'
type PendingTaskAction = 'run' | 'rerun' | 'cancel' | 'pin' | 'move' | 'delete'
const DRAG_START_DISTANCE = 8
const REORDER_TASK_LIMIT = 10_000

function isOrderMutation(action: PendingTaskAction) {
  return action === 'pin' || action === 'move'
}

function hasPendingOrderMutation(actions: Map<string, PendingTaskAction>) {
  return [...actions.values()].some(isOrderMutation)
}

const MANAGER_STATUS_DOTS: Record<TaskStatus, string> = {
  pending: 'bg-slate-500',
  queued: 'bg-blue-600 dark:bg-blue-400',
  running: 'bg-amber-600 dark:bg-amber-400',
  completed: 'bg-emerald-600 dark:bg-emerald-400',
  failed: 'bg-rose-600 dark:bg-rose-400',
  cancelled: 'bg-slate-500 dark:bg-slate-400',
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
  useTranslation()
  const {
    tasks, total, statusCounts, offset, limit, query, statusFilter, sortMode, selectedIds, loading, error, columns, viewMode, setViewMode,
    setQuery, searchField, setSearchField, setStatusFilter, setSortMode, setOffset, setColumns, fetchTasks,
    searchOptions, setSearchOptions, searchLimitHit,
    toggleSelect, selectAll, clearSelection,
  } = useTaskStore(useShallow(state => ({
    tasks: state.tasks, total: state.total, statusCounts: state.statusCounts,
    offset: state.offset, limit: state.limit, query: state.query,
    statusFilter: state.statusFilter, sortMode: state.sortMode,
    viewMode: state.viewMode, setViewMode: state.setViewMode,
    selectedIds: state.selectedIds, loading: state.loading, error: state.error, columns: state.columns,
    setQuery: state.setQuery, searchField: state.searchField, setSearchField: state.setSearchField,
    setStatusFilter: state.setStatusFilter, setSortMode: state.setSortMode,
    setOffset: state.setOffset, setColumns: state.setColumns, fetchTasks: state.fetchTasks,
    searchOptions: state.searchOptions, setSearchOptions: state.setSearchOptions,
    searchLimitHit: state.searchLimitHit, toggleSelect: state.toggleSelect,
    selectAll: state.selectAll, clearSelection: state.clearSelection,
  })))
  const maxResults = useSearchSettingsStore(state => state.maxResults)
  const sortOption = TASK_SORT_OPTIONS.find(option => option.value === sortMode)!

  const [maxWorkersInput, setMaxWorkersInput] = useState('2')
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const [deleteTask, setDeleteTask] = useState<Task | null>(null)
  const selectedSources = useTaskStore(state => state.selectedSources)
  const [cancelTask, setCancelTask] = useState<Task | null>(null)
  const [detailTask, setDetailTask] = useState<Task | null>(null)
  const [selectMode, setSelectMode] = useState(false)
  const [viewOptionsOpen, setViewOptionsOpen] = useState(false)
  const viewOptionsRef = useRef<HTMLDivElement>(null)
  const viewOptionsTriggerRef = useRef<HTMLButtonElement>(null)
  const viewOptionsId = useId()
  useEffect(() => {
    if (!viewOptionsOpen) return
    viewOptionsRef.current?.querySelector('select')?.focus()
    const closeOutside = (event: PointerEvent) => {
      if (!viewOptionsRef.current?.contains(event.target as Node)) setViewOptionsOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      setViewOptionsOpen(false)
      viewOptionsTriggerRef.current?.focus()
    }
    document.addEventListener('pointerdown', closeOutside)
    document.addEventListener('keydown', closeOnEscape)
    return () => {
      document.removeEventListener('pointerdown', closeOutside)
      document.removeEventListener('keydown', closeOnEscape)
    }
  }, [viewOptionsOpen])
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
  const detailRequestControllerRef = useRef<AbortController | null>(null)
  const detailTaskRef = useRef(detailTask)
  detailTaskRef.current = detailTask
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const focusTaskName = searchParams.get('task')
  const notify = useToastStore(state => state.notify)
  const workspaceEpoch = useWorkspaceStore(state => state.workspaceEpoch)
  const workspaceKind = useWorkspaceStore(state => state.workspace?.workspace_kind)
  const loadDetailTask = useCallback((task: Task) => {
    const requestId = ++detailRequestSeqRef.current
    const requestedKey = taskDetailRefreshKey(task)
    const requestIsCurrent = () => requestId === detailRequestSeqRef.current
      && workspaceEpoch === useWorkspaceStore.getState().workspaceEpoch
    detailRequestControllerRef.current?.abort()
    const controller = new AbortController()
    detailRequestControllerRef.current = controller
    void api.getTask(task.name, true, false, controller.signal).then(fullTask => {
      if (!requestIsCurrent()) return
      setDetailTask(current => current?.name === task.name && taskDetailRefreshKey(current) === requestedKey
        ? fullTask : current)
    }).catch(err => {
      if (requestIsCurrent() && !controller.signal.aborted) {
        notify({ tone: 'error', title: t('Could not load task details'), detail: errorMessage(err) })
      }
    }).finally(() => {
      if (detailRequestControllerRef.current === controller) detailRequestControllerRef.current = null
    })
  }, [notify, workspaceEpoch])
  const rejectIncompleteReorder = useCallback((page: { has_more: boolean; total: number }) => {
    if (!page.has_more) {
      return false
    }
    notify({
      tone: 'error',
      title: t("Too many tasks to reorder safely"),
      detail: t('This workspace has {{count}} tasks. Reordering is disabled when the complete order cannot be loaded.', { count: page.total.toLocaleString() }),
    })
    return true
  }, [notify])

  const workspaceKey = useWorkspaceStore(state => String(state.workspace?.run_root || ''))
  useTaskRefresh(
    fromDisk => fetchTasks({ background: true, refresh: fromDisk }),
    workspaceKey,
    Boolean(query),
    loading,
  )

  useEffect(() => {
    void fetchTasks()
    return () => useTaskStore.getState().cancelTaskSearch()
  }, [query, searchField, searchOptions, statusFilter, sortMode, offset, fetchTasks, workspaceEpoch, maxResults, selectedSources])

  useEffect(() => {
    detailRequestSeqRef.current += 1
    detailRequestControllerRef.current?.abort()
    detailRequestControllerRef.current = null
    detailTaskRef.current = null
    dragCandidateRef.current = null
    draggedTaskNameRef.current = ''
    dragOverTargetRef.current = null
    dropIntentRef.current = null
    pendingDragPointRef.current = null
    if (dragFrameRef.current != null) {
      window.cancelAnimationFrame(dragFrameRef.current)
      dragFrameRef.current = null
    }
    setDraggedTaskName('')
    setDragOverTarget(null)
    setDropIntent(null)
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
    const requestId = ++detailRequestSeqRef.current
    const requestWorkspaceEpoch = workspaceEpoch
    const controller = new AbortController()
    const requestIsCurrent = () => (
      !cancelled
      && requestId === detailRequestSeqRef.current
      && requestWorkspaceEpoch === useWorkspaceStore.getState().workspaceEpoch
    )
    void api.getTask(focusTaskName, true, false, controller.signal).then(task => {
      if (requestIsCurrent()) {
        setDetailTask(task)
      }
    }).catch(() => {
      if (requestIsCurrent() && !controller.signal.aborted) {
        const next = new URLSearchParams(searchParams)
        next.delete('task')
        setSearchParams(next, { replace: true })
      }
    })

    return () => {
      cancelled = true
      controller.abort()
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
  const mutableCount = tasks.filter(task => task.task_storage !== 'remote').length
  const allPageSelected = mutableCount > 0 && visibleSelectedCount === mutableCount
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
    const current = detailTaskRef.current
    if (!current) return
    const refreshed = tasks.find(task => task.name === current.name)
    if (!refreshed || refreshed === current) return

    const replaced = current.task_identity !== refreshed.task_identity
    const next = replaced ? refreshed : {
      ...current,
      ...refreshed,
      config: current.config,
      config_text: current.config_text,
      records: current.records,
      tracks: current.tracks,
    }
    setDetailTask(next)
    if (taskDetailRefreshKey(current) !== taskDetailRefreshKey(refreshed)) {
      loadDetailTask(next)
    }
  }, [tasks, detailTask?.name, loadDetailTask])

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
        title: skipped.length ? t("Some tasks skipped") : t("Tasks queued"),
        detail: skipped.length
          ? t('{{count}} queued; skipped {{names}}.', { count: result.count, names: skipped.join(', ') })
          : t('{{count}} task(s) scheduled with {{workers}} worker(s).', { count: result.count, workers: maxWorkers }),
      })
    } catch (err) {
      notify({ tone: 'error', title: t("Could not start tasks"), detail: errorMessage(err) })
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
        title: skipped.length ? t("Some tasks could not be deleted") : t("Tasks moved to trash"),
        detail: skipped.length
          ? t('{{count}} deleted; skipped {{names}}.', { count: result.count, names: skipped.join(', ') })
          : t('{{count}} task(s) deleted.', { count: result.count }),
      })
    } catch (err) {
      notify({ tone: 'error', title: t("Could not delete tasks"), detail: errorMessage(err) })
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
      notify({ tone: 'success', title: t("Task moved to trash"), detail: target.name })
    } catch (err) {
      notify({ tone: 'error', title: t("Could not delete task"), detail: errorMessage(err) })
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
          ? t("Cancel requested")
          : waitingForGpu
            ? t("Waiting for GPU capacity")
            : queued
              ? t("Task queued")
              : t("Task started"),
        detail: task.name,
      })
    } catch (err) {
      notify({
        tone: 'error',
        title: action === 'cancel' ? t("Could not cancel task") : t("Could not start task"),
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
      .catch(err => notify({ tone: 'error', title: t("Could not load task logs"), detail: errorMessage(err) }))
    navigate('/monitor')
  }, [navigate, notify])

  const handlePin = useCallback(async (task: Task) => {
    if (!beginTaskAction(task.name, 'pin')) return
    try {
      await api.pinTask(task.name, !task.pinned)
      setTaskActionMessage(task.pinned ? t('Moved {{name}} back to Tasks.', { name: task.name }) : t('Pinned {{name}}.', { name: task.name }))
      await fetchTasks()
    } catch (err) {
      notify({ tone: 'error', title: t("Could not update pin"), detail: errorMessage(err) })
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
    const isCurrentWorkspace = () => workspaceEpoch === useWorkspaceStore.getState().workspaceEpoch
    if (!isCurrentWorkspace()) return
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
      setTaskActionMessage(t('{{name}} is already in this position.', { name: task.name }))
      return
    }
    if (!beginTaskAction(task.name, 'move')) {
      return
    }

    try {
      const allTasks = await api.getTasks({
        sources: ['shared', 'local'],
        limit: REORDER_TASK_LIMIT,
        refresh: false,
        compact: true,
        sort: sortMode,
      })
      if (!isCurrentWorkspace()) return
      if (rejectIncompleteReorder(allTasks)) {
        return
      }
      const items = buildReorderedItems(allTasks.items, task.name, intent)
      const movedItem = items.find(item => item.name === task.name)
      if (!items.length || !movedItem) {
        return
      }
      if (!hasReorderChanges(allTasks.items, items)) {
        setTaskActionMessage(t('{{name}} is already in this position.', { name: task.name }))
        return
      }

      await api.reorderTasks(items)
      if (!isCurrentWorkspace()) return
      if (movedItem.pinned !== task.pinned) {
        setTaskActionMessage(movedItem.pinned ? t('Pinned {{name}}.', { name: task.name }) : t('Moved {{name}} back to Tasks.', { name: task.name }))
      } else {
        setTaskActionMessage(t('Moved {{name}}.', { name: task.name }))
      }
      await refreshManualOrder()
    } catch (err) {
      if (isCurrentWorkspace()) notify({ tone: 'error', title: t("Could not move task"), detail: errorMessage(err) })
    } finally {
      if (isCurrentWorkspace()) finishTaskAction(task.name)
    }
  }, [beginTaskAction, notify, refreshManualOrder, rejectIncompleteReorder, sortMode, tasks, finishTaskAction, workspaceEpoch])

  const handleMoveTask = useCallback(async (task: Task, direction: -1 | 1) => {
    const isCurrentWorkspace = () => workspaceEpoch === useWorkspaceStore.getState().workspaceEpoch
    if (!isCurrentWorkspace()) return
    if (!beginTaskAction(task.name, 'move')) return
    try {
      const visibleSectionTasks = tasks.filter(item => item.task_storage !== 'remote' && Boolean(item.pinned) === Boolean(task.pinned))
      const currentIndex = visibleSectionTasks.findIndex(item => item.name === task.name)
      const targetTask = visibleSectionTasks[currentIndex + direction]
      if (currentIndex < 0 || !targetTask) {
        setTaskActionMessage(
          direction < 0
            ? t('{{name}} is already first among the visible tasks.', { name: task.name })
            : t('{{name}} is already last among the visible tasks.', { name: task.name }),
        )
        return
      }

      const allTasks = await api.getTasks({
        sources: ['shared', 'local'],
        limit: REORDER_TASK_LIMIT,
        refresh: false,
        compact: true,
        sort: sortMode,
      })
      if (!isCurrentWorkspace()) return
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
      if (!isCurrentWorkspace()) return
      setTaskActionMessage(t(direction < 0 ? 'Moved {{name}} earlier.' : 'Moved {{name}} later.', { name: task.name }))
      await refreshManualOrder()
    } catch (err) {
      if (isCurrentWorkspace()) notify({ tone: 'error', title: t("Could not move task"), detail: errorMessage(err) })
    } finally {
      if (isCurrentWorkspace()) finishTaskAction(task.name)
    }
  }, [beginTaskAction, notify, refreshManualOrder, rejectIncompleteReorder, sortMode, tasks, finishTaskAction, workspaceEpoch])

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
    if (selectMode && task.task_storage !== 'remote') {
      toggleSelect(task.name)
      return
    }
    setDetailTask(task)
    loadDetailTask(task)
  }, [selectMode, toggleSelect, loadDetailTask])

  const closeDetailPanel = useCallback(() => {
    detailRequestSeqRef.current += 1
    detailRequestControllerRef.current?.abort()
    detailRequestControllerRef.current = null
    setDetailTask(null)
    if (!searchParams.get('task')) {
      return
    }
    const next = new URLSearchParams(searchParams)
    next.delete('task')
    setSearchParams(next, { replace: true })
  }, [searchParams, setSearchParams])

  return (
    <div className="manager-page flex h-full flex-col overflow-hidden bg-surface-base" data-view={viewMode}>
      <header data-tour="manager-controls" className="workspace-header flex-none border-b border-border-subtle bg-surface-raised px-3 py-3 sm:px-5">
        <div className="manager-heading flex flex-wrap items-center gap-x-6 gap-y-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h1 className="workspace-heading text-lg font-semibold tracking-tight text-txt-primary">{t("Task Manager")}</h1>
              <TaskSourceFilter />
              <button
                type="button"
                aria-label={loading && query
                  ? t("Cancel search")
                  : query
                    ? t("Refresh search results")
                    : t("Refresh tasks")}
                title={loading && query
                  ? t("Cancel search")
                  : query
                    ? t("Refresh search results")
                    : t("Refresh tasks")}
                disabled={loading && !query}
                onClick={() => loading && query
                  ? useTaskStore.getState().cancelTaskSearch()
                  : void fetchTasks({ forceRefresh: true })}
                className="touch-target inline-flex h-11 w-11 flex-none items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 disabled:cursor-wait disabled:opacity-50 sm:h-8 sm:w-8"
              >
                {loading
                  ? query
                    ? <Square aria-hidden="true" className="h-3.5 w-3.5" />
                    : <Loader2 aria-hidden="true" className="h-3.5 w-3.5 animate-spin" />
                  : <RefreshCw aria-hidden="true" className="h-4 w-4" />}
              </button>
            </div>
          </div>

          <div className="manager-search min-w-0">
            <TaskSearchInput
              compact minimal
              workspaceKind={workspaceKind}
              value={query}
              onChange={setQuery}
              searchField={searchField}
              onSearchFieldChange={setSearchField}
              searchOptions={searchOptions}
              onSearchOptionsChange={setSearchOptions}
              searching={loading}
              limitHit={searchLimitHit}
              onRefresh={() => void fetchTasks({ forceRefresh: true })}
              onCancel={() => useTaskStore.getState().cancelTaskSearch()}
              ariaLabel={t("Search tasks")}
            />
          </div>
          <ActionButton
            icon={selectMode ? <X aria-hidden="true" className="size-4" /> : <ListChecks aria-hidden="true" className="size-4" />}
            variant={selectMode ? 'secondary' : 'accentTint'}
            className={clsx('manager-select-action shrink-0', !selectMode && 'border border-accent/20')}
            aria-label={t(selectMode ? 'Cancel' : 'Select tasks')}
            aria-expanded={selectMode}
            aria-controls={selectMode ? 'manager-selection-actions' : undefined}
            disabled={Boolean(bulkAction) || (!selectMode && (mutableCount === 0 || anyTaskActionPending))}
            title={anyTaskActionPending ? t('Wait for the current task action to finish') : t(selectMode ? 'Cancel' : 'Select tasks')}
            onClick={() => selectMode ? exitSelectMode() : setSelectMode(true)}
          >
            {t(selectMode ? 'Cancel' : 'Select tasks')}
          </ActionButton>
        </div>

        <div className="manager-toolbar flex flex-wrap items-center justify-between gap-2">
          <div
            className="manager-status-tabs flex min-w-0 items-center gap-1"
            role="group"
            aria-label={t("Workspace task totals")}
            title={t("Workspace totals are independent of the current search, status filter, and page.")}
          >
            <ManagerMetric label={t("All tasks")} value={summary?.total ?? '—'} selected={statusFilter === 'All'} onClick={() => setStatusFilter('All')} />
            <ManagerMetric label={t("Active")} value={summary?.active ?? '—'} selected={statusFilter === 'active'} onClick={() => setStatusFilter('active')} />
            <ManagerMetric label={t("Done")} value={summary?.completed ?? '—'} selected={statusFilter === 'completed'} onClick={() => setStatusFilter('completed')} />
            <ManagerMetric label={t("Failed")} value={summary?.failed ?? '—'} selected={statusFilter === 'failed'} onClick={() => setStatusFilter('failed')} />
          </div>

          <div className="manager-view-tools flex items-center gap-3">
            <div className="manager-view-options relative" ref={viewOptionsRef} onBlur={event => {
              if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) setViewOptionsOpen(false)
            }}>
              <button type="button" ref={viewOptionsTriggerRef} aria-label={t('View options')} aria-haspopup="dialog" aria-expanded={viewOptionsOpen} aria-controls={viewOptionsOpen ? viewOptionsId : undefined}
                className="manager-options-trigger touch-target inline-flex min-h-11 items-center gap-2 rounded-md px-2 text-xs text-txt-secondary transition-colors hover:bg-surface-overlay aria-expanded:bg-accent/10 aria-expanded:text-accent-ink sm:min-h-9"
                onClick={() => setViewOptionsOpen(open => !open)}>
                <ArrowDownWideNarrow aria-hidden="true" className="size-4" />
                <span>{!['All', 'active', 'completed', 'failed'].includes(statusFilter) && `${t(STATUS_LABELS[statusFilter as TaskStatus])} · `}{t(sortOption.label)}</span>
                <ChevronDown aria-hidden="true" className="size-3.5" />
              </button>
              {viewOptionsOpen && (
                <div id={viewOptionsId} role="dialog" aria-label={t('View options')} className="manager-options-panel">
                  <h2 className="mb-2 text-sm font-semibold text-txt-primary">{t('View options')}</h2>
                  <ManagerViewOption id={`${viewOptionsId}-sort`} label={t('Sort by')}>
                    <select
                      id={`${viewOptionsId}-sort`}
                      value={sortMode}
                      onChange={event => setSortMode(event.target.value as TaskSortMode)}
                      aria-label={t('Sort task cards')}
                      aria-describedby={`${viewOptionsId}-sort-description`}
                    >
                      {TASK_SORT_OPTIONS.map(option => (
                        <option key={option.value} value={option.value}>{t(option.label)}</option>
                      ))}
                    </select>
                  </ManagerViewOption>
                  <p id={`${viewOptionsId}-sort-description`} className="px-0.5 pb-2 pt-1 text-2xs leading-5 text-txt-secondary">
                    {t('Pinned tasks always appear first.')} {t(sortOption.description)}
                  </p>
                  <ManagerViewOption id={`${viewOptionsId}-status`} label={t('Status')}>
                    <select
                      id={`${viewOptionsId}-status`}
                      value={statusFilter}
                      onChange={event => setStatusFilter(event.target.value)}
                      aria-label={t('Filter tasks by status')}
                    >
                      {STATUS_OPTIONS.map(option => (
                        <option key={option} value={option}>
                          {option === 'All' ? t('All statuses') : option === 'active' ? t('Active') : t(STATUS_LABELS[option as TaskStatus])}
                        </option>
                      ))}
                    </select>
                  </ManagerViewOption>
                  {viewMode === 'cards' && (
                    <ManagerViewOption id={`${viewOptionsId}-columns`} label={t('Cards per row')}>
                      <select
                        id={`${viewOptionsId}-columns`}
                        value={columns}
                        onChange={event => setColumns(Number(event.target.value))}
                        aria-label={t('Maximum cards per row')}
                        title={t('Maximum cards per row; narrow windows reduce the count automatically')}
                      >
                        {[1, 2, 3, 4, 5, 6, 7, 8].map(count => (
                          <option key={count} value={count}>{t('{{count}} column(s)', { count })}</option>
                        ))}
                      </select>
                    </ManagerViewOption>
                  )}
                </div>
              )}
            </div>
            <div role="group" aria-label={t('Task view')} className="manager-view-toggle flex items-center divide-x divide-border-subtle rounded-md border border-border-subtle">
              {([{ mode: 'cards', label: 'Card view', icon: LayoutGrid }, { mode: 'list', label: 'List view', icon: List }] as const).map(({ mode, label, icon: Icon }) => (
                <button key={mode} type="button" aria-label={t(label)} title={t(label)} aria-pressed={viewMode === mode}
                  onClick={() => setViewMode(mode)} className={clsx('touch-target flex h-10 w-10 items-center justify-center sm:h-8 sm:w-9', viewMode === mode ? 'bg-accent/10 text-accent-ink' : 'text-txt-secondary hover:bg-surface-overlay hover:text-txt-primary')}>
                  <Icon aria-hidden="true" className="h-4 w-4" />
                </button>
              ))}
            </div>
          </div>
        </div>

        {taskActionMessage && (
          <div
            role="status"
            aria-live="polite"
            className="mt-2 border-l-2 border-accent/40 pl-2.5 text-xs text-txt-secondary"
            title={taskActionMessage}
          >
            {taskActionMessage}
          </div>
        )}

        {selectMode && (
          <div
            id="manager-selection-actions"
            className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border-subtle pt-2"
            role="region"
            aria-label={t("Current page selection actions")}
          >
            <div className="text-xs font-medium tabular-nums text-txt-primary" aria-live="polite"
              title={t("Selection is page-scoped and resets when you change the page, search, or status filter.")}>
                {t('{{selected}} of {{total}} selected on this page', { selected: visibleSelectedCount, total: mutableCount })}
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <ActionButton
                icon={<SelectionIndicator selected={allPageSelected} />}
                variant="ghost"
                className="min-h-11 sm:min-h-9"
                disabled={Boolean(bulkAction)}
                onClick={() => (allPageSelected ? clearSelection() : selectAll())}
              >
                {allPageSelected ? t("Clear page") : t("Select page")}
              </ActionButton>

              <label className="touch-target flex min-h-11 items-center gap-1.5 rounded-md border border-border-subtle bg-surface-raised px-2.5 text-2xs text-txt-secondary sm:min-h-9">
                {t("Workers")}
                <input
                  type="number"
                  min={1}
                  max={32}
                  value={maxWorkersInput}
                  disabled={Boolean(bulkAction)}
                  onChange={event => setMaxWorkersInput(event.target.value)}
                  onBlur={() => setMaxWorkersInput(current => String(normalizeWorkerInput(current)))}
                  aria-label={t("Maximum parallel workers")}
                  className="w-10 bg-transparent text-xs tabular-nums text-txt-primary outline-none disabled:opacity-50"
                />
              </label>

              <ActionButton
                icon={bulkAction === 'run' ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Play className="h-3.5 w-3.5" />}
                variant="primary"
                className="min-h-11 sm:min-h-9"
                onClick={handleRunSelected}
                disabled={visibleSelectedCount === 0 || Boolean(bulkAction)}
                aria-busy={bulkAction === 'run' || undefined}
              >
                {t('Run ({{count}})', { count: visibleSelectedCount })}
              </ActionButton>

              <ActionButton
                icon={<Trash2 className="h-3.5 w-3.5" />}
                variant="ghost"
                className="min-h-11 text-rose-700 dark:text-rose-300 sm:min-h-9"
                onClick={() => visibleSelectedCount > 0 && setDeleteConfirm(true)}
                disabled={visibleSelectedCount === 0 || Boolean(bulkAction)}
              >
                {t("Delete")}
              </ActionButton>

            </div>
          </div>
        )}
      </header>

      <section
        className="manager-tasks-content min-h-0 flex-1 overflow-y-auto p-3 sm:p-5"
        aria-label={t("Task list")}
        aria-busy={loading || undefined}
      >
        {error && (
          <div
            role="alert"
            className="mb-3 flex flex-col gap-2 rounded-md border border-rose-200 bg-rose-50 px-3 py-2.5 text-rose-800 dark:border-rose-800 dark:bg-rose-950/45 dark:text-rose-300 sm:flex-row sm:items-center"
          >
            <AlertTriangle className="h-4 w-4 flex-none" />
            <div className="min-w-0 flex-1 text-xs">
              <span className="font-semibold">{t("Tasks could not be refreshed.")}</span>{' '}
              <span className="break-words">{error}</span>
            </div>
            <ActionButton
              icon={<RefreshCw className="h-3.5 w-3.5" />}
              variant="ghost"
              className="min-h-11 sm:min-h-9"
              disabled={loading}
              onClick={() => void fetchTasks({ forceRefresh: true })}
            >
              {t("Retry")}
            </ActionButton>
          </div>
        )}

        {loading && tasks.length === 0 ? (
          <ManagerLoadingState columns={viewMode === 'list' ? 1 : columns} />
        ) : tasks.length === 0 ? (
          error ? null : (
            <ManagerEmptyState
              filtered={Boolean(query) || statusFilter !== 'All' || selectedSources !== null}
              onClear={() => {
                setQuery('')
                setStatusFilter('All')
                if (selectedSources !== null) useTaskStore.getState().setSelectedSources(null)
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
                  title={t("Pinned Tasks")}
                  subtitle={dragOverTarget === 'pinned' ? t("Drop to pin or reorder") : undefined}
                  count={pinnedTasks.length}
                  icon={<Pin className="h-3.5 w-3.5 text-accent" />}
                  accent
                  bodyClassName="pt-1"
                >
                  {pinnedTasks.length === 0 ? (
                    <div className="px-2 py-5 text-center text-xs font-medium text-accent">
                      {t("Drop here to pin")}
                    </div>
                  ) : (
                    <TaskGrid
                      tasks={pinnedTasks}
                      columns={viewMode === 'list' ? 1 : columns}
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
              title={t("Tasks")}
              subtitle={dragOverTarget === 'tasks' ? t("Drop to reorder") : t('{{count}} on this page · {{total}} matching', { count: otherTasks.length, total })}
              icon={<Rows3 className="h-3.5 w-3.5 text-txt-tertiary" />}
              bodyClassName="pt-1"
            >
              <TaskGrid
                tasks={otherTasks}
                columns={viewMode === 'list' ? 1 : columns}
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

      <footer className="flex flex-none items-center gap-3 border-t border-border-subtle bg-surface-raised px-3 py-2 sm:px-4">
        <span className="min-w-0 flex-1 truncate text-2xs text-txt-secondary">
          {selectMode && visibleSelectedCount > 0 ? t('{{count}} selected on page · ', { count: visibleSelectedCount }) : ''}
          {tasks.length > 0 ? t('Showing {{start}}–{{end}} of ', { start: offset + 1, end: offset + tasks.length }) : ''}
          {t('{{count}} matching task(s)', { count: total })}
        </span>
        <Pagination total={total} offset={offset} limit={limit} onOffsetChange={setOffset} />
      </footer>

      <ConfirmDialog
        open={deleteConfirm}
        title={t("Delete Tasks")}
        description={activeSelectedCount > 0
          ? t('Stop {{active}} active task(s) and move all {{selected}} selected tasks from this page to trash?', { active: activeSelectedCount, selected: visibleSelectedCount })
          : t('Move {{count}} selected task(s) from this page to trash?', { count: visibleSelectedCount })}
        confirmLabel={t("Delete")}
        confirmVariant="danger"
        onConfirm={handleDeleteSelected}
        onCancel={() => setDeleteConfirm(false)}
      />

      <ConfirmDialog
        open={Boolean(deleteTask)}
        title={deleteTask && (deleteTask.status === 'running' || deleteTask.status === 'queued') ? t("Stop and Delete Task") : t("Delete Task")}
        description={deleteTask
          ? deleteTask.status === 'running' || deleteTask.status === 'queued'
            ? t("Stop '{{name}}' and move it to trash?", { name: deleteTask.name })
            : t("Move '{{name}}' to trash?", { name: deleteTask.name })
          : ''}
        confirmLabel={t("Delete")}
        confirmVariant="danger"
        onConfirm={handleDeleteTask}
        onCancel={() => setDeleteTask(null)}
      />

      <ConfirmDialog
        open={Boolean(cancelTask)}
        title={t("Stop active task?")}
        description={cancelTask ? t("Request cancellation for '{{name}}'? Its process tree will be stopped.", { name: cancelTask.name }) : ''}
        confirmLabel={t("Stop Task")}
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
            useTaskStore.setState(state => ({
              tasks: state.tasks.map(task => task.name === updatedTask.name ? { ...task, ...updatedTask } : task),
            }))
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
  useTranslation()
  if (tasks.length === 0) {
    return <div className="px-2 py-5 text-center text-2xs text-txt-tertiary">{t("No tasks in this section")}</div>
  }

  return (
    <div
      data-task-grid="true"
      data-task-grid-columns={columns}
      className="manager-task-grid grid gap-2"
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
  useTranslation()
  const actionBtn = getActionButton(task)
  const readOnly = task.task_storage === 'remote'
  const displayName = task.display_name ?? task.name
  const accessibleName = task.task_source_host ? `${displayName} · ${task.task_source_host}` : displayName
  const folderName = task.dir.split(/[\\/]/).pop() || task.dir
  const createdAtLabel = task.created_at?.replace(/^(\d{4}-\d{2}-\d{2})[_T](\d{2})[-:](\d{2}).*$/, '$1 · $2:$3')
  const taskKindLabel = task.task_kind === 'shell' ? 'shell' : 'python'
  const cardDescription = task._load_error || task.preview_text || t("No preview available.")
  const dropIndicator = dropPlacement ? <DropIndicator placement={dropPlacement} axis={dropAxis || 'horizontal'} /> : null
  const taskPending = Boolean(pendingAction)
  const gpuWait = task.status === 'queued' ? task.gpu_wait : null
  const gpuCapacity = gpuWait
    && typeof gpuWait.eligible_gpu_count === 'number'
    && typeof gpuWait.requested_gpu_count === 'number'
    ? t('{{eligible}}/{{requested}} eligible', { eligible: gpuWait.eligible_gpu_count, requested: gpuWait.requested_gpu_count })
    : ''

  return (
    <article
      data-task-card={task.name}
      data-task-card-pinned={task.pinned ? 'true' : 'false'}
      data-selection-mode={selectMode}
      aria-busy={taskPending || undefined}
      className={clsx(
        'task-card group relative flex flex-col border bg-surface-raised [contain-intrinsic-size:220px] [content-visibility:auto] transition-colors duration-150 ease-out focus-within:border-accent',
        selected ? 'border-accent bg-accent/8 ring-1 ring-accent/20' : 'border-border-subtle hover:border-border',
        task.pinned && !selected && 'border-accent/20',
        dragging && 'scale-[0.985] border-accent/35 bg-accent/5 opacity-70 shadow-[0_10px_30px_rgba(15,23,42,0.14)] ring-1 ring-accent/35',
        dropPlacement && !dragging && 'border-accent/35 bg-accent/5 ring-1 ring-accent/25',
      )}
    >
      {dropIndicator}
      {!selectMode && !readOnly && (
        <span
          data-task-drag-handle="true"
          aria-hidden="true"
          onPointerDown={event => onPointerDown(task, event)}
          className={clsx(
            'task-card-drag touch-target absolute left-0.5 top-0.5 z-10 flex h-11 w-11 cursor-grab touch-none items-center justify-center rounded-md text-txt-tertiary transition-[background-color,color,opacity] active:cursor-grabbing sm:left-2 sm:top-2 sm:h-7 sm:w-7',
            'group-hover:bg-surface-overlay group-hover:text-txt-secondary',
            dragging && 'bg-accent/10 text-accent',
            (taskPending || reorderDisabled) && 'pointer-events-none opacity-40',
          )}
          title={t("Drag to set manual order; keyboard users can use Move earlier and Move later")}
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
          disabled={readOnly || taskPending || reorderDisabled}
          title={task.pinned ? t("Unpin") : t("Pin")}
          aria-label={t(task.pinned ? 'Unpin {{name}}' : 'Pin {{name}}', { name: accessibleName })}
          aria-busy={pendingAction === 'pin' || undefined}
          className={clsx(
            'task-card-pin touch-target absolute right-0.5 top-0.5 z-10 inline-flex h-11 w-11 items-center justify-center rounded-md p-1 transition-colors hover:bg-surface-overlay focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/35 disabled:cursor-wait disabled:opacity-50 sm:right-2 sm:top-1.5 sm:h-9 sm:w-9',
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
          ? t('{{action}} {{name}}', { action: selected ? t("Deselect") : t("Select"), name: accessibleName })
          : t('Open details for {{name}}', { name: accessibleName })}
        aria-pressed={selectMode ? selected : undefined}
        className="task-card-main block w-full min-w-0 flex-1 rounded-xl px-3 pb-3 pt-2.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/35"
      >
        <div className="task-card-status flex items-center gap-2 pl-11 pr-11 sm:pl-5 sm:pr-7">
          <ManagerStatusBadge task={task} />
          <span className="text-2xs uppercase tracking-[0.08em] text-txt-tertiary">
            {taskKindLabel}
          </span>
          {taskPending && pendingAction !== 'pin' && (
            <span className="ml-auto inline-flex items-center gap-1 text-2xs font-medium text-txt-secondary" role="status">
              <Loader2 className="h-3 w-3 animate-spin" />
              {pendingAction === 'move' ? t("Moving") : pendingAction === 'delete' ? t("Deleting") : t("Updating")}
            </span>
          )}
        </div>

        <div className="task-card-name mt-3 text-sm font-semibold leading-6 text-txt-primary [overflow-wrap:anywhere]" title={displayName}>
          {displayName}
        </div>

        <div className="task-card-preview mt-2 border-l-2 border-border pl-2.5 font-mono text-2xs leading-5 text-txt-secondary" title={cardDescription}>
          <div className={clsx('truncate-2 whitespace-pre-wrap [overflow-wrap:anywhere]', task._load_error && 'text-rose-700 dark:text-rose-300')}>
            {cardDescription}
          </div>
        </div>

        <TaskExecutionInfo task={task} className="task-card-execution mt-2" />
        {gpuWait && (
          <div
            className="task-card-gpu mt-2 inline-flex max-w-full items-center gap-1.5 rounded-md border border-blue-200 bg-blue-50 px-2 py-1 text-2xs font-medium text-blue-700 dark:border-blue-800 dark:bg-blue-950/45 dark:text-blue-300"
            title={gpuWait.reason || t("Waiting for GPU capacity")}
          >
            <Cpu className="h-3 w-3 flex-none" aria-hidden="true" />
            <span>{t("Waiting for GPU")}{gpuCapacity ? ` · ${gpuCapacity}` : ''}</span>
          </div>
        )}

        <div className="task-card-meta mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 text-2xs text-txt-tertiary">
          <span title={task.created_at}>{createdAtLabel}</span>
          {task.run_index > 0 && <span>{t('Run #{{index}}', { index: task.run_index })}</span>}
          {folderName !== displayName && <span className="truncate" title={task.dir}>{folderName}</span>}
        </div>


        {task._load_error && (
          <div className="task-card-error mt-2 inline-flex max-w-full items-center gap-1 rounded-md bg-rose-500/10 px-2 py-1 text-2xs font-medium text-rose-700 dark:text-rose-300" title={task._load_error}>
            <AlertTriangle className="h-3 w-3" />
            <span className="truncate">{t("Task load error")}</span>
          </div>
        )}
      </div>

      {query && <div className="task-card-matches mx-3 mb-2 border-t border-border-subtle pt-1">
        <TaskSearchMatches task={task} onSelect={() => onCardClick(task)} exportMode={selectMode} action={selectMode ? t("Select") : t("View")} />
      </div>}
      {!selectMode && (
        <div className="task-card-actions mx-3 flex items-center justify-between gap-2 border-t border-border-subtle pb-2 pt-1.5">
          <div className="flex items-center gap-0.5" aria-label={t('Reorder {{name}}', { name: task.name })}>
            <TaskIconButton
              label={t('Move {{name}} earlier', { name: task.name })}
              title={t("Move earlier")}
              disabled={readOnly || taskPending || reorderDisabled}
              onClick={() => void onMove(task, -1)}
            >
              <ArrowUp className="h-3.5 w-3.5" />
            </TaskIconButton>
            <TaskIconButton
              label={t('Move {{name}} later', { name: task.name })}
              title={t("Move later")}
              disabled={readOnly || taskPending || reorderDisabled}
              onClick={() => void onMove(task, 1)}
            >
              <ArrowDown className="h-3.5 w-3.5" />
            </TaskIconButton>
          </div>

          <div className="flex items-center gap-0.5">
            {actionBtn && (
              <TaskIconButton
                label={t('{{action}} {{name}}', { action: t(actionBtn.label), name: task.name })}
                title={t(actionBtn.label)}
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
              label={t('View logs for {{name}}', { name: accessibleName })}
              title={t("View logs")}
              onClick={() => onMonitor(task)}
              className="text-txt-tertiary hover:bg-surface-overlay hover:text-txt-primary"
            >
              <Terminal className="h-3.5 w-3.5" />
            </TaskIconButton>
            <TaskIconButton
              label={t('Delete {{name}}', { name: task.name })}
              title={t("Delete task")}
              disabled={readOnly || taskPending}
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

function ManagerViewOption({ id, label, children }: { id: string; label: string; children: ReactNode }) {
  return (
    <div className="manager-option-row">
      <label htmlFor={id} className="text-xs font-medium text-txt-secondary">{label}</label>
      <div className="relative min-w-0">
        {children}
        <ChevronDown aria-hidden="true" className="pointer-events-none absolute right-2.5 top-1/2 size-3.5 -translate-y-1/2 text-txt-secondary" />
      </div>
    </div>
  )
}

function ManagerMetric({
  label,
  value,
  selected,
  onClick,
}: {
  label: string
  value: number | string
  selected: boolean
  onClick: () => void
}) {
  useTranslation()
  const countId = useId()
  return (
    <button type="button" onClick={onClick} aria-pressed={selected} aria-label={t('Filter tasks: {{status}}', { status: label })} aria-describedby={countId} className={clsx(
      'manager-status-tab touch-target flex min-h-11 shrink-0 items-center justify-between gap-2 rounded-lg px-3 py-1.5 text-xs transition-colors sm:min-h-9',
      selected ? 'bg-surface-overlay text-txt-primary' : 'text-txt-secondary hover:bg-surface-overlay',
    )}>
      <span className="font-medium">{t(label)}</span>
      <span id={countId} className="text-txt-tertiary tabular-nums">{value}</span>
    </button>
  )
}

function ManagerStatusBadge({ task }: { task: Task }) {
  const status = task.status
  useTranslation()
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium text-txt-primary">
      <span className={clsx('h-1.5 w-1.5 rounded-full', MANAGER_STATUS_DOTS[status])} aria-hidden="true" />
      {t(taskStatusLabel(task))}
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
  useTranslation()
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
  useTranslation()
  const count = Math.min(8, Math.max(2, columns * 2))
  return (
    <div role="status" aria-live="polite" aria-label={t("Loading tasks")}>
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
      <span className="sr-only">{t("Loading tasks…")}</span>
    </div>
  )
}

function ManagerEmptyState({ filtered, onClear }: { filtered: boolean; onClear: () => void }) {
  useTranslation()
  return (
    <div className="mx-auto flex min-h-72 max-w-md flex-col items-center justify-center rounded-md border border-dashed border-border bg-surface-raised px-6 py-12 text-center">
      <div className="flex h-11 w-11 items-center justify-center rounded-md border border-border-subtle bg-surface-overlay text-txt-secondary">
        <Rows3 className="h-5 w-5" />
      </div>
      <h2 className="mt-4 text-sm font-semibold text-txt-primary">
        {filtered ? t("No matching tasks") : t("No tasks yet")}
      </h2>
      <p className="mt-1.5 max-w-sm text-xs leading-5 text-txt-secondary">
        {filtered
          ? t('Try a broader search or clear the status filter to see more tasks.')
          : t('Create tasks in Generator and they will appear here ready to organize and run.')}
      </p>
      {filtered && (
        <ActionButton className="mt-4 min-h-11 sm:min-h-9" variant="accentTint" onClick={onClear}>
          {t("Clear filters")}
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


function getActionButton(task: Task) {
  if (task._load_error || task.task_storage === 'remote') {
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
