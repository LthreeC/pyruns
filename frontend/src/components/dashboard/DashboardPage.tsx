import { useCallback, useEffect, useState, type ElementType, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  ChevronRight,
  CheckCircle2,
  Cpu,
  Layers,
  MemoryStick,
  RefreshCw,
  Wand2,
  X,
  XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import { useDashboardStore, useMonitorStore, useToastStore, useWorkspaceStore } from '@/store'
import { usePolling } from '@/hooks/usePolling'
import StatusBadge from '@/components/shared/StatusBadge'
import { getWorkspaceWorkingPath } from '@/utils/workspace'
import { errorMessage } from '@/utils/errors'
import type { GPUMetric, Task, SystemMetrics } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import * as api from '@/api'

const STAT_CARDS: { key: string; label: string; icon: ElementType; color: string }[] = [
  { key: 'total', label: 'Total Tasks', icon: Layers, color: 'text-txt-secondary' },
  { key: 'running', label: 'Running', icon: Activity, color: 'text-amber-400' },
  { key: 'completed', label: 'Completed', icon: CheckCircle2, color: 'text-emerald-400' },
  { key: 'failed', label: 'Failed', icon: XCircle, color: 'text-rose-400' },
]

export default function DashboardPage() {
  const { data, loading, fetch } = useDashboardStore()
  const workspace = useWorkspaceStore(s => s.workspace)
  const notify = useToastStore(state => state.notify)
  const navigate = useNavigate()
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null)
  const [metricsError, setMetricsError] = useState('')
  const [activeGpuKey, setActiveGpuKey] = useState<string | null>(null)
  const [manualRefreshing, setManualRefreshing] = useState(false)
  const refreshIntervalRaw = Number(workspace?.settings?.header_refresh_interval ?? 3)
  const refreshIntervalSec = Number.isFinite(refreshIntervalRaw) ? Math.max(1, refreshIntervalRaw) : 3

  const refreshDashboard = useCallback(async () => {
    await Promise.all([
      fetch(),
      api.getMetrics()
        .then(nextMetrics => {
          setMetrics(nextMetrics)
          setMetricsError('')
        })
        .catch(err => {
          setMetricsError(errorMessage(err, 'System metrics unavailable.'))
        }),
    ])
  }, [fetch])

  const openTaskInMonitor = useCallback((task: Task) => {
    void useMonitorStore.getState().selectTask(task.name)
      .catch(err => notify({ tone: 'error', title: 'Could not load task logs', detail: errorMessage(err) }))
    navigate('/monitor')
  }, [navigate, notify])

  const handleManualRefresh = useCallback(async () => {
    setManualRefreshing(true)
    try {
      await refreshDashboard()
      notify({
        tone: 'success',
        title: 'Dashboard refreshed',
        detail: 'Task summary and system metrics are up to date.',
      })
    } catch (err) {
      notify({ tone: 'error', title: 'Could not refresh dashboard', detail: errorMessage(err) })
    } finally {
      setManualRefreshing(false)
    }
  }, [notify, refreshDashboard])

  usePolling(refreshDashboard, refreshIntervalSec * 1000, true, true)

  const summary = data?.summary
  const workspaceReady = workspace?.workspace_ready === true
  const isShellWorkspace = workspaceReady && workspace?.workspace_kind === 'shell'
  const workspaceKindLabel = !workspaceReady ? 'Workspace Needed' : isShellWorkspace ? 'Shell Workspace' : 'Script Workspace'
  const workspaceName = workspaceReady
    ? (workspace?.script_name || (isShellWorkspace ? '_shell_' : 'Workspace'))
    : 'Choose a workspace to start'
  const workspaceWorkingPath = getWorkspaceWorkingPath(workspace)
  const activeGpu = metrics?.gpus.find(gpu => gpuKey(gpu) === activeGpuKey) ?? null
  const gpuCount = metrics?.gpus.length ?? 0
  const gpuProcessCount = metrics?.gpus.reduce((total, gpu) => total + gpu.processes.length, 0) ?? 0
  const averageGpuUtil = gpuCount
    ? (metrics?.gpus.reduce((total, gpu) => total + gpu.util, 0) ?? 0) / gpuCount
    : 0
  const queuedCount = summary?.queued ?? 0
  const pendingCount = summary?.pending ?? 0
  const activeCount = (summary?.running ?? 0) + queuedCount

  return (
    <>
      <div className="h-full overflow-hidden bg-surface-base">
        <div className="flex h-full min-h-0 w-full flex-col gap-3 px-4 py-4 2xl:px-6">
          <header className="shrink-0 flex flex-wrap items-center justify-between gap-3 border-b border-border-default pb-3">
            <div className="min-w-0 flex-1">
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
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => void handleManualRefresh()}
                disabled={manualRefreshing || loading}
                className="inline-flex min-h-10 items-center justify-center gap-2 rounded-md border border-border-subtle bg-surface-raised px-3 py-2 text-sm font-medium text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary disabled:cursor-not-allowed disabled:opacity-60"
                title="Refresh dashboard now"
              >
                <RefreshCw className={clsx('h-4 w-4', manualRefreshing && 'animate-spin')} />
                Refresh
              </button>
              <button
                type="button"
                onClick={() => navigate(workspaceReady ? '/generator' : '/?launcher=1&mode=python')}
                className="inline-flex min-h-10 items-center justify-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent/90"
              >
                <Wand2 className="h-4 w-4" /> {workspaceReady ? 'Start New Task' : 'Choose Workspace'}
              </button>
            </div>
          </header>

          <div className="grid shrink-0 grid-cols-2 gap-2 lg:grid-cols-4">
            {STAT_CARDS.map(({ key, label, icon: Icon, color }) => (
              <div key={key} className="rounded-md border border-border-default bg-surface-raised px-3 py-2.5">
                <div className="flex items-center gap-3">
                  <div className={clsx('rounded-md bg-surface-overlay p-2', color)}>
                    <Icon className="h-4 w-4" />
                  </div>
                  <div>
                    <div className="text-lg font-semibold tabular-nums text-txt-primary">
                      {loading ? '--' : (summary as Record<string, number> | undefined)?.[key] ?? 0}
                    </div>
                    <div className="text-2xs text-txt-tertiary">{label}</div>
                  </div>
                </div>
                {key === 'running' && (
                  <div className="mt-3 flex flex-wrap gap-1.5 text-2xs">
                    <SummaryPill>{queuedCount} queued</SummaryPill>
                    <SummaryPill>{activeCount} active</SummaryPill>
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

          <div className="grid min-h-0 flex-1 grid-cols-1 gap-3 xl:grid-cols-[minmax(20rem,0.7fr)_minmax(42rem,1.3fr)]">
            <section className="flex min-h-0 flex-col overflow-hidden rounded-md border border-border-default bg-surface-raised">
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
                    className="flex items-center gap-1 rounded-md px-2 py-1 text-2xs text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-accent"
                  >
                    View all <ArrowRight className="h-3 w-3" />
                  </button>
                </div>
              </div>
              <div className="min-h-0 flex-1 divide-y divide-border-subtle overflow-y-auto">
                {!data?.recent_tasks?.length ? (
                  <div className="py-8 text-center text-xs text-txt-tertiary">No tasks yet</div>
                ) : (
                  data.recent_tasks.map(task => (
                    <TaskRow key={task.name} task={task} onClick={() => openTaskInMonitor(task)} />
                  ))
                )}
              </div>
            </section>

            <section className="flex min-h-0 flex-col overflow-hidden rounded-md border border-border-default bg-surface-raised">
              <div className="shrink-0 flex flex-wrap items-start justify-between gap-2 border-b border-border-subtle px-4 py-3">
                <div>
                  <h2 className="text-sm font-medium text-txt-primary">GPU & System</h2>
                  <p className="mt-1 text-2xs text-txt-tertiary">Auto-refreshes every {refreshIntervalSec}s. Click a GPU for processes.</p>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-2xs">
                  <SummaryPill>{gpuCount} GPU{gpuCount === 1 ? '' : 's'}</SummaryPill>
                  <SummaryPill>{gpuProcessCount} proc{gpuProcessCount === 1 ? '' : 's'}</SummaryPill>
                </div>
              </div>

              <div className="shrink-0 border-b border-border-subtle p-3">
                {metrics ? (
                  <>
                    {metricsError && (
                      <div className="mb-3 flex items-center gap-2 rounded-md bg-amber-500/10 px-3 py-2 text-2xs text-amber-400">
                        <AlertTriangle className="h-3.5 w-3.5 flex-none" />
                        <span className="min-w-0 break-words">Metrics refresh failed. Showing last values.</span>
                      </div>
                    )}
                    <div className="grid grid-cols-2 gap-2">
                      <ResourceTile label="CPU" value={`${metrics.cpu_percent.toFixed(0)}%`}>
                        <MetricBar label="CPU" value={metrics.cpu_percent} icon={Cpu} compact />
                      </ResourceTile>
                      <ResourceTile label="RAM" value={`${metrics.mem_percent.toFixed(0)}%`}>
                        <MetricBar label="RAM" value={metrics.mem_percent} icon={MemoryStick} compact />
                      </ResourceTile>
                      <ResourceTile label="GPU Avg" value={`${averageGpuUtil.toFixed(0)}%`} tone="sky" />
                      <ResourceTile label="GPU Proc" value={String(gpuProcessCount)} tone={gpuProcessCount ? 'emerald' : 'slate'} />
                    </div>
                  </>
                ) : (
                  <div
                    className={clsx(
                      'flex items-center gap-2 rounded-md px-3 py-4 text-2xs',
                      metricsError ? 'bg-amber-500/10 text-amber-400' : 'bg-surface-overlay/50 text-txt-tertiary',
                    )}
                  >
                    {metricsError && <AlertTriangle className="h-3.5 w-3.5 flex-none" />}
                    <span className="min-w-0 break-words">{metricsError || 'Loading system metrics...'}</span>
                  </div>
                )}
              </div>

              <div className="min-h-0 flex-1 overflow-y-auto p-3">
                {metrics?.gpus?.length ? (
                  <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
                    {metrics.gpus.map(gpu => (
                      <GpuMetricCard
                        key={gpuKey(gpu)}
                        gpu={gpu}
                        onClick={() => setActiveGpuKey(gpuKey(gpu))}
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
          </div>
        </div>
      </div>

      <GpuProcessDialog gpu={activeGpu} onClose={() => setActiveGpuKey(null)} />
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
      className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-surface-overlay focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/40"
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
  tone?: 'emerald' | 'sky' | 'slate'
  children?: ReactNode
}) {
  const toneClass = {
    emerald: 'bg-emerald-500/10 text-emerald-300',
    sky: 'bg-sky-500/10 text-sky-300',
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

function GpuMetricCard({ gpu, onClick }: { gpu: GPUMetric; onClick: () => void }) {
  const memoryPct = gpu.mem_total > 0 ? (gpu.mem_used / gpu.mem_total) * 100 : 0
  const memoryFree = Math.max(0, gpu.mem_total - gpu.mem_used)
  const processCount = gpu.processes.length
  const processMemoryTotal = gpu.processes.reduce((total, process) => total + Math.max(0, process.memory_mb), 0)
  const averageProcessMemory = processCount ? processMemoryTotal / processCount : 0
  const topProcess = getTopGpuProcess(gpu)

  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={`Inspect GPU ${gpu.index} ${gpu.name}`}
      className="w-full rounded-md border border-border-subtle bg-surface-raised px-4 py-4 text-left transition-colors hover:border-accent/25 hover:bg-surface-overlay focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
    >
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

      <div className="mt-3 space-y-2">
        <UsageTrack label="Compute" value={gpu.util} tone="util" />
        <UsageTrack label="VRAM" value={memoryPct} tone="memory" />
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2">
        <GpuMiniMetric label="Free" value={formatMemory(memoryFree)} />
        <GpuMiniMetric label="Proc VRAM" value={formatMemory(processMemoryTotal)} />
        <GpuMiniMetric label="Avg/proc" value={processCount ? formatMemory(averageProcessMemory) : '--'} />
      </div>

      {topProcess && (
        <div className="mt-3 min-w-0 rounded-md bg-surface-overlay/50 px-2.5 py-2 text-2xs text-txt-secondary" title={`${topProcess.user || 'unknown'} | ${topProcess.name}`}>
          <div className="flex min-w-0 items-center justify-between gap-2">
            <span className="min-w-0 truncate">
              Top proc: {topProcess.user || 'unknown'} / {topProcess.name}
            </span>
            <span className="flex-none font-mono">{formatMemory(topProcess.memory_mb)}</span>
          </div>
        </div>
      )}

      <div className="mt-3 flex items-center justify-between gap-3 text-2xs text-txt-secondary">
        <span className="tabular-nums">
          {formatMemory(gpu.mem_used)} / {formatMemory(gpu.mem_total)}
        </span>
        <span className="inline-flex items-center gap-1 whitespace-nowrap">
          {processCount} proc{processCount === 1 ? '' : 's'}
          <ChevronRight className="h-3 w-3" />
        </span>
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

function GpuMiniMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-md bg-surface-overlay px-2 py-1.5">
      <div className="truncate text-[10px] uppercase tracking-[0.12em] text-txt-tertiary">{label}</div>
      <div className="mt-0.5 truncate font-mono text-2xs text-txt-secondary" title={value}>{value}</div>
    </div>
  )
}

function GpuProcessDialog({ gpu, onClose }: { gpu: GPUMetric | null; onClose: () => void }) {
  useEffect(() => {
    if (!gpu) {
      return
    }

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [gpu, onClose])

  if (!gpu) {
    return null
  }

  const memoryPct = gpu.mem_total > 0 ? (gpu.mem_used / gpu.mem_total) * 100 : 0
  const memoryFree = Math.max(0, gpu.mem_total - gpu.mem_used)
  const sortedProcesses = [...gpu.processes].sort((left, right) => right.memory_mb - left.memory_mb)
  const processMemoryTotal = sortedProcesses.reduce((total, process) => total + Math.max(0, process.memory_mb), 0)

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="flex max-h-[calc(100vh-2rem)] w-full max-w-5xl flex-col overflow-hidden rounded-md border border-border-subtle bg-surface-raised shadow-md"
        onClick={event => event.stopPropagation()}
      >
        <div className="shrink-0 flex items-start justify-between gap-4 border-b border-border-subtle px-5 py-4">
          <div className="min-w-0">
            <div className="text-xs uppercase tracking-[0.18em] text-txt-tertiary">GPU Detail</div>
            <div className="mt-1 truncate text-base font-semibold text-txt-primary">
              GPU {gpu.index} | {gpu.name}
            </div>
            <div className="mt-1 truncate font-mono text-2xs text-txt-tertiary">{gpu.uuid}</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close GPU details"
            className="inline-flex h-9 w-9 flex-none items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-hover hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="shrink-0 grid grid-cols-1 gap-3 border-b border-border-subtle px-5 py-4 sm:grid-cols-2 lg:grid-cols-5">
          <DetailChip label="Compute" value={`${gpu.util.toFixed(0)}%`} tone="emerald" />
          <DetailChip label="VRAM" value={`${formatMemory(gpu.mem_used)} / ${formatMemory(gpu.mem_total)}`} tone="sky" />
          <DetailChip label="Free VRAM" value={formatMemory(memoryFree)} tone={memoryPct > 85 ? 'amber' : 'slate'} />
          <DetailChip label="Proc VRAM" value={formatMemory(processMemoryTotal)} tone={processMemoryTotal ? 'sky' : 'slate'} />
          <DetailChip label="Processes" value={String(gpu.processes.length)} tone={memoryPct > 85 ? 'amber' : 'slate'} />
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {gpu.processes.length === 0 ? (
            <div className="rounded-md bg-surface-overlay/60 px-4 py-8 text-center text-sm text-txt-tertiary">
              No active compute processes reported by NVIDIA for this GPU.
            </div>
          ) : (
            <div className="overflow-x-auto rounded-md border border-border-subtle">
              <div className="min-w-[640px]">
                <div className="grid grid-cols-[88px_132px_minmax(0,1fr)_120px_88px] gap-3 border-b border-border-subtle bg-surface-overlay/70 px-4 py-2 text-2xs uppercase tracking-[0.18em] text-txt-tertiary">
                  <span>PID</span>
                  <span>User</span>
                  <span>Process</span>
                  <span className="text-right">VRAM</span>
                  <span className="text-right">Share</span>
                </div>
                {sortedProcesses.map(process => (
                  <div
                    key={`${process.pid}-${process.name}`}
                    className="grid grid-cols-[88px_132px_minmax(0,1fr)_120px_88px] gap-3 border-b border-border-subtle/80 px-4 py-3 text-sm last:border-b-0"
                  >
                    <span className="font-mono text-txt-secondary">{process.pid >= 0 ? process.pid : '--'}</span>
                    <span className="truncate font-mono text-xs text-txt-secondary" title={process.user || 'unknown'}>
                      {process.user || 'unknown'}
                    </span>
                    <span className="truncate text-txt-primary" title={process.name}>{process.name}</span>
                    <span className="text-right font-mono text-txt-secondary">{formatMemory(process.memory_mb)}</span>
                    <span className="text-right font-mono text-txt-tertiary">{formatPercent(gpu.mem_total > 0 ? (process.memory_mb / gpu.mem_total) * 100 : 0)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function DetailChip({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone: 'emerald' | 'sky' | 'amber' | 'slate'
}) {
  const toneClass = {
    emerald: 'bg-emerald-500/10 text-emerald-300',
    sky: 'bg-sky-500/10 text-sky-300',
    amber: 'bg-amber-500/10 text-amber-300',
    slate: 'bg-surface-overlay text-txt-secondary',
  }[tone]

  return (
    <div className={clsx('rounded-md px-3 py-3', toneClass)}>
      <div className="text-2xs uppercase tracking-[0.18em] text-txt-tertiary">{label}</div>
      <div className="mt-1 truncate text-sm font-semibold">{value}</div>
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

function getTopGpuProcess(gpu: GPUMetric) {
  return gpu.processes.reduce<GPUMetric['processes'][number] | null>(
    (top, process) => !top || process.memory_mb > top.memory_mb ? process : top,
    null,
  )
}

function formatMemory(memoryMb: number): string {
  if (!Number.isFinite(memoryMb) || memoryMb <= 0) {
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
