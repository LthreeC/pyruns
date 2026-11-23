import { t, useTranslation } from '@/i18n'
import {
  memo,
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  AlertTriangle, ArrowDownWideNarrow, ChevronDown,
  Cpu, GripVertical, ListChecks, Loader2, Pin, Play,
  RefreshCw, RotateCcw, Rows3, Search, Square, Terminal, Trash2, X,
} from 'lucide-react'
import clsx from 'clsx'
import { useShallow } from 'zustand/react/shallow'
import { MANAGER_TASK_PAGE_SIZE, useMonitorStore, useSearchSettingsStore, useTaskStore, useToastStore, useWorkspaceStore } from '@/store'
import { useTaskRefresh } from '@/hooks/useTaskRefresh'
import TaskSearchInput from '@/components/shared/TaskSearchInput'
import TaskSourceFilter from '@/components/shared/TaskSourceFilter'
import TaskExecutionInfo from '@/components/shared/TaskExecutionInfo'
import TaskStatusDot from '@/components/shared/TaskStatusDot'
import TaskSearchMatches from '@/components/shared/TaskSearchMatches'
import SelectionIndicator from '@/components/shared/SelectionIndicator'
import Pagination from '@/components/shared/Pagination'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import ActionButton from '@/components/shared/ActionButton'
import CompactSection from '@/components/shared/CompactSection'
import TaskDetailPanel from './TaskDetailPanel'
import type { Task, TaskSortMode } from '@/types'
import { ALL_STATUSES, STATUS_LABELS, taskStatusLabel } from '@/theme/tokens'
import { errorMessage } from '@/utils/errors'
import { taskDetailRefreshKey } from '@/utils/taskSnapshots'
import { canStopTask, describeTaskStop, isActiveTask, stopOnExecutionDevice, taskStopFeedback } from '@/utils/taskStop'
import * as api from '@/api'

const STATUS_OPTIONS = ['All', 'active', ...ALL_STATUSES] as const
const TASK_SORT_OPTIONS: { value: TaskSortMode; label: string }[] = [
  { value: 'priority', label: 'Active first' },
  { value: 'manual', label: 'Manual order' },
  { value: 'activity_desc', label: 'Recent activity' },
  { value: 'activity_asc', label: 'Oldest activity' },
  { value: 'name_asc', label: 'Name A-Z' },
  { value: 'name_desc', label: 'Name Z-A' },
]
type DragTarget = 'pinned' | 'tasks'
type DragPlacement = 'before' | 'after'
type KeyboardDragAction = 'toggle' | 'cancel' | 'previous' | 'next'
type PendingTaskAction = 'run' | 'rerun' | 'cancel' | 'pin' | 'move' | 'delete'
const DRAG_START_DISTANCE = 8
const REORDER_TASK_LIMIT = 10_000
const TASK_DETAIL_TIMEOUT_MS = 10_000

function isOrderMutation(action: PendingTaskAction) {
  return action === 'pin' || action === 'move'
}

function hasPendingOrderMutation(actions: Map<string, PendingTaskAction>) {
  return [...actions.values()].some(isOrderMutation)
}

interface DropIntent {
  target: DragTarget
  targetName: string
  placement: DragPlacement
  axis: 'horizontal' | 'vertical'
}

interface DragCandidate {
  taskName: string
  pointerId: number
  startX: number
  startY: number
  gridAxes: WeakMap<HTMLElement, DropIntent['axis']>
}

function visibleCardPositions(content: HTMLElement) {
  const viewport = content.getBoundingClientRect()
  const positions = new Map<string, { card: HTMLElement; left: number; top: number }>()
  for (const card of content.querySelectorAll<HTMLElement>('[data-task-card]')) {
    const rect = card.getBoundingClientRect()
    if (rect.bottom > viewport.top && rect.top < viewport.bottom) {
      positions.set(card.dataset.taskCard!, { card, left: rect.left - viewport.left, top: rect.top - viewport.top })
    }
  }
  return positions
}

function getDropTargetFromElement(element: Element): DragTarget | null {
  const target = element.closest<HTMLElement>('[data-task-drop-target]')
  const value = target?.dataset.taskDropTarget
  return value === 'pinned' || value === 'tasks' ? value : null
}

function gridDropAxis(element: HTMLElement, gridAxes?: DragCandidate['gridAxes']): DropIntent['axis'] {
  const grid = element.closest<HTMLElement>('[data-task-grid]')
  if (!grid) return 'vertical'
  const cachedAxis = gridAxes?.get(grid)
  if (cachedAxis) return cachedAxis
  const columns = window.getComputedStyle(grid).gridTemplateColumns.split(/\s+/).filter(track => Number.parseFloat(track) > 0)
  const axis = columns.length <= 1 ? 'vertical' : 'horizontal'
  gridAxes?.set(grid, axis)
  return axis
}

function getCardDropPlacement(card: HTMLElement, clientX: number, clientY: number, gridAxes: DragCandidate['gridAxes']): Pick<DropIntent, 'placement' | 'axis'> {
  const rect = card.getBoundingClientRect()
  const axis = gridDropAxis(card, gridAxes)
  const placement = axis === 'vertical'
    ? (clientY < rect.top + rect.height / 2 ? 'before' : 'after')
    : (clientX < rect.left + rect.width / 2 ? 'before' : 'after')
  return { placement, axis }
}

function getPointerDropIntent(clientX: number, clientY: number, gridAxes: DragCandidate['gridAxes']): DropIntent | null {
  const element = document.elementFromPoint(clientX, clientY)
  if (!element) return null

  const card = element.closest<HTMLElement>('[data-task-card]')
  if (card) {
    const target = getDropTargetFromElement(card)
    if (target) {
      return {
        target,
        targetName: card.dataset.taskCard || '',
        ...getCardDropPlacement(card, clientX, clientY, gridAxes),
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
    tasks, total, statusCounts, offset, query, statusFilter, sortMode, selectedIds, loading, error,
    setQuery, searchField, setSearchField, setStatusFilter, setSortMode, setOffset, fetchTasks,
    searchOptions, setSearchOptions, searchLimitHit,
    toggleSelect, selectAll, clearSelection,
  } = useTaskStore(useShallow(state => ({
    tasks: state.tasks, total: state.total, statusCounts: state.statusCounts,
    offset: state.offset, query: state.query,
    statusFilter: state.statusFilter, sortMode: state.sortMode,
    selectedIds: state.selectedIds, loading: state.loading, error: state.error,
    setQuery: state.setQuery, searchField: state.searchField, setSearchField: state.setSearchField,
    setStatusFilter: state.setStatusFilter, setSortMode: state.setSortMode,
    setOffset: state.setOffset, fetchTasks: state.fetchTasks,
    searchOptions: state.searchOptions, setSearchOptions: state.setSearchOptions,
    searchLimitHit: state.searchLimitHit, toggleSelect: state.toggleSelect,
    selectAll: state.selectAll, clearSelection: state.clearSelection,
  })))
  const maxResults = useSearchSettingsStore(state => state.maxResults)

  const [maxWorkersInput, setMaxWorkersInput] = useState('2')
  const [deleteConfirm, setDeleteConfirm] = useState(false)
  const [deleteTask, setDeleteTask] = useState<Task | null>(null)
  const selectedSources = useTaskStore(state => state.selectedSources)
  const [cancelTask, setCancelTask] = useState<Task | null>(null)
  const [detailTask, setDetailTask] = useState<Task | null>(null)
  const [selectMode, setSelectMode] = useState(false)
  const [draggedTaskName, setDraggedTaskName] = useState('')
  const [dropIntent, setDropIntent] = useState<DropIntent | null>(null)
  const dragOverTarget = dropIntent?.target ?? null
  const [taskActionMessage, setTaskActionMessage] = useState('')
  const [pendingTaskActions, setPendingTaskActions] = useState<Map<string, PendingTaskAction>>(() => new Map())
  const [bulkAction, setBulkAction] = useState<'run' | 'delete' | null>(null)
  const dragCandidateRef = useRef<DragCandidate | null>(null)
  const draggedTaskNameRef = useRef('')
  const dropIntentRef = useRef<DropIntent | null>(null)
  const pendingDragPointRef = useRef<{ clientX: number; clientY: number } | null>(null)
  const dragFrameRef = useRef<number | null>(null)
  const tasksContentRef = useRef<HTMLElement>(null)
  const orderMotionRef = useRef<{
    view: string; tasks: Task[]; scrollTop: number; positions: ReturnType<typeof visibleCardPositions>
  } | null>(null)
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
  const orderView = JSON.stringify([workspaceEpoch, query, statusFilter, offset, selectedSources])
  const applyDropIntent = useCallback((intent: DropIntent | null) => {
    if (!sameDropIntent(dropIntentRef.current, intent)) setDropIntent(intent)
    dropIntentRef.current = intent
  }, [])
  const clearDrag = useCallback(() => {
    dragCandidateRef.current = null
    draggedTaskNameRef.current = ''
    pendingDragPointRef.current = null
    if (dragFrameRef.current != null) {
      window.cancelAnimationFrame(dragFrameRef.current)
      dragFrameRef.current = null
    }
    setDraggedTaskName('')
    applyDropIntent(null)
  }, [applyDropIntent])
  const captureOrderMotion = useCallback(() => {
    const content = tasksContentRef.current
    orderMotionRef.current = content && !window.matchMedia('(prefers-reduced-motion: reduce)').matches
      ? { view: orderView, tasks: useTaskStore.getState().tasks, scrollTop: content.scrollTop, positions: visibleCardPositions(content) }
      : null
  }, [orderView])
  useLayoutEffect(() => {
    const motion = orderMotionRef.current
    const content = tasksContentRef.current
    if (!motion || !content) return
    if (motion.view !== orderView || motion.tasks.length !== tasks.length || motion.scrollTop !== content.scrollTop) {
      orderMotionRef.current = null
      return
    }
    if (motion.tasks.every((task, index) => task.name === tasks[index].name && Boolean(task.pinned) === Boolean(tasks[index].pinned))) return
    orderMotionRef.current = null
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return
    for (const [name, { card, left, top }] of visibleCardPositions(content)) {
      const previous = motion.positions.get(name)
      if (!previous) continue
      const dx = previous.left - left
      const dy = previous.top - top
      if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue
      card.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'translate(0, 0)' }], {
        duration: 185,
        easing: 'cubic-bezier(0.2, 0.75, 0.25, 1)',
      })
    }
  }, [orderView, tasks])
  const loadDetailTask = useCallback((task: Task, refresh = true) => {
    const requestId = ++detailRequestSeqRef.current
    const requestedKey = taskDetailRefreshKey(task)
    const requestIsCurrent = () => requestId === detailRequestSeqRef.current
      && workspaceEpoch === useWorkspaceStore.getState().workspaceEpoch
    detailRequestControllerRef.current?.abort()
    const controller = new AbortController()
    detailRequestControllerRef.current = controller
    let timedOut = false
    const timeout = window.setTimeout(() => {
      timedOut = true
      controller.abort(new Error(t('Task detail loading timed out. Please retry.')))
    }, TASK_DETAIL_TIMEOUT_MS)
    return api.getTask(task.name, refresh, false, controller.signal).then(fullTask => {
      if (!requestIsCurrent() || controller.signal.aborted) return
      setDetailTask(current => current?.name === task.name && taskDetailRefreshKey(current) === requestedKey
        ? fullTask : current)
    }).catch(err => {
      if (requestIsCurrent() && (!controller.signal.aborted || timedOut)) {
        notify({ tone: 'error', title: t('Could not load task details'), detail: errorMessage(err) })
      }
    }).finally(() => {
      window.clearTimeout(timeout)
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
    async fromDisk => {
      await fetchTasks({ background: true, refresh: fromDisk })
      const current = detailTaskRef.current
      // A status update can remove an open task from the current filter or page.
      if (current && !detailRequestControllerRef.current
        && (query || !useTaskStore.getState().tasks.some(task => task.name === current.name))) {
        await loadDetailTask(current, fromDisk)
      }
    },
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

  useEffect(() => { clearDrag() }, [clearDrag, orderView, sortMode, selectMode])

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

  const selectedTasks = useMemo(() => tasks.filter(task => selectedIds.has(task.name)), [tasks, selectedIds])
  const runSelection = useMemo(() => selectedTasks.filter(task => task.task_storage !== 'remote'), [selectedTasks])
  const deleteSelection = useMemo(() => selectedTasks.filter(task => !stopOnExecutionDevice(task)), [selectedTasks])
  const visibleSelectedCount = selectedTasks.length
  const anyTaskActionPending = pendingTaskActions.size > 0
  const orderMutationPending = hasPendingOrderMutation(pendingTaskActions)
  const activeSelectedCount = deleteSelection.filter(isActiveTask).length
  const allPageSelected = tasks.length > 0 && visibleSelectedCount === tasks.length
  const pinnedTasks = useMemo(() => tasks.filter(task => task.pinned), [tasks])
  const otherTasks = useMemo(() => tasks.filter(task => !task.pinned), [tasks])
  const showPinnedSection = pinnedTasks.length > 0
  const summary = useMemo(() => statusCounts ? {
    total: Object.values(statusCounts).reduce((count, value) => count + value, 0),
    active: statusCounts.running + statusCounts.queued,
  } : null, [statusCounts])

  useEffect(() => {
    if (!taskActionMessage) {
      return
    }
    const timeout = window.setTimeout(() => setTaskActionMessage(''), 4200)
    return () => window.clearTimeout(timeout)
  }, [taskActionMessage])

  useEffect(() => {
    if (deleteConfirm && deleteSelection.length === 0 && !bulkAction) {
      setDeleteConfirm(false)
    }
  }, [bulkAction, deleteConfirm, deleteSelection.length])

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
    const names = runSelection.map(task => task.name)
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
  }, [runSelection, beginBulkAction, normalizeWorkerInput, maxWorkersInput, clearSelection, fetchTasks, notify, finishBulkAction])

  const handleDeleteSelected = useCallback(async () => {
    const names = deleteSelection.map(task => task.name)
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
  }, [deleteSelection, beginBulkAction, clearSelection, fetchTasks, notify, finishBulkAction])

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
      notify({ tone: 'success', title: t("Task moved to trash"), detail: target.display_name ?? target.name })
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
          ? t(taskStopFeedback(updatedTask))
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
      || !event.isPrimary
    ) {
      return
    }

    event.preventDefault()
    clearDrag()
    dragCandidateRef.current = {
      taskName: task.name,
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      gridAxes: new WeakMap(),
    }
    setTaskActionMessage('')
  }, [clearDrag, selectMode])

  const refreshManualOrder = useCallback(async () => {
    if (useTaskStore.getState().sortMode === 'manual') {
      await fetchTasks()
      return
    }
    setSortMode('manual')
  }, [fetchTasks, setSortMode])

  const handleTaskDrop = useCallback(async (intent: DropIntent, taskName: string) => {
    const isCurrentWorkspace = () => workspaceEpoch === useWorkspaceStore.getState().workspaceEpoch
    if (!isCurrentWorkspace()) return
    const task = useTaskStore.getState().tasks.find(item => item.name === taskName)

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
        limit: REORDER_TASK_LIMIT,
        refresh: false,
        compact: true,
        sort: useTaskStore.getState().sortMode,
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

      captureOrderMotion()
      await api.reorderTasks(items)
      if (!isCurrentWorkspace()) return
      if (movedItem.pinned !== task.pinned) {
        setTaskActionMessage(movedItem.pinned ? t('Pinned {{name}}.', { name: task.name }) : t('Moved {{name}} back to Tasks.', { name: task.name }))
      } else {
        setTaskActionMessage(t('Moved {{name}}.', { name: task.name }))
      }
      await refreshManualOrder()
    } catch (err) {
      orderMotionRef.current = null
      if (isCurrentWorkspace()) notify({ tone: 'error', title: t("Could not move task"), detail: errorMessage(err) })
    } finally {
      if (isCurrentWorkspace()) finishTaskAction(task.name)
    }
  }, [beginTaskAction, captureOrderMotion, notify, refreshManualOrder, rejectIncompleteReorder, finishTaskAction, workspaceEpoch])

  const handleKeyboardDrag = useCallback((task: Task, action: KeyboardDragAction, grip: HTMLElement) => {
    if (dragCandidateRef.current) return
    const pickedUp = draggedTaskNameRef.current === task.name
    if (action === 'cancel' || (action === 'toggle' && pickedUp)) {
      if (!pickedUp) return
      const intent = dropIntentRef.current
      clearDrag()
      setTaskActionMessage('')
      if (action === 'toggle' && intent) void handleTaskDrop(intent, task.name)
      return
    }
    if (action === 'toggle') {
      clearDrag()
      draggedTaskNameRef.current = task.name
      setDraggedTaskName(task.name)
      setTaskActionMessage(t('Use the arrow keys to choose a drop position.'))
      return
    }
    if (!pickedUp) return

    const section = useTaskStore.getState().tasks.filter(item => Boolean(item.pinned) === Boolean(task.pinned))
    const source = section.findIndex(item => item.name === task.name)
    const anchor = section.findIndex(item => item.name === (dropIntentRef.current?.targetName || task.name))
    const next = anchor + (action === 'previous' ? -1 : 1)
    const target = section[next]
    if (source < 0 || anchor < 0 || !target) return
    const placement = next < source ? 'before' : 'after'
    applyDropIntent(next === source ? null : {
      target: task.pinned ? 'pinned' : 'tasks', targetName: target.name, placement, axis: gridDropAxis(grip),
    })
    setTaskActionMessage(next === source ? t('Original position') : t(placement === 'before' ? 'Drop before {{name}}' : 'Drop after {{name}}', { name: target.name }))
    const grid = grip.closest('[data-task-grid]')
    grid?.querySelector(`[data-task-card="${CSS.escape(target.name)}"]`)?.scrollIntoView({ block: 'nearest' })
  }, [applyDropIntent, clearDrag, handleTaskDrop])

  useEffect(() => {
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

      applyDropIntent(getPointerDropIntent(point.clientX, point.clientY, candidate.gridAxes))
    }

    const handleGlobalPointerMove = (event: PointerEvent) => {
      const candidate = dragCandidateRef.current
      if (!candidate || candidate.pointerId !== event.pointerId) {
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
      if (!candidate || candidate.pointerId !== event.pointerId) {
        return
      }

      const distance = Math.hypot(event.clientX - candidate.startX, event.clientY - candidate.startY)
      const wasDragging = Boolean(draggedTaskNameRef.current) || distance >= DRAG_START_DISTANCE
      const intent = wasDragging ? getPointerDropIntent(event.clientX, event.clientY, candidate.gridAxes) : null

      clearDrag()

      if (intent) {
        void handleTaskDrop(intent, candidate.taskName)
      }
    }

    const cancelPointerDrag = (event: PointerEvent) => {
      if (dragCandidateRef.current?.pointerId !== event.pointerId) return
      clearDrag()
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
  }, [applyDropIntent, clearDrag, handleTaskDrop])

  useEffect(() => {
    if (!draggedTaskName || !dragCandidateRef.current) {
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

  const refreshLabel = query ? loading ? 'Cancel search' : 'Refresh search results' : 'Refresh tasks'

  return (
    <div className="manager-page flex h-full flex-col overflow-hidden bg-surface-base">
      <header data-tour="manager-controls" className="workspace-header flex-none border-b border-border-subtle bg-surface-raised px-3 py-2.5 sm:px-5">
        <div className="manager-heading">
          <div className="manager-title flex min-w-0 items-center gap-2">
            <h1 className="workspace-heading min-w-0 font-semibold tracking-tight text-txt-primary">{t("Task Manager")}</h1>
            <div className="manager-title-actions flex shrink-0 items-center gap-1">
              <TaskSourceFilter />
              <button
                type="button"
                aria-label={t(refreshLabel)}
                title={t(refreshLabel)}
                disabled={loading && !query}
                onClick={() => loading && query
                  ? useTaskStore.getState().cancelTaskSearch()
                  : void fetchTasks({ forceRefresh: true })}
                className="touch-target inline-flex h-11 w-11 flex-none items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 disabled:cursor-wait disabled:opacity-50 sm:h-8 sm:w-8"
              >
                {loading
                  ? query
                    ? <Square aria-hidden="true" className="h-3.5 w-3.5" />
                    : <Loader2 aria-hidden="true" className="h-3.5 w-3.5 motion-safe:animate-spin" />
                  : <RefreshCw aria-hidden="true" className="h-4 w-4" />}
              </button>
            </div>
          </div>

          <div className="manager-filters">
            <div className="manager-filter-control">
              <select
                className="manager-toolbar-select"
                aria-label={t('Filter tasks by status')}
                value={statusFilter}
                onChange={event => setStatusFilter(event.target.value)}
              >
                {STATUS_OPTIONS.map(option => {
                  const label = option === 'All' ? 'All tasks' : option === 'active' ? 'Active' : STATUS_LABELS[option]
                  const count = option === 'All' ? summary?.total : option === 'active' ? summary?.active : statusCounts?.[option]
                  return <option key={option} value={option}>{t(label)}{count != null ? ` · ${count}` : ''}</option>
                })}
              </select>
              <ChevronDown aria-hidden="true" className="manager-filter-chevron" />
            </div>
            <div className="manager-filter-control manager-sort-control">
              <ArrowDownWideNarrow aria-hidden="true" className="manager-sort-icon" />
              <select
                className="manager-toolbar-select"
                aria-label={t('Sort task cards')}
                value={sortMode}
                onChange={event => setSortMode(event.target.value as TaskSortMode)}
              >
                {TASK_SORT_OPTIONS.map(option => (
                  <option key={option.value} value={option.value}>{t(option.label)}</option>
                ))}
              </select>
              <ChevronDown aria-hidden="true" className="manager-filter-chevron" />
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
            disabled={Boolean(bulkAction) || (!selectMode && (tasks.length === 0 || anyTaskActionPending))}
            title={anyTaskActionPending ? t('Wait for the current task action to finish') : t(selectMode ? 'Cancel' : 'Select tasks')}
            onClick={() => selectMode ? exitSelectMode() : setSelectMode(true)}
          >
            {t(selectMode ? 'Cancel' : 'Select tasks')}
          </ActionButton>
        </div>

        <span id="manager-reorder-help" className="sr-only">
          {t('Press Enter or Space to pick up or drop the card. Use the arrow keys to choose a position and Escape to cancel.')}
        </span>

        {selectMode && (
          <div
            id="manager-selection-actions"
            className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-border-subtle pt-2"
            role="region"
            aria-label={t("Current page selection actions")}
          >
            <div className="text-xs font-medium tabular-nums text-txt-primary" aria-live="polite"
              title={t("Selection is page-scoped and resets when you change the page, search, or status filter.")}>
                {t('{{selected}} of {{total}} selected on this page', { selected: visibleSelectedCount, total: tasks.length })}
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
                disabled={runSelection.length === 0 || Boolean(bulkAction)}
                aria-busy={bulkAction === 'run' || undefined}
              >
                {t('Run ({{count}})', { count: runSelection.length })}
              </ActionButton>

              <ActionButton
                icon={<Trash2 className="h-3.5 w-3.5" />}
                variant="ghost"
                className="min-h-11 text-rose-700 dark:text-rose-300 sm:min-h-9"
                onClick={() => setDeleteConfirm(true)}
                disabled={deleteSelection.length === 0 || Boolean(bulkAction)}
              >
                {t("Delete")}
              </ActionButton>

            </div>
          </div>
        )}
      </header>

      <section
        ref={tasksContentRef}
        className="manager-tasks-content min-h-0 flex-1 overflow-y-auto px-3 py-4 sm:px-6 sm:py-5"
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

        {draggedTaskName && dragCandidateRef.current && !showPinnedSection && (
          <div className="sticky top-0 z-20 h-0">
            <div
              data-task-drop-target="pinned"
              className={clsx('absolute right-0 top-0 rounded-md border border-accent/40 bg-surface-raised px-4 py-3 text-xs font-medium text-accent-ink shadow-sm',
                dragOverTarget === 'pinned' && 'ring-2 ring-accent/50')}
            >
              {t("Drop here to pin")}
            </div>
          </div>
        )}
        {loading && tasks.length === 0 ? (
          <ManagerLoadingState />
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
          <div className="space-y-5">
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
                  icon={<Pin className="h-3.5 w-3.5 text-txt-tertiary" />}
                  bodyClassName="pt-1"
                >
                  <TaskGrid
                    tasks={pinnedTasks}
                    query={query}
                    draggedTaskName={draggedTaskName}
                    dropIntent={dragOverTarget === 'pinned' ? dropIntent : null}
                    selectedIds={selectedIds}
                    selectMode={selectMode}
                    orderMutationPending={orderMutationPending}
                    pendingTaskActions={pendingTaskActions}
                    onCardClick={handleCardClick}
                    onTaskAction={requestTaskAction}
                    onPin={handlePin}
                    onKeyboardDrag={handleKeyboardDrag}
                    onDelete={setDeleteTask}
                    onMonitor={openTaskLogs}
                    onPointerDown={handleTaskPointerDown}
                  />
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
              {showPinnedSection && (
                <h2 className="mb-2 flex items-baseline gap-2 px-0.5 text-xs font-medium">
                  {t("Tasks")}
                  {dragOverTarget === 'tasks' && <span className="text-2xs font-normal text-txt-tertiary">{t("Drop to reorder")}</span>}
                </h2>
              )}
              <TaskGrid
                tasks={otherTasks}
                query={query}
                draggedTaskName={draggedTaskName}
                dropIntent={dragOverTarget === 'tasks' ? dropIntent : null}
                selectedIds={selectedIds}
                selectMode={selectMode}
                orderMutationPending={orderMutationPending}
                pendingTaskActions={pendingTaskActions}
                onCardClick={handleCardClick}
                onTaskAction={requestTaskAction}
                onPin={handlePin}
                onKeyboardDrag={handleKeyboardDrag}
                onDelete={setDeleteTask}
                onMonitor={openTaskLogs}
                onPointerDown={handleTaskPointerDown}
              />
            </div>
          </div>
        )}
      </section>

      <footer className="flex flex-none items-center gap-3 border-t border-border-subtle bg-surface-raised px-3 py-2 sm:px-4">
        <span className="min-w-0 flex-1 truncate text-2xs text-txt-secondary" title={taskActionMessage || undefined}>
          <span role="status" aria-live="polite">{taskActionMessage}</span>
          {!taskActionMessage && <>
            {selectMode && visibleSelectedCount > 0 ? t('{{count}} selected on page · ', { count: visibleSelectedCount }) : ''}
            {tasks.length > 0 ? t('Showing {{start}}–{{end}} of ', { start: offset + 1, end: offset + tasks.length }) : ''}
            {t('{{count}} matching task(s)', { count: total })}
          </>}
        </span>
        <Pagination total={total} offset={offset} limit={MANAGER_TASK_PAGE_SIZE} showTotal={false} onOffsetChange={nextOffset => {
          setOffset(nextOffset)
          if (tasksContentRef.current) tasksContentRef.current.scrollTop = 0
        }} />
      </footer>

      <ConfirmDialog
        open={deleteConfirm}
        title={t("Delete Tasks")}
        description={activeSelectedCount > 0
          ? t('Stop {{active}} active task(s) and move {{selected}} task(s) from this page to trash?', { active: activeSelectedCount, selected: deleteSelection.length })
          : t('Move {{count}} selected task(s) from this page to trash?', { count: deleteSelection.length })}
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
            ? t("Stop '{{name}}' and move it to trash?", { name: deleteTask.display_name ?? deleteTask.name })
            : t("Move '{{name}}' to trash?", { name: deleteTask.display_name ?? deleteTask.name })
          : ''}
        confirmLabel={t("Delete")}
        confirmVariant="danger"
        onConfirm={handleDeleteTask}
        onCancel={() => setDeleteTask(null)}
      />

      <ConfirmDialog
        open={Boolean(cancelTask)}
        title={t("Stop active task?")}
        description={cancelTask ? describeTaskStop(cancelTask) : ''}
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

const TaskGrid = memo(function TaskGrid({
  tasks,
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
  onKeyboardDrag,
  onDelete,
  onMonitor,
  onPointerDown,
}: {
  tasks: Task[]
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
  onKeyboardDrag: (task: Task, action: KeyboardDragAction, grip: HTMLElement) => void
  onDelete: (task: Task) => void
  onMonitor: (task: Task) => void
  onPointerDown: (task: Task, event: ReactPointerEvent<HTMLElement>) => void
}) {
  useTranslation()
  if (tasks.length === 0) {
    return <div className="px-2 py-5 text-center text-2xs text-txt-tertiary">{t("No tasks in this section")}</div>
  }

  return (
    <div data-task-grid="true" className="manager-task-grid">
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
          onKeyboardDrag={onKeyboardDrag}
          onDelete={onDelete}
          onMonitor={onMonitor}
          onPointerDown={onPointerDown}
        />
      ))}
    </div>
  )
})

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
  onKeyboardDrag,
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
  onKeyboardDrag: (task: Task, action: KeyboardDragAction, grip: HTMLElement) => void
  onDelete: (task: Task) => void
  onMonitor: (task: Task) => void
  onPointerDown: (task: Task, event: ReactPointerEvent<HTMLElement>) => void
}) {
  useTranslation()
  const actionBtn = getActionButton(task)
  const canDelete = !stopOnExecutionDevice(task)
  const statusId = useId()
  const statusLabel = taskStatusLabel(task)
  const displayName = task.display_name ?? task.name
  const accessibleName = task.task_source_host ? `${displayName} · ${task.task_source_host}` : displayName
  const folderName = task.dir.split(/[\\/]/).pop() || task.dir
  const createdAtLabel = task.created_at?.replace(/^(\d{4}-\d{2}-\d{2})[_T](\d{2})[-:](\d{2}).*$/, '$1 · $2:$3')
  const taskKindLabel = task.task_kind === 'shell' ? 'Shell' : 'Python'
  const cardDescription = task._load_error || task.preview_text
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
      data-task-status={task.status}
      data-task-tone={task.status}
      data-task-card-pinned={task.pinned ? 'true' : 'false'}
      data-selection-mode={selectMode}
      aria-busy={taskPending || undefined}
      className={clsx(
        'task-card relative flex min-w-0 flex-col bg-surface-raised [contain-intrinsic-size:auto_176px] [content-visibility:auto] motion-safe:transition-[opacity,background-color] duration-150',
        selected && 'z-[1] ring-1 ring-accent',
        dragging && 'opacity-70 ring-1 ring-accent/50',
        dropPlacement && !dragging && 'ring-1 ring-accent/35',
      )}
    >
      {dropIndicator}
      {selectMode && (
        <div className="task-card-corner task-card-control pointer-events-none flex items-center justify-center">
          <SelectionIndicator selected={selected} />
        </div>
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
        aria-describedby={statusId}
        aria-pressed={selectMode ? selected : undefined}
        className="task-card-main flex min-w-0 flex-1 flex-col gap-2 text-left focus-visible:outline-offset-[-2px]"
      >
        <div className="task-card-state flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span id={statusId} className="task-card-status-label inline-flex min-w-0 max-w-full items-center gap-1.5 text-xs font-semibold leading-4 [overflow-wrap:anywhere]">
            <TaskStatusDot task={task} decorative />
            <span>{t(statusLabel)}</span>
          </span>
          {taskPending && (
            <span className="inline-flex items-center gap-1 text-2xs text-txt-secondary" role="status">
              <Loader2 aria-hidden="true" className="size-3 animate-spin" />
              {pendingAction === 'move' ? t("Moving") : pendingAction === 'delete' ? t("Deleting") : t("Updating")}
            </span>
          )}
        </div>
        <div className="task-card-name text-base font-semibold leading-5 tracking-tight text-txt-primary [overflow-wrap:anywhere]" title={displayName}>
          {displayName}
        </div>
        {cardDescription && (
          <div className="task-card-preview min-w-0 font-mono text-2xs leading-5 text-txt-secondary" title={cardDescription}>
            <div className="truncate-2 whitespace-pre-wrap [overflow-wrap:anywhere]">{cardDescription}</div>
          </div>
        )}
        <div className="task-card-context flex min-w-0 flex-col gap-1.5">
          <div className="task-card-meta flex flex-wrap items-center gap-x-2.5 gap-y-1 text-2xs text-txt-tertiary">
            <span>{taskKindLabel}</span>
            <span title={task.created_at}>{createdAtLabel}</span>
            {task.run_index > 0 && <span>{t('Run #{{index}}', { index: task.run_index })}</span>}
            {folderName !== displayName && <span className="truncate" title={task.dir}>{folderName}</span>}
          </div>
          <TaskExecutionInfo task={task} />
          {gpuWait && (
            <div
              className="inline-flex min-w-0 items-center gap-1.5 text-2xs font-medium text-[var(--task-status-color)]"
              title={gpuWait.reason || t("Waiting for GPU capacity")}
            >
              <Cpu className="h-3 w-3 flex-none" aria-hidden="true" />
              <span>{t("Waiting for GPU")}{gpuCapacity ? ` · ${gpuCapacity}` : ''}</span>
            </div>
          )}
          {task._load_error && (
            <div className="inline-flex min-w-0 items-center gap-1 text-2xs font-medium text-rose-700 dark:text-rose-300" title={task._load_error}>
              <AlertTriangle className="h-3 w-3" aria-hidden="true" />
              <span className="truncate">{t("Task load error")}</span>
            </div>
          )}
        </div>
      </div>

      {!selectMode && (
        <div className="task-card-footer flex min-w-0 items-center justify-end">
          <button
            type="button"
            data-task-drag-handle="true"
            aria-label={t('Reorder {{name}}', { name: accessibleName })}
            aria-describedby="manager-reorder-help"
            aria-pressed={dragging}
            disabled={taskPending || reorderDisabled}
            onPointerDown={event => onPointerDown(task, event)}
            onClick={event => {
              if (event.detail === 0) onKeyboardDrag(task, 'toggle', event.currentTarget)
            }}
            onKeyDown={event => {
              if (event.repeat && (event.key === 'Enter' || event.key === ' ')) event.preventDefault()
              if (!dragging) return
              const action = event.key === 'Escape' ? 'cancel'
                : event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? 'previous'
                : event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 'next' : null
              if (!action) return
              event.preventDefault()
              onKeyboardDrag(task, action, event.currentTarget)
            }}
            onBlur={event => onKeyboardDrag(task, 'cancel', event.currentTarget)}
            className="task-card-grip task-card-control touch-target me-auto flex shrink-0 cursor-grab touch-none items-center justify-center text-txt-secondary transition-[opacity,color,background-color] hover:bg-surface-overlay hover:text-txt-primary active:cursor-grabbing disabled:cursor-not-allowed disabled:opacity-40"
            title={t('Drag to reorder')}
          >
            <GripVertical aria-hidden="true" className="size-4" />
          </button>
          <div className="task-card-actions flex flex-none items-center">
            <TaskIconButton
              label={t(task.pinned ? 'Unpin {{name}}' : 'Pin {{name}}', { name: accessibleName })}
              title={t(task.pinned ? 'Unpin' : 'Pin')}
              className="task-card-pin"
              disabled={taskPending || reorderDisabled}
              busy={pendingAction === 'pin'}
              onClick={() => void onPin(task)}
            >
              {pendingAction === 'pin'
                ? <Loader2 aria-hidden="true" className="size-3.5 animate-spin" />
                : <Pin aria-hidden="true" className={clsx('size-3.5', task.pinned && 'fill-current text-accent-ink')} />}
            </TaskIconButton>
            {actionBtn && (
              <TaskIconButton
                label={t('{{action}} {{name}}', { action: t(actionBtn.label), name: task.name })}
                title={t(actionBtn.label)}
                disabled={taskPending}
                busy={pendingAction === actionBtn.action}
                danger={actionBtn.action === 'cancel'}
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
            >
              <Terminal className="h-3.5 w-3.5" />
            </TaskIconButton>
            {canDelete && (
              <TaskIconButton
                label={t('Delete {{name}}', { name: accessibleName })}
                title={t('Delete task')}
                danger
                disabled={taskPending}
                busy={pendingAction === 'delete'}
                onClick={() => onDelete(task)}
              >
                {pendingAction === 'delete'
                  ? <Loader2 aria-hidden="true" className="size-3.5 motion-safe:animate-spin" />
                  : <Trash2 aria-hidden="true" className="size-3.5" />}
              </TaskIconButton>
            )}
          </div>
        </div>
      )}
      {query && <div className="task-card-matches mx-4 mb-3 border-t border-border-subtle pt-1">
        <TaskSearchMatches task={task} onSelect={() => onCardClick(task)} exportMode={selectMode} action={selectMode ? t("Select") : t("View")} />
      </div>}
    </article>
  )
})

function TaskIconButton({
  children,
  label,
  title,
  className,
  danger = false,
  disabled = false,
  busy = false,
  onClick,
}: {
  children: ReactNode
  label: string
  title: string
  className?: string
  danger?: boolean
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
        'task-card-control touch-target inline-flex items-center justify-center text-txt-secondary transition-colors hover:bg-surface-overlay focus-visible:outline-offset-0 disabled:cursor-not-allowed disabled:opacity-40',
        className,
        danger
          ? 'hover:text-rose-700 focus-visible:text-rose-700 dark:hover:text-rose-300 dark:focus-visible:text-rose-300'
          : 'hover:text-txt-primary',
      )}
    >
      {children}
    </button>
  )
}

function ManagerLoadingState() {
  useTranslation()
  return (
    <div role="status" aria-live="polite" aria-label={t("Loading tasks")}>
      <div className="manager-task-grid">
        {Array.from({ length: 8 }, (_, index) => (
          <div key={index} className="rounded-lg border border-border-subtle bg-surface-raised p-3 motion-safe:animate-pulse">
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
  if (canStopTask(task)) return { label: 'Stop', action: 'cancel' as const, icon: Square }
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
      }
    case 'completed':
      return {
        action: 'rerun' as const,
        icon: RotateCcw,
        label: 'Rerun',
      }
    default:
      return null
  }
}
