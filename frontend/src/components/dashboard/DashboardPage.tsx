import { useCallback, useEffect, useRef, useState, type ElementType, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  Cpu,
  Fan,
  Gauge,
  Layers,
  MemoryStick,
  RefreshCw,
  Thermometer,
  Wand2,
  X,
  XCircle,
  Zap,
} from 'lucide-react'
import clsx from 'clsx'
import { useDashboardStore, useMonitorStore, useToastStore, useWorkspaceStore } from '@/store'
import { usePolling } from '@/hooks/usePolling'
import StatusBadge from '@/components/shared/StatusBadge'
import CopyButton from '@/components/shared/CopyButton'
import { formatElapsedDuration } from '@/utils/taskRuntime'
import { getWorkspaceWorkingPath } from '@/utils/workspace'
import { errorMessage } from '@/utils/errors'
import type { GPUMetric, GPUProcessDetails, GPUProcessInfo, Task, SystemMetrics } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import * as api from '@/api'

const STAT_CARDS: { key: string; label: string; icon: ElementType; color: string }[] = [
  { key: 'total', label: 'Total Tasks', icon: Layers, color: 'text-txt-secondary' },
  { key: 'running', label: 'Running', icon: Activity, color: 'text-amber-700 dark:text-amber-300' },
  { key: 'completed', label: 'Completed', icon: CheckCircle2, color: 'text-emerald-700 dark:text-emerald-300' },
  { key: 'failed', label: 'Failed', icon: XCircle, color: 'text-rose-700 dark:text-rose-300' },
]

const GPU_DETAILS_REQUEST_TIMEOUT_MS = 10_000
const PROCESS_DETAILS_REQUEST_TIMEOUT_MS = 10_000

interface DashboardRefreshResult {
  dashboardOk: boolean
  metricsOk: boolean
}

interface ProcessDetailLoadState {
  loading: boolean
  data: GPUProcessDetails | null
  error: string
}

export default function DashboardPage() {
  const { data, loading, error: dashboardError, fetch } = useDashboardStore()
  const workspace = useWorkspaceStore(s => s.workspace)
  const notify = useToastStore(state => state.notify)
  const navigate = useNavigate()
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null)
  const [metricsError, setMetricsError] = useState('')
  const [activeGpu, setActiveGpu] = useState<GPUMetric | null>(null)
  const [gpuDetailsLoading, setGpuDetailsLoading] = useState(false)
  const [gpuDetailsError, setGpuDetailsError] = useState('')
  const gpuDialogTriggerRef = useRef<HTMLElement | null>(null)
  const gpuDetailsRequestSeqRef = useRef(0)
  const gpuDetailsAbortControllerRef = useRef<AbortController | null>(null)
  const metricsRefreshSeqRef = useRef(0)
  const dashboardRefreshPromiseRef = useRef<Promise<DashboardRefreshResult> | null>(null)
  const [manualRefreshing, setManualRefreshing] = useState(false)
  const refreshIntervalRaw = Number(workspace?.settings?.header_refresh_interval ?? 3)
  const refreshIntervalSec = Number.isFinite(refreshIntervalRaw) ? Math.max(1, refreshIntervalRaw) : 3

  const refreshDashboard = useCallback(() => {
    if (dashboardRefreshPromiseRef.current) {
      return dashboardRefreshPromiseRef.current
    }

    const requestId = ++metricsRefreshSeqRef.current
    const refreshPromise = Promise.allSettled([
      fetch(),
      api.getMetrics(),
    ]).then(([dashboardResult, metricsResult]) => {
      if (requestId === metricsRefreshSeqRef.current) {
        if (metricsResult.status === 'fulfilled') {
          const nextMetrics = metricsResult.value
          setMetrics(nextMetrics)
          setMetricsError('')
        } else {
          setMetricsError(errorMessage(metricsResult.reason, 'System metrics unavailable.'))
        }
      }
      return {
        dashboardOk: dashboardResult.status === 'fulfilled',
        metricsOk: metricsResult.status === 'fulfilled',
      }
    })
    const trackedPromise: Promise<DashboardRefreshResult> = refreshPromise.finally(() => {
      if (dashboardRefreshPromiseRef.current === trackedPromise) {
        dashboardRefreshPromiseRef.current = null
      }
    })
    dashboardRefreshPromiseRef.current = trackedPromise
    return trackedPromise
  }, [fetch])

  const openTaskInMonitor = useCallback((task: Task) => {
    void useMonitorStore.getState().selectTask(task.name)
      .catch(err => notify({ tone: 'error', title: 'Could not load task logs', detail: errorMessage(err) }))
    navigate('/monitor')
  }, [navigate, notify])

  const loadGpuDetails = useCallback(async (gpu: GPUMetric) => {
    gpuDetailsAbortControllerRef.current?.abort()
    const controller = new AbortController()
    gpuDetailsAbortControllerRef.current = controller
    const requestId = ++gpuDetailsRequestSeqRef.current
    let timedOut = false
    const timeoutId = window.setTimeout(() => {
      timedOut = true
      controller.abort()
    }, GPU_DETAILS_REQUEST_TIMEOUT_MS)
    setGpuDetailsLoading(true)
    setGpuDetailsError('')
    try {
      const details = await api.getMetrics(
        { includeProcesses: true, detail: true },
        controller.signal,
      )
      const matchingGpu = details.gpus.find(item => gpuKey(item) === gpuKey(gpu))
      if (!matchingGpu) {
        throw new Error('This GPU is no longer available.')
      }
      if (requestId !== gpuDetailsRequestSeqRef.current) {
        return
      }
      setActiveGpu(matchingGpu)
    } catch (err) {
      if (requestId !== gpuDetailsRequestSeqRef.current) {
        return
      }
      setGpuDetailsError(timedOut
        ? 'GPU process details timed out. Check the connection and retry.'
        : errorMessage(err, 'GPU process details are unavailable.'))
    } finally {
      window.clearTimeout(timeoutId)
      if (gpuDetailsAbortControllerRef.current === controller) {
        gpuDetailsAbortControllerRef.current = null
      }
      if (requestId === gpuDetailsRequestSeqRef.current) {
        setGpuDetailsLoading(false)
      }
    }
  }, [])

  const openGpuDetails = useCallback((gpu: GPUMetric, trigger: HTMLElement) => {
    gpuDialogTriggerRef.current = trigger
    setActiveGpu(gpu)
    void loadGpuDetails(gpu)
  }, [loadGpuDetails])

  const closeGpuDetails = useCallback(() => {
    gpuDetailsRequestSeqRef.current += 1
    gpuDetailsAbortControllerRef.current?.abort()
    gpuDetailsAbortControllerRef.current = null
    setActiveGpu(null)
    setGpuDetailsLoading(false)
    setGpuDetailsError('')
    const trigger = gpuDialogTriggerRef.current
    gpuDialogTriggerRef.current = null
    window.requestAnimationFrame(() => trigger?.focus())
  }, [])

  const handleManualRefresh = useCallback(async () => {
    setManualRefreshing(true)
    try {
      const result = await refreshDashboard()
      if (result.dashboardOk && result.metricsOk) {
        notify({
          tone: 'success',
          title: 'Dashboard refreshed',
          detail: 'Task summary and system metrics are up to date.',
        })
      } else if (result.dashboardOk || result.metricsOk) {
        notify({
          tone: 'info',
          title: 'Dashboard partially refreshed',
          detail: result.dashboardOk
            ? 'Task summary is current; system metrics are showing the last available values.'
            : 'System metrics are current; task summary could not be refreshed.',
        })
      } else {
        notify({ tone: 'error', title: 'Could not refresh dashboard', detail: 'Task summary and system metrics are unavailable.' })
      }
    } catch (err) {
      notify({ tone: 'error', title: 'Could not refresh dashboard', detail: errorMessage(err) })
    } finally {
      setManualRefreshing(false)
    }
  }, [notify, refreshDashboard])

  usePolling(async () => {
    await refreshDashboard()
  }, refreshIntervalSec * 1000, true, true)

  useEffect(() => () => {
    gpuDetailsRequestSeqRef.current += 1
    gpuDetailsAbortControllerRef.current?.abort()
    gpuDetailsAbortControllerRef.current = null
    metricsRefreshSeqRef.current += 1
    dashboardRefreshPromiseRef.current = null
  }, [])

  useEffect(() => {
    metricsRefreshSeqRef.current += 1
    gpuDetailsRequestSeqRef.current += 1
    gpuDetailsAbortControllerRef.current?.abort()
    gpuDetailsAbortControllerRef.current = null
    dashboardRefreshPromiseRef.current = null
    setMetrics(null)
    setMetricsError('')
    setActiveGpu(null)
    setGpuDetailsLoading(false)
    setGpuDetailsError('')
    void refreshDashboard()
  }, [refreshDashboard, workspace?.run_root])

  const summary = data?.summary
  const workspaceReady = workspace?.workspace_ready === true
  const isShellWorkspace = workspaceReady && workspace?.workspace_kind === 'shell'
  const workspaceKindLabel = !workspaceReady ? 'Workspace Needed' : isShellWorkspace ? 'Shell Workspace' : 'Script Workspace'
  const workspaceName = workspaceReady
    ? (workspace?.script_name || (isShellWorkspace ? '_shell_' : 'Workspace'))
    : 'Choose a workspace to start'
  const workspaceWorkingPath = getWorkspaceWorkingPath(workspace)
  const gpuCount = metrics?.gpus.length ?? 0
  const averageGpuUtil = gpuCount
    ? (metrics?.gpus.reduce((total, gpu) => total + gpu.util, 0) ?? 0) / gpuCount
    : 0
  const totalGpuMemory = metrics?.gpus.reduce((total, gpu) => total + Math.max(0, gpu.mem_total), 0) ?? 0
  const usedGpuMemory = metrics?.gpus.reduce((total, gpu) => total + Math.max(0, gpu.mem_used), 0) ?? 0
  const gpuMemoryPct = totalGpuMemory > 0 ? (usedGpuMemory / totalGpuMemory) * 100 : 0
  const gpuSchedulerEnabledRaw = workspace?.settings?.gpu_scheduler_enabled
  const gpuSchedulerEnabled = gpuSchedulerEnabledRaw === true
    || ['1', 'true', 'yes', 'on'].includes(String(gpuSchedulerEnabledRaw || '').toLowerCase())
  const configuredMinFreeGiB = Number(workspace?.settings?.gpu_scheduler_min_free_memory_gb ?? 0)
  const largestGpuGiB = gpuCount
    ? Math.max(...(metrics?.gpus.map(gpu => gpu.mem_total / 1024) || [0]))
    : 0
  const impossibleGpuRule = gpuSchedulerEnabled
    && Number.isFinite(configuredMinFreeGiB)
    && configuredMinFreeGiB > 0
    && largestGpuGiB > 0
    && configuredMinFreeGiB > largestGpuGiB
  const queuedCount = summary?.queued ?? 0
  const runningCount = summary?.running ?? 0
  const pendingCount = summary?.pending ?? 0
  const activeCount = runningCount + queuedCount

  return (
    <>
      <div className="h-full overflow-y-auto bg-surface-base">
        <div className="flex min-h-full w-full flex-col gap-3 px-4 py-4 2xl:px-6">
          <header className="shrink-0 flex flex-wrap items-center justify-between gap-3 border-b border-border-default pb-3">
            <div className="min-w-0 flex-1 basis-full sm:basis-0">
              <div className="flex flex-wrap items-center gap-2">
                <h1 className="mr-2 text-lg font-semibold text-txt-primary">Dashboard</h1>
                <span className="inline-flex items-center rounded-md bg-accent/10 px-2 py-1 text-2xs font-medium text-accent">
                  {workspaceKindLabel}
                </span>
                <span className="min-w-[12rem] max-w-full truncate font-mono text-2xs text-txt-tertiary sm:max-w-[56rem]" title={workspaceWorkingPath || ''}>
                  {workspaceWorkingPath || 'Choose a workspace to start'}
                </span>
              </div>
              <div className="mt-1 truncate font-mono text-2xs text-txt-secondary" title={workspaceName}>
                {workspaceName}
              </div>
            </div>
            <div className="grid w-full min-w-0 grid-cols-[44px_minmax(0,1fr)] gap-2 sm:flex sm:w-auto sm:flex-wrap sm:items-center">
              <button
                type="button"
                onClick={() => void handleManualRefresh()}
                disabled={manualRefreshing || loading}
                className="touch-target inline-flex min-h-11 min-w-0 items-center justify-center gap-2 rounded-md border border-border-subtle bg-surface-raised px-2 py-2 text-sm font-medium text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary disabled:cursor-not-allowed disabled:opacity-60 sm:min-h-10 sm:px-3"
                aria-label="Refresh dashboard"
                title="Refresh dashboard now"
              >
                <RefreshCw aria-hidden="true" className={clsx('h-4 w-4', manualRefreshing && 'animate-spin')} />
                <span className="hidden sm:inline">Refresh</span>
              </button>
              <button
                type="button"
                onClick={() => navigate(workspaceReady ? '/generator' : '/?launcher=1&mode=python')}
                className="touch-target inline-flex min-h-11 min-w-0 items-center justify-center gap-2 rounded-md bg-accent px-2 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent-hover sm:min-h-10 sm:px-4"
              >
                <Wand2 className="h-4 w-4" />
                <span className="whitespace-nowrap">{workspaceReady ? 'Start New Task' : 'Choose Workspace'}</span>
              </button>
            </div>
          </header>

          {dashboardError && (
            <div role="alert" className="flex shrink-0 items-start gap-2 rounded-md border border-rose-200 bg-rose-50 px-3 py-2.5 text-xs text-rose-800 dark:border-rose-800 dark:bg-rose-950/45 dark:text-rose-300">
              <AlertTriangle className="mt-0.5 h-4 w-4 flex-none" />
              <div className="min-w-0">
                <div className="font-semibold">{data ? 'Task summary refresh failed' : 'Task summary unavailable'}</div>
                <div className="mt-0.5 break-words">{dashboardError}</div>
                {data && <div className="mt-0.5">Showing the last successful values.</div>}
              </div>
            </div>
          )}

          <section className="flex shrink-0 flex-col overflow-hidden rounded-md border border-border-default bg-surface-raised">
            <div className="shrink-0 flex flex-wrap items-start justify-between gap-2 border-b border-border-subtle px-4 py-3">
              <div>
                <h2 className="text-sm font-medium text-txt-primary">GPU & System</h2>
                <p className="mt-1 text-2xs text-txt-tertiary">Refreshes every {refreshIntervalSec}s. Process details load on demand.</p>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-2xs">
                <SummaryPill>{gpuCount} GPU{gpuCount === 1 ? '' : 's'}</SummaryPill>
              </div>
            </div>

            <div className="shrink-0 border-b border-border-subtle p-3">
              {metrics ? (
                <>
                  {impossibleGpuRule && (
                    <div className="mb-3 flex items-start gap-2 rounded-md border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-xs text-rose-700 dark:text-rose-300">
                      <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none" />
                      <span>
                        GPU rule needs attention: {configuredMinFreeGiB.toFixed(0)} GiB free exceeds this machine&apos;s {largestGpuGiB.toFixed(1)} GiB physical capacity.
                      </span>
                    </div>
                  )}
                  {metricsError && (
                    <div className="mb-3 flex items-center gap-2 rounded-md bg-amber-500/10 px-3 py-2 text-2xs text-amber-800 dark:text-amber-300">
                      <AlertTriangle className="h-3.5 w-3.5 flex-none" />
                      <span className="min-w-0 break-words">Metrics refresh failed. Showing last values.</span>
                    </div>
                  )}
                  <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
                    <ResourceTile label="CPU" value={`${metrics.cpu_percent.toFixed(0)}%`}>
                      <MetricBar label="CPU" value={metrics.cpu_percent} icon={Cpu} compact />
                    </ResourceTile>
                    <ResourceTile label="RAM" value={`${metrics.mem_percent.toFixed(0)}%`}>
                      <MetricBar label="RAM" value={metrics.mem_percent} icon={MemoryStick} compact />
                    </ResourceTile>
                    <ResourceTile label="GPU Avg" value={`${averageGpuUtil.toFixed(0)}%`} tone="sky" />
                    <ResourceTile label="GPU VRAM" value={`${gpuMemoryPct.toFixed(0)}%`} tone={gpuMemoryPct > 85 ? 'amber' : 'slate'} />
                  </div>
                </>
              ) : (
                <div
                  className={clsx(
                    'flex items-center gap-2 rounded-md px-3 py-4 text-2xs',
                    metricsError ? 'bg-amber-500/10 text-amber-800 dark:text-amber-300' : 'bg-surface-overlay/50 text-txt-tertiary',
                  )}
                >
                  {metricsError && <AlertTriangle className="h-3.5 w-3.5 flex-none" />}
                  <span className="min-w-0 break-words">{metricsError || 'Loading system metrics...'}</span>
                </div>
              )}
            </div>

            <div className="p-3">
              {metrics?.gpus?.length ? (
                <div className={clsx(
                  'grid grid-cols-1 gap-3',
                  gpuCount > 1 && 'md:grid-cols-2 xl:grid-cols-3 2xl:grid-cols-4',
                )}>
                  {metrics.gpus.map(gpu => (
                    <GpuMetricCard
                      key={gpuKey(gpu)}
                      gpu={gpu}
                      wide={gpuCount === 1}
                      onClick={trigger => openGpuDetails(gpu, trigger)}
                    />
                  ))}
                </div>
              ) : (
                <div className="flex h-full min-h-[10rem] items-center justify-center rounded-md bg-surface-overlay/50 px-3 py-8 text-center text-2xs text-txt-tertiary">
                  {metricsError && !metrics ? 'System metrics unavailable.' : 'No NVIDIA GPU metrics detected.'}
                </div>
              )}
            </div>
          </section>

          <div className="grid shrink-0 grid-cols-2 gap-2 lg:grid-cols-4">
            {STAT_CARDS.map(({ key, label, icon: Icon, color }) => (
              <div key={key} className="rounded-md border border-border-default bg-surface-raised px-3 py-2.5">
                <div className="flex items-center gap-3">
                  <div className={clsx('rounded-md bg-surface-overlay p-2', color)}>
                    <Icon className="h-4 w-4" />
                  </div>
                  <div>
                    <div className="text-lg font-semibold tabular-nums text-txt-primary">
                      {loading && !data ? '--' : (summary as Record<string, number> | undefined)?.[key] ?? 0}
                    </div>
                    <div className="text-2xs text-txt-tertiary">{label}</div>
                  </div>
                </div>
                {key === 'running' && (
                  <div className="mt-3 flex flex-wrap gap-1.5 text-2xs">
                    <SummaryPill>{queuedCount} queued</SummaryPill>
                    <SummaryPill>{runningCount} executing</SummaryPill>
                  </div>
                )}
                {key === 'total' && (
                  <div className="mt-3 text-2xs text-txt-tertiary">
                    {pendingCount} pending task{pendingCount === 1 ? '' : 's'}
                  </div>
                )}
              </div>
            ))}
          </div>

          <section className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-md border border-border-default bg-surface-raised">
            <div className="shrink-0 flex flex-wrap items-center justify-between gap-2 border-b border-border-subtle px-4 py-3">
              <div>
                <h2 className="text-sm font-medium text-txt-primary">Recent Tasks</h2>
                <p className="mt-1 text-2xs text-txt-tertiary">Quick status glance.</p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                <SummaryPill>{activeCount} active</SummaryPill>
                <SummaryPill>{pendingCount} pending</SummaryPill>
                <button
                  type="button"
                  onClick={() => navigate('/manager')}
                  className="touch-target inline-flex min-h-11 items-center gap-1 rounded-md px-2 py-1 text-2xs text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-accent sm:min-h-0"
                >
                  View all <ArrowRight className="h-3 w-3" />
                </button>
              </div>
            </div>
            <div className="min-h-0 flex-1 divide-y divide-border-subtle overflow-y-auto">
              {dashboardError && !data ? (
                <div className="py-8 text-center text-xs text-rose-700 dark:text-rose-300">Recent tasks could not be loaded.</div>
              ) : !data?.recent_tasks?.length ? (
                <div className="py-8 text-center text-xs text-txt-tertiary">No tasks yet</div>
              ) : (
                data.recent_tasks.map(task => (
                  <TaskRow key={task.name} task={task} onClick={() => openTaskInMonitor(task)} />
                ))
              )}
            </div>
          </section>
        </div>
      </div>

      <GpuProcessDialog
        gpu={activeGpu}
        loading={gpuDetailsLoading}
        error={gpuDetailsError}
        onRetry={() => activeGpu && void loadGpuDetails(activeGpu)}
        onClose={closeGpuDetails}
      />
    </>
  )
}

function TaskRow({ task, onClick }: { task: Task; onClick: () => void }) {
  const taskKindLabel = task.task_kind === 'shell' ? 'shell' : 'python'
  const runIndex = Math.max(task.run_index || 1, 1)

  return (
    <button
      type="button"
      onClick={onClick}
      className="flex min-h-11 w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-surface-overlay focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40"
    >
      <div className="flex-none">
        <StatusBadge status={task.status as TaskStatus} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
          <span className="min-w-0 truncate text-sm font-medium text-txt-primary" title={task.name}>{task.name}</span>
          <span className="rounded-md bg-surface-overlay px-1.5 py-0.5 text-2xs uppercase tracking-[0.14em] text-txt-tertiary">
            {taskKindLabel}
          </span>
          <span className="font-mono text-2xs text-txt-tertiary">Run #{runIndex}</span>
        </div>
        <div className="mt-1 font-mono text-2xs text-txt-tertiary" title={task.created_at}>
          {task.created_at}
        </div>
      </div>
      <ChevronRight className="mt-1 h-3.5 w-3.5 flex-none text-txt-tertiary" />
    </button>
  )
}

function ResourceTile({
  label,
  value,
  tone = 'slate',
  children,
}: {
  label: string
  value: string
  tone?: 'emerald' | 'sky' | 'amber' | 'slate'
  children?: ReactNode
}) {
  const toneClass = {
    emerald: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
    sky: 'bg-sky-500/10 text-sky-700 dark:text-sky-300',
    amber: 'bg-amber-500/10 text-amber-800 dark:text-amber-300',
    slate: 'bg-surface-overlay text-txt-secondary',
  }[tone]

  return (
    <div className={clsx('min-w-0 rounded-md px-3 py-2.5', toneClass)}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-2xs uppercase tracking-[0.16em] text-txt-tertiary">{label}</span>
        <span className="font-mono text-xs font-semibold tabular-nums">{value}</span>
      </div>
      {children && <div className="mt-2">{children}</div>}
    </div>
  )
}

function MetricBar({
  label,
  value,
  icon: Icon,
  compact = false,
}: {
  label: string
  value: number
  icon: ElementType
  compact?: boolean
}) {
  const pct = Math.min(100, Math.max(0, value))
  const color = pct > 90 ? 'bg-rose-500' : pct > 70 ? 'bg-amber-500' : 'bg-emerald-500'

  return (
    <div className="flex items-center gap-2">
      <Icon className="h-3.5 w-3.5 flex-none text-txt-tertiary" />
      {!compact && <span className="w-8 flex-none text-2xs text-txt-secondary">{label}</span>}
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-overlay">
        <div className={clsx('h-full rounded-full transition-all duration-500', color)} style={{ width: `${pct}%` }} />
      </div>
      {!compact && <span className="w-10 text-right text-2xs tabular-nums text-txt-secondary">{pct.toFixed(0)}%</span>}
    </div>
  )
}

function GpuMetricCard({
  gpu,
  wide,
  onClick,
}: {
  gpu: GPUMetric
  wide: boolean
  onClick: (trigger: HTMLElement) => void
}) {
  const memoryPct = gpu.mem_total > 0 ? (gpu.mem_used / gpu.mem_total) * 100 : 0

  return (
    <button
      type="button"
      onClick={event => onClick(event.currentTarget)}
      aria-label={`View details for GPU ${gpu.index} ${gpu.name}`}
      className={clsx(
        'w-full rounded-md border border-border-subtle bg-surface-raised px-4 py-4 text-left transition-colors hover:border-accent/25 hover:bg-surface-overlay focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40',
        wide ? 'min-h-[7.5rem]' : 'h-[10.5rem]',
      )}
    >
      <div className={clsx(wide && 'grid gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(15rem,1.25fr)_auto] sm:items-center sm:gap-6')}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-xs font-semibold text-txt-primary">GPU {gpu.index}</div>
          <div className="truncate text-xs text-txt-secondary" title={gpu.name}>{gpu.name}</div>
        </div>
        <div className="flex items-center gap-1 rounded-md bg-accent/10 px-2 py-1 text-2xs text-accent">
          <span className="font-medium tabular-nums">{gpu.util.toFixed(0)}%</span>
          <span>util</span>
        </div>
      </div>

      <div className={clsx('space-y-2', wide ? 'mt-3 sm:mt-0' : 'mt-3')}>
        <UsageTrack label="Compute" value={gpu.util} tone="util" />
        <UsageTrack label="VRAM" value={memoryPct} tone="memory" />
      </div>

      <div className={clsx(
        'mt-3 flex items-center justify-between gap-3 text-2xs text-txt-secondary',
        wide && 'sm:mt-0 sm:flex-col sm:items-end sm:justify-center',
      )}>
        <span className="tabular-nums">
          {formatMemory(gpu.mem_used)} / {formatMemory(gpu.mem_total)}
        </span>
        <span className="inline-flex min-h-7 items-center gap-1 whitespace-nowrap rounded-md bg-accent/10 px-2 font-medium text-accent">
          View details
          <ChevronRight className="h-3 w-3" />
        </span>
      </div>
      </div>
    </button>
  )
}

function UsageTrack({ label, value, tone }: { label: string; value: number; tone: 'util' | 'memory' }) {
  const pct = Math.min(100, Math.max(0, value))
  const barColor = tone === 'memory'
    ? pct > 92 ? 'bg-rose-400' : pct > 75 ? 'bg-amber-400' : 'bg-cyan-400'
    : pct > 92 ? 'bg-rose-400' : pct > 75 ? 'bg-amber-400' : 'bg-emerald-400'

  return (
    <div className="flex items-center gap-2">
      <span className="w-12 flex-none text-2xs text-txt-secondary">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-overlay/80">
        <div className={clsx('h-full rounded-full transition-all duration-500', barColor)} style={{ width: `${pct}%` }} />
      </div>
      <span className="w-10 text-right text-2xs tabular-nums text-txt-secondary">{pct.toFixed(0)}%</span>
    </div>
  )
}

function GpuProcessDialog({
  gpu,
  loading,
  error,
  onRetry,
  onClose,
}: {
  gpu: GPUMetric | null
  loading: boolean
  error: string
  onRetry: () => void
  onClose: () => void
}) {
  const backdropPointerStartedRef = useRef(false)
  const dialogRef = useRef<HTMLDivElement>(null)
  const closeButtonRef = useRef<HTMLButtonElement>(null)
  const [expandedProcessKey, setExpandedProcessKey] = useState<string | null>(null)
  const [processDetailsByKey, setProcessDetailsByKey] = useState<Record<string, ProcessDetailLoadState>>({})
  const processDetailsControllersRef = useRef<Map<string, AbortController>>(new Map())
  const selectedGpuKey = gpu ? gpuKey(gpu) : ''

  useEffect(() => {
    for (const controller of processDetailsControllersRef.current.values()) {
      controller.abort()
    }
    processDetailsControllersRef.current.clear()
    setExpandedProcessKey(null)
    setProcessDetailsByKey({})
  }, [gpu, loading])

  useEffect(() => () => {
    for (const controller of processDetailsControllersRef.current.values()) {
      controller.abort()
    }
    processDetailsControllersRef.current.clear()
  }, [])

  const loadProcessDetails = useCallback(async (process: GPUProcessInfo, rowKey: string) => {
    if (processDetailsControllersRef.current.has(rowKey)) {
      return
    }
    const controller = new AbortController()
    processDetailsControllersRef.current.set(rowKey, controller)
    let timedOut = false
    const timeoutId = window.setTimeout(() => {
      timedOut = true
      controller.abort()
    }, PROCESS_DETAILS_REQUEST_TIMEOUT_MS)
    setProcessDetailsByKey(current => ({
      ...current,
      [rowKey]: { loading: true, data: null, error: '' },
    }))
    try {
      const details = await api.getGpuProcessDetails(process.pid, controller.signal)
      if (processDetailsControllersRef.current.get(rowKey) !== controller) {
        return
      }
      setProcessDetailsByKey(current => ({
        ...current,
        [rowKey]: { loading: false, data: details, error: '' },
      }))
    } catch (err) {
      if (
        processDetailsControllersRef.current.get(rowKey) !== controller
        || (controller.signal.aborted && !timedOut)
      ) {
        return
      }
      setProcessDetailsByKey(current => ({
        ...current,
        [rowKey]: {
          loading: false,
          data: null,
          error: timedOut
            ? 'Process details timed out. Check the connection and retry.'
            : errorMessage(err, 'Process details are unavailable.'),
        },
      }))
    } finally {
      window.clearTimeout(timeoutId)
      if (processDetailsControllersRef.current.get(rowKey) === controller) {
        processDetailsControllersRef.current.delete(rowKey)
      }
    }
  }, [])

  const toggleProcessDetails = useCallback((process: GPUProcessInfo, rowKey: string) => {
    if (loading) return
    if (expandedProcessKey === rowKey) {
      setExpandedProcessKey(null)
      const controller = processDetailsControllersRef.current.get(rowKey)
      if (controller) {
        controller.abort()
        processDetailsControllersRef.current.delete(rowKey)
        setProcessDetailsByKey(current => {
          const next = { ...current }
          delete next[rowKey]
          return next
        })
      }
      return
    }

    if (expandedProcessKey) {
      const previousController = processDetailsControllersRef.current.get(expandedProcessKey)
      if (previousController) {
        previousController.abort()
        processDetailsControllersRef.current.delete(expandedProcessKey)
        setProcessDetailsByKey(current => {
          const next = { ...current }
          delete next[expandedProcessKey]
          return next
        })
      }
    }
    setExpandedProcessKey(rowKey)
    const state = processDetailsByKey[rowKey]
    if (!state?.data && !state?.loading) {
      void loadProcessDetails(process, rowKey)
    }
  }, [expandedProcessKey, loadProcessDetails, loading, processDetailsByKey])

  useEffect(() => {
    if (!gpu) {
      return
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (document.querySelector('dialog[open]')) return
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab' || !dialogRef.current) {
        return
      }
      const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )).filter(element => element.getClientRects().length > 0)
      if (!focusable.length) {
        event.preventDefault()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const outsideDialog = !dialogRef.current.contains(document.activeElement)
      if (event.shiftKey && (document.activeElement === first || outsideDialog)) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (document.activeElement === last || outsideDialog)) {
        event.preventDefault()
        first.focus()
      }
    }

    const focusTimer = window.setTimeout(() => closeButtonRef.current?.focus(), 0)
    window.addEventListener('keydown', handleKeyDown)
    return () => {
      window.clearTimeout(focusTimer)
      window.removeEventListener('keydown', handleKeyDown)
    }
  }, [selectedGpuKey, onClose])

  if (!gpu) {
    return null
  }

  const memoryPct = gpu.mem_total > 0 ? (gpu.mem_used / gpu.mem_total) * 100 : 0
  const memoryFree = Math.max(0, gpu.mem_total - gpu.mem_used)
  const sortedProcesses = [...gpu.processes].sort(
    (left, right) => (right.memory_mb ?? -1) - (left.memory_mb ?? -1),
  )
  const knownMemoryProcesses = sortedProcesses.filter(
    (process): process is typeof process & { memory_mb: number } => process.memory_mb != null,
  )
  const processMemoryTotal = knownMemoryProcesses.reduce(
    (total, process) => total + Math.max(0, process.memory_mb),
    0,
  )
  const averageProcessMemory = knownMemoryProcesses.length
    ? processMemoryTotal / knownMemoryProcesses.length
    : null
  const reportedMemoryFree = gpu.mem_free == null ? memoryFree : Math.max(0, gpu.mem_free)

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onPointerDown={event => {
        backdropPointerStartedRef.current = event.target === event.currentTarget
      }}
      onClick={event => {
        if (backdropPointerStartedRef.current && event.target === event.currentTarget) {
          onClose()
        }
        backdropPointerStartedRef.current = false
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="gpu-detail-title"
        aria-describedby="gpu-detail-description"
        aria-busy={loading}
        className="flex max-h-[calc(100dvh-2rem)] w-full max-w-5xl flex-col overflow-hidden rounded-md border border-border-subtle bg-surface-raised shadow-md"
        onPointerDown={() => {
          backdropPointerStartedRef.current = false
        }}
        onClick={event => event.stopPropagation()}
      >
        <div className="shrink-0 flex items-start justify-between gap-4 border-b border-border-subtle px-4 py-3 sm:px-5 sm:py-4">
          <div className="min-w-0">
            <div className="text-xs uppercase tracking-[0.18em] text-txt-tertiary">GPU Detail</div>
            <div id="gpu-detail-title" className="mt-1 truncate text-base font-semibold text-txt-primary">
              GPU {gpu.index} | {gpu.name}
            </div>
            <div className="mt-1 flex min-w-0 items-center gap-1">
              <div id="gpu-detail-description" className="min-w-0 flex-1 truncate font-mono text-2xs text-txt-tertiary" title={gpu.uuid}>{gpu.uuid || 'UUID unavailable'}</div>
              {gpu.uuid && <CopyButton value={gpu.uuid} label="Copy GPU UUID" className="-my-1" />}
            </div>
          </div>
          <div className="flex flex-none items-center gap-1">
            <button
              type="button"
              onClick={onRetry}
              disabled={loading}
              aria-label="Refresh GPU details"
              title="Refresh GPU details"
              className="touch-target inline-flex h-11 w-11 items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-hover hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-wait disabled:opacity-50 sm:h-9 sm:w-9"
            >
              <RefreshCw className={clsx('h-4 w-4', loading && 'motion-safe:animate-spin')} />
            </button>
            <button
              ref={closeButtonRef}
              type="button"
              onClick={onClose}
              aria-label="Close GPU details"
              className="touch-target inline-flex h-11 w-11 items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-hover hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 sm:h-9 sm:w-9"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-4 sm:px-5">
          <section aria-labelledby="gpu-live-metrics-title">
            <h3 id="gpu-live-metrics-title" className="mb-2 flex items-center gap-2 text-xs font-semibold text-txt-primary">
              <Gauge className="h-3.5 w-3.5 text-txt-tertiary" />
              Live metrics
            </h3>
            <div className="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-border-subtle bg-border-subtle lg:grid-cols-4">
              <DetailMetric label="Compute" value={`${gpu.util.toFixed(0)}%`} tone="emerald" />
              <DetailMetric label="Memory I/O" value={formatOptionalMetric(gpu.mem_util, '%')} tone="sky" />
              <DetailMetric label="VRAM used" value={`${formatMemory(gpu.mem_used)} / ${formatMemory(gpu.mem_total)}`} tone="sky" />
              <DetailMetric label="VRAM free" value={formatMemory(reportedMemoryFree)} tone={memoryPct > 85 ? 'amber' : 'slate'} />
              <DetailMetric label="Temperature" value={formatOptionalMetric(gpu.temperature_c, ' °C')} tone={(gpu.temperature_c ?? 0) >= 80 ? 'amber' : 'slate'} icon={Thermometer} />
              <DetailMetric label="Power" value={formatPower(gpu.power_draw_w, gpu.power_limit_w)} tone="slate" icon={Zap} />
              <DetailMetric label="Fan" value={formatOptionalMetric(gpu.fan_speed_pct, '%')} tone="slate" icon={Fan} />
              <DetailMetric label="Processes" value={loading ? '--' : String(gpu.processes.length)} tone={gpu.processes.length ? 'emerald' : 'slate'} />
            </div>
          </section>

          <section aria-labelledby="gpu-device-information-title">
            <h3 id="gpu-device-information-title" className="mb-2 flex items-center gap-2 text-xs font-semibold text-txt-primary">
              <Cpu className="h-3.5 w-3.5 text-txt-tertiary" />
              Device information
            </h3>
            <dl className="grid grid-cols-2 gap-px overflow-hidden rounded-md border border-border-subtle bg-border-subtle lg:grid-cols-4">
              <DeviceDetail label="GPU index" value={String(gpu.index)} mono />
              <DeviceDetail label="PCI bus" value={gpu.pci_bus_id} mono />
              <DeviceDetail label="Driver" value={gpu.driver_version} mono />
              <DeviceDetail label="Performance state" value={gpu.performance_state} mono />
              <DeviceDetail label="Compute mode" value={gpu.compute_mode} />
              <DeviceDetail label="Graphics clock" value={formatOptionalMetric(gpu.graphics_clock_mhz, ' MHz')} mono />
              <DeviceDetail label="Memory clock" value={formatOptionalMetric(gpu.memory_clock_mhz, ' MHz')} mono />
              <DeviceDetail label="Process VRAM" value={knownMemoryProcesses.length ? `${formatMemory(processMemoryTotal)} total | ${formatMemory(averageProcessMemory)} avg` : ''} mono />
            </dl>
          </section>

          <section aria-labelledby="gpu-processes-title">
            <div className="mb-2 flex items-center justify-between gap-3">
              <h3 id="gpu-processes-title" className="flex items-center gap-2 text-xs font-semibold text-txt-primary">
                <Activity className="h-3.5 w-3.5 text-txt-tertiary" />
                GPU processes
              </h3>
              <span className="text-2xs tabular-nums text-txt-tertiary" aria-live="polite">
                {loading ? 'Refreshing' : `${gpu.processes.length} reported`}
              </span>
            </div>
          {error && (
            <div role="alert" className="mb-3 flex items-center justify-between gap-3 rounded-md border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-xs text-rose-700 dark:text-rose-300">
              <span>{error}</span>
              <button type="button" onClick={onRetry} className="touch-target inline-flex min-h-11 flex-none items-center rounded-md px-2 font-medium text-accent hover:text-accent-hover sm:min-h-0">Retry</button>
            </div>
          )}
          {loading && gpu.processes.length === 0 ? (
            <div className="flex items-center justify-center gap-2 rounded-md bg-surface-overlay/60 px-4 py-8 text-sm text-txt-tertiary">
              <RefreshCw className="h-4 w-4 animate-spin" /> Loading GPU processes…
            </div>
          ) : gpu.processes.length === 0 ? (
            <div className="rounded-md bg-surface-overlay/60 px-4 py-8 text-center text-sm text-txt-tertiary">
              No GPU processes are currently reported by NVIDIA for this GPU.
            </div>
          ) : (
            <div className="overflow-hidden rounded-md border border-border-subtle">
              <div>
                <div className="grid grid-cols-[56px_minmax(0,1fr)_64px_16px] gap-2 border-b border-border-subtle bg-surface-overlay/70 px-3 py-2 text-2xs uppercase tracking-[0.18em] text-txt-tertiary sm:grid-cols-[72px_96px_minmax(0,1fr)_80px_48px_16px] sm:gap-3 sm:px-4">
                  <span>PID</span>
                  <span className="hidden sm:block">User</span>
                  <span>Process</span>
                  <span className="text-right">VRAM</span>
                  <span className="hidden text-right sm:block">Share</span>
                  <span aria-hidden="true" />
                </div>
                {sortedProcesses.map(process => {
                  const rowKey = gpuProcessKey(process)
                  const expanded = expandedProcessKey === rowKey
                  const processDetailsState = processDetailsByKey[rowKey]
                  const processDetails = processDetailsState?.data
                  const displayName = processDetails?.process_name?.trim() || process.name
                  return (
                    <div key={rowKey} className="border-b border-border-subtle/80 last:border-b-0">
                      <button
                        type="button"
                        disabled={loading}
                        aria-expanded={expanded}
                        aria-label={`${expanded ? 'Hide' : 'View'} details for process ${process.pid} ${displayName}`}
                        onClick={() => toggleProcessDetails(process, rowKey)}
                        className="grid min-h-11 w-full grid-cols-[56px_minmax(0,1fr)_64px_16px] items-center gap-x-2 gap-y-0.5 px-3 py-3 text-left text-xs transition-colors hover:bg-surface-overlay/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40 disabled:cursor-wait disabled:opacity-50 sm:grid-cols-[72px_96px_minmax(0,1fr)_80px_48px_16px] sm:gap-3 sm:px-4 sm:text-sm"
                      >
                        <span className="row-span-2 truncate font-mono text-txt-secondary sm:row-span-1" title={String(process.pid)}>{process.pid >= 0 ? process.pid : '--'}</span>
                        <span className="col-start-2 row-start-2 truncate font-mono text-xs text-txt-secondary sm:col-auto sm:row-auto" title={processDetails?.user || 'Load details to view'}>
                          {processDetails?.user || '--'}
                        </span>
                        <span className="col-start-2 row-start-1 truncate text-txt-primary sm:col-auto sm:row-auto" title={process.name}>{displayName}</span>
                        <span className="row-span-2 text-right font-mono text-txt-secondary sm:row-span-1">{formatMemory(process.memory_mb)}</span>
                        <span className="hidden text-right font-mono text-txt-tertiary sm:block">
                          {process.memory_mb == null || gpu.mem_total <= 0
                            ? '--'
                            : formatPercent((process.memory_mb / gpu.mem_total) * 100)}
                        </span>
                        <ChevronDown className={clsx('row-span-2 h-4 w-4 text-txt-tertiary transition-transform sm:row-span-1', expanded && 'rotate-180')} />
                      </button>
                      {expanded && (
                        <GpuProcessMetadata
                          process={process}
                          state={processDetailsState}
                          onRetry={() => void loadProcessDetails(process, rowKey)}
                        />
                      )}
                    </div>
                  )
                })}
              </div>
            </div>
          )}
          </section>
        </div>
      </div>
    </div>
  )
}

function DetailMetric({
  label,
  value,
  tone,
  icon: Icon,
}: {
  label: string
  value: string
  tone: 'emerald' | 'sky' | 'amber' | 'slate'
  icon?: ElementType
}) {
  const toneClass = {
    emerald: 'text-emerald-700 dark:text-emerald-300',
    sky: 'text-sky-700 dark:text-sky-300',
    amber: 'text-amber-800 dark:text-amber-300',
    slate: 'text-txt-secondary',
  }[tone]

  return (
    <div className="min-w-0 bg-surface-raised px-3 py-3">
      <div className="flex items-center gap-1.5 text-2xs text-txt-tertiary">
        {Icon && <Icon className="h-3 w-3 flex-none" />}
        <span>{label}</span>
      </div>
      <div className={clsx('mt-1 truncate text-sm font-semibold tabular-nums', toneClass)} title={value}>{value}</div>
    </div>
  )
}

function GpuProcessMetadata({
  process,
  state,
  onRetry,
}: {
  process: GPUProcessInfo
  state?: ProcessDetailLoadState
  onRetry: () => void
}) {
  if (!state || state.loading) {
    return (
      <div className="flex items-center justify-center gap-2 border-t border-border-subtle/80 bg-surface-overlay/30 px-4 py-6 text-xs text-txt-tertiary" aria-live="polite">
        <RefreshCw className="h-3.5 w-3.5 motion-safe:animate-spin" /> Loading process details...
      </div>
    )
  }

  if (state.error || !state.data?.available) {
    return (
      <div role="alert" className="flex items-center justify-between gap-3 border-t border-border-subtle/80 bg-surface-overlay/30 px-4 py-4 text-xs text-txt-secondary">
        <span>{state.error || 'This process is no longer available on the host.'}</span>
        <button type="button" onClick={onRetry} className="touch-target inline-flex min-h-11 flex-none items-center rounded-md px-2 font-medium text-accent hover:text-accent-hover sm:min-h-0">
          Retry
        </button>
      </div>
    )
  }

  const details = state.data
  const startedAt = formatProcessStartedAt(details.created_at)
  const runtime = formatProcessRuntime(details.created_at)
  const hostMemory = details.host_memory_mb == null
    ? 'Not available'
    : `${formatMemory(details.host_memory_mb)}${
      details.host_memory_percent == null
        ? ''
        : ` (${formatOptionalMetric(details.host_memory_percent, '%')} RAM)`
    }`

  return (
    <div className="border-t border-border-subtle/80 bg-surface-overlay/30 px-4 py-3">
      <dl className="grid grid-cols-2 gap-x-5 gap-y-3 lg:grid-cols-6">
        <ProcessDetail label="Status" value={formatProcessStatus(details.status)} />
        <ProcessDetail label="Host RAM" value={hostMemory} mono />
        <ProcessDetail label="Started" value={startedAt} />
        <ProcessDetail label="Runtime" value={runtime} mono />
        <ProcessDetail label="Parent PID" value={formatOptionalInteger(details.parent_pid)} mono />
        <ProcessDetail label="Threads" value={formatOptionalInteger(details.thread_count)} mono />
      </dl>
      <div className="mt-3 grid gap-3 border-t border-border-subtle/80 pt-3 lg:grid-cols-2">
        <ProcessTextDetail label="Executable" value={details.executable} copyLabel={`Copy executable path for PID ${process.pid}`} />
        <ProcessTextDetail label="Working directory" value={details.working_directory} copyLabel={`Copy working directory for PID ${process.pid}`} />
        <ProcessTextDetail label="GPU-reported process" value={process.name} copyLabel={`Copy GPU process path for PID ${process.pid}`} />
        <ProcessTextDetail
          label={details.command_line_truncated ? 'Command line (truncated)' : 'Command line'}
          value={details.command_line}
          copyLabel={`Copy command line for PID ${process.pid}`}
          wide
        />
      </div>
    </div>
  )
}

function ProcessDetail({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="min-w-0">
      <dt className="text-2xs text-txt-tertiary">{label}</dt>
      <dd className={clsx('mt-0.5 truncate text-xs text-txt-primary', mono && 'font-mono')} title={value}>{value}</dd>
    </div>
  )
}

function ProcessTextDetail({
  label,
  value,
  copyLabel,
  wide = false,
}: {
  label: string
  value?: string
  copyLabel: string
  wide?: boolean
}) {
  const copyValue = value?.trim() || ''
  const displayValue = copyValue || 'Not available'
  return (
    <div className={clsx('min-w-0', wide && 'lg:col-span-2')}>
      <div className="flex items-center justify-between gap-2">
        <div className="text-2xs text-txt-tertiary">{label}</div>
        {copyValue && <CopyButton value={copyValue} label={copyLabel} size="xs" />}
      </div>
      <div className="mt-0.5 whitespace-pre-wrap break-all font-mono text-xs leading-5 text-txt-primary">{displayValue}</div>
    </div>
  )
}

function DeviceDetail({ label, value, mono = false }: { label: string; value?: string; mono?: boolean }) {
  const displayValue = value?.trim() || 'Not reported'
  return (
    <div className="min-w-0 bg-surface-raised px-3 py-2.5">
      <dt className="text-2xs text-txt-tertiary">{label}</dt>
      <dd className={clsx('mt-0.5 truncate text-xs text-txt-primary', mono && 'font-mono')} title={displayValue}>
        {displayValue}
      </dd>
    </div>
  )
}

function SummaryPill({ children }: { children: ReactNode }) {
  return (
    <span className="rounded-md bg-surface-overlay px-2.5 py-1 text-txt-secondary">
      {children}
    </span>
  )
}

function gpuKey(gpu: GPUMetric): string {
  return gpu.uuid || `${gpu.id}`
}

function gpuProcessKey(process: GPUProcessInfo): string {
  return `${process.pid}-${process.name}`
}

function formatProcessStatus(value: string | null | undefined): string {
  const status = value?.trim()
  if (!status) {
    return 'Not available'
  }
  return status.replace(/_/g, ' ')
}

function formatProcessStartedAt(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value <= 0) {
    return 'Not available'
  }
  const date = new Date(value * 1000)
  return Number.isNaN(date.getTime()) ? 'Not available' : date.toLocaleString()
}

function formatProcessRuntime(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value <= 0) {
    return 'Not available'
  }
  return formatElapsedDuration(Math.max(0, Date.now() / 1000 - value))
}

function formatOptionalInteger(value: number | null | undefined): string {
  return value == null || !Number.isFinite(value) || value < 0
    ? 'Not available'
    : Math.floor(value).toLocaleString()
}

function formatMemory(memoryMb: number | null | undefined): string {
  if (memoryMb == null || !Number.isFinite(memoryMb)) {
    return 'Unknown'
  }
  if (memoryMb <= 0) {
    return '0 MB'
  }
  if (memoryMb >= 1024) {
    return `${(memoryMb / 1024).toFixed(memoryMb >= 10240 ? 0 : 1)} GB`
  }
  return `${memoryMb.toFixed(memoryMb >= 100 ? 0 : 1)} MB`
}

function formatPercent(value: number): string {
  if (!Number.isFinite(value) || value <= 0) {
    return '0%'
  }
  return `${value >= 10 ? value.toFixed(0) : value.toFixed(1)}%`
}

function formatOptionalMetric(value: number | null | undefined, suffix: string): string {
  if (value == null || !Number.isFinite(value)) {
    return 'Not reported'
  }
  const formatted = Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(1).replace(/\.0$/, '')
  return `${formatted}${suffix}`
}

function formatPower(draw: number | null | undefined, limit: number | null | undefined): string {
  if (draw == null || !Number.isFinite(draw)) {
    return 'Not reported'
  }
  const drawText = formatOptionalMetric(draw, ' W')
  return limit == null || !Number.isFinite(limit)
    ? drawText
    : `${drawText} / ${formatOptionalMetric(limit, ' W')}`
}
