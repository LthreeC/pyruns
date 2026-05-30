import { useCallback, useState, type ElementType, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Activity,
  ArrowRight,
  ChevronRight,
  CheckCircle2,
  Cpu,
  Layers,
  MemoryStick,
  Wand2,
  X,
  XCircle,
} from 'lucide-react'
import clsx from 'clsx'
import { useDashboardStore, useMonitorStore, useWorkspaceStore } from '@/store'
import { usePolling } from '@/hooks/usePolling'
import StatusBadge from '@/components/shared/StatusBadge'
import { getWorkspaceStoragePath, getWorkspaceWorkingPath } from '@/utils/workspace'
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
  const navigate = useNavigate()
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null)
  const [activeGpuKey, setActiveGpuKey] = useState<string | null>(null)
  const refreshIntervalRaw = Number(workspace?.settings?.header_refresh_interval ?? 3)
  const refreshIntervalSec = Number.isFinite(refreshIntervalRaw) ? Math.max(1, refreshIntervalRaw) : 3

  const refreshDashboard = useCallback(async () => {
    await Promise.all([
      fetch(),
      api.getMetrics().then(setMetrics).catch(() => {}),
    ])
  }, [fetch])

  const openTaskInMonitor = useCallback((task: Task) => {
    void useMonitorStore.getState().selectTask(task.name)
    navigate('/monitor')
  }, [navigate])

  usePolling(refreshDashboard, refreshIntervalSec * 1000, true, true)

  const summary = data?.summary
  const isShellWorkspace = workspace?.workspace_kind === 'shell'
  const workspaceKindLabel = isShellWorkspace ? 'Shell Workspace' : 'Script Workspace'
  const workspaceName = workspace?.script_name || (isShellWorkspace ? '_shell_' : 'No workspace selected')
  const workspaceWorkingPath = getWorkspaceWorkingPath(workspace)
  const workspaceStoragePath = getWorkspaceStoragePath(workspace)
  const workspacePathSegments = splitPathSegments(workspaceWorkingPath)
  const activeGpu = metrics?.gpus.find(gpu => gpuKey(gpu) === activeGpuKey) ?? null
  const gpuCount = metrics?.gpus.length ?? 0
  const gpuProcessCount = metrics?.gpus.reduce((total, gpu) => total + gpu.processes.length, 0) ?? 0
  const averageGpuUtil = gpuCount
    ? (metrics?.gpus.reduce((total, gpu) => total + gpu.util, 0) ?? 0) / gpuCount
    : 0

  return (
    <>
      <div className="h-full overflow-y-auto bg-surface-base">
        <div className="flex w-full flex-col gap-5 px-5 py-5 2xl:px-8">
          <header className="flex flex-wrap items-center justify-between gap-4 border-b border-border-default pb-4">
            <div className="min-w-0 flex-1">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span className="inline-flex items-center rounded-md bg-accent/10 px-2 py-1 text-2xs font-medium text-accent">
                  {workspaceKindLabel}
                </span>
                <span className="truncate font-mono text-2xs text-txt-tertiary" title={workspaceWorkingPath || ''}>
                  {workspaceWorkingPath || 'Open a workspace to start'}
                </span>
              </div>
              <h1 className="text-xl font-semibold text-txt-primary">Dashboard</h1>
              <WorkspacePathTrail name={workspaceName} segments={workspacePathSegments} />
            </div>
            <button
              type="button"
              onClick={() => navigate('/generator')}
              className="inline-flex min-h-10 items-center justify-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-white transition-colors hover:bg-accent/90"
            >
              <Wand2 className="h-4 w-4" /> Start New Task
            </button>
          </header>

          <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
            {STAT_CARDS.map(({ key, label, icon: Icon, color }) => (
              <div key={key} className="rounded-md border border-border-default bg-surface-raised px-4 py-3">
                <div className="flex items-center gap-3">
                  <div className={clsx('rounded-md bg-surface-overlay p-2', color)}>
                    <Icon className="h-4 w-4" />
                  </div>
                  <div>
                    <div className="text-xl font-semibold tabular-nums text-txt-primary">
                      {loading ? '--' : (summary as Record<string, number> | undefined)?.[key] ?? 0}
                    </div>
                    <div className="text-2xs text-txt-tertiary">{label}</div>
                  </div>
                </div>
              </div>
            ))}
          </div>

          <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,2.2fr)_minmax(22rem,1fr)]">
            <section className="rounded-md border border-border-default bg-surface-raised">
              <div className="flex items-center justify-between border-b border-border-subtle px-4 py-3">
                <h2 className="text-sm font-medium text-txt-primary">Recent Tasks</h2>
                <button
                  type="button"
                  onClick={() => navigate('/manager')}
                  className="flex items-center gap-1 text-2xs text-txt-tertiary transition-colors hover:text-accent"
                >
                  View all <ArrowRight className="h-3 w-3" />
                </button>
              </div>
              <div className="divide-y divide-border-subtle">
                {!data?.recent_tasks?.length ? (
                  <div className="py-8 text-center text-xs text-txt-tertiary">No tasks yet</div>
                ) : (
                  data.recent_tasks.map(task => (
                    <TaskRow key={task.name} task={task} onClick={() => openTaskInMonitor(task)} />
                  ))
                )}
              </div>
            </section>

            <div className="flex flex-col gap-4">
              <section className="rounded-md border border-border-default bg-surface-raised p-4">
                <h2 className="mb-3 text-sm font-medium text-txt-primary">Workspace</h2>
                <div className="space-y-2 text-xs">
                  <InfoRow label="Mode" value={workspaceKindLabel} />
                  <InfoRow label="Script" value={workspace?.script_name || '--'} />
                  <InfoRow label="Templates" value={String(data?.template_count ?? 0)} />
                  <InfoRow label="Working" value={workspaceWorkingPath || '--'} mono />
                  <InfoRow label="Storage" value={workspaceStoragePath || '--'} mono />
                </div>
                <div className="mt-3">
                  <WorkspacePathTrail name={workspaceName} segments={workspacePathSegments} compact />
                </div>
                <button
                  type="button"
                  onClick={() => navigate('/generator')}
                  className="mt-4 flex w-full items-center justify-center gap-2 rounded-md bg-accent/10 px-3 py-2 text-xs font-medium text-accent transition-colors hover:bg-accent/20"
                >
                  <Wand2 className="h-3.5 w-3.5" /> Generate Tasks
                </button>
              </section>

              <section className="rounded-md border border-border-default bg-surface-raised p-4">
                <div className="flex items-start justify-between gap-3">
                  <div>
                    <h2 className="text-sm font-medium text-txt-primary">System</h2>
                    <p className="mt-1 text-2xs text-txt-tertiary">Auto-refreshes every {refreshIntervalSec}s.</p>
                  </div>
                  {gpuCount > 0 && (
                    <div className="rounded-md bg-sky-500/8 px-2.5 py-1 text-2xs text-sky-300">
                      {gpuCount} GPU{gpuCount > 1 ? 's' : ''} online
                    </div>
                  )}
                </div>

                {metrics ? (
                  <>
                    <div className="mt-3 space-y-2.5">
                      <MetricBar label="CPU" value={metrics.cpu_percent} icon={Cpu} />
                      <MetricBar label="RAM" value={metrics.mem_percent} icon={MemoryStick} />
                    </div>

                    <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-3 xl:grid-cols-1">
                      <SummaryChip label="GPU Util Avg" value={`${averageGpuUtil.toFixed(0)}%`} tone="sky" />
                      <SummaryChip label="GPU Processes" value={String(gpuProcessCount)} tone={gpuProcessCount ? 'emerald' : 'slate'} />
                      <SummaryChip label="Refresh" value={`${refreshIntervalSec}s`} tone="slate" />
                    </div>
                  </>
                ) : (
                  <div className="mt-3 rounded-md bg-surface-overlay/50 px-3 py-4 text-2xs text-txt-tertiary">
                    Loading system metrics...
                  </div>
                )}
              </section>
            </div>
          </div>

          <section className="rounded-md border border-border-default bg-surface-raised">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border-subtle px-4 py-3">
              <div>
                <h2 className="text-sm font-medium text-txt-primary">GPU Fleet</h2>
                <p className="mt-1 text-2xs text-txt-tertiary">
                  Click any GPU card to inspect active processes and VRAM usage.
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2 text-2xs">
                <SummaryPill>{gpuCount} GPU{gpuCount === 1 ? '' : 's'}</SummaryPill>
                <SummaryPill>{gpuProcessCount} active proc{gpuProcessCount === 1 ? '' : 's'}</SummaryPill>
              </div>
            </div>

            <div className="p-4">
              {metrics?.gpus?.length ? (
              <div className="grid grid-cols-1 gap-3 md:grid-cols-2 2xl:grid-cols-4">
                  {metrics.gpus.map(gpu => (
                    <GpuMetricCard
                      key={gpuKey(gpu)}
                      gpu={gpu}
                      onClick={() => setActiveGpuKey(gpuKey(gpu))}
                    />
                  ))}
                </div>
              ) : (
                <div className="rounded-md bg-surface-overlay/50 px-3 py-8 text-center text-2xs text-txt-tertiary">
                  No NVIDIA GPU metrics detected.
                </div>
              )}
            </div>
          </section>
        </div>
      </div>

      <GpuProcessDialog gpu={activeGpu} onClose={() => setActiveGpuKey(null)} />
    </>
  )
}

function TaskRow({ task, onClick }: { task: Task; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors hover:bg-surface-overlay"
    >
      <StatusBadge status={task.status as TaskStatus} />
      <span className="flex-1 truncate text-sm text-txt-primary">{task.name}</span>
      <span className="flex-none font-mono text-2xs text-txt-tertiary">{task.created_at}</span>
    </button>
  )
}

function MetricBar({ label, value, icon: Icon }: { label: string; value: number; icon: ElementType }) {
  const pct = Math.min(100, Math.max(0, value))
  const color = pct > 90 ? 'bg-rose-500' : pct > 70 ? 'bg-amber-500' : 'bg-emerald-500'

  return (
    <div className="flex items-center gap-2">
      <Icon className="h-3.5 w-3.5 flex-none text-txt-tertiary" />
      <span className="w-8 flex-none text-2xs text-txt-secondary">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-overlay">
        <div className={clsx('h-full rounded-full transition-all duration-500', color)} style={{ width: `${pct}%` }} />
      </div>
      <span className="w-10 text-right text-2xs tabular-nums text-txt-secondary">{pct.toFixed(0)}%</span>
    </div>
  )
}

function GpuMetricCard({ gpu, onClick }: { gpu: GPUMetric; onClick: () => void }) {
  const memoryPct = gpu.mem_total > 0 ? (gpu.mem_used / gpu.mem_total) * 100 : 0
  const processCount = gpu.processes.length

  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full rounded-md border border-border-subtle bg-surface-raised px-4 py-4 text-left transition-colors hover:border-accent/25 hover:bg-surface-overlay"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-xs font-semibold text-txt-primary">GPU {gpu.index}</div>
          <div className="truncate text-xs text-txt-secondary">{gpu.name}</div>
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

function GpuProcessDialog({ gpu, onClose }: { gpu: GPUMetric | null; onClose: () => void }) {
  if (!gpu) {
    return null
  }

  const memoryPct = gpu.mem_total > 0 ? (gpu.mem_used / gpu.mem_total) * 100 : 0

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4" onClick={onClose}>
      <div
        className="w-full max-w-5xl rounded-md border border-border-subtle bg-surface-raised shadow-md"
        onClick={event => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-border-subtle px-5 py-4">
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
            className="rounded-md p-1 text-txt-tertiary transition-colors hover:bg-surface-hover hover:text-txt-primary"
          >
            <X className="h-4 w-4" />
          </button>
        </div>

        <div className="grid grid-cols-1 gap-3 border-b border-border-subtle px-5 py-4 sm:grid-cols-3">
          <DetailChip label="Compute" value={`${gpu.util.toFixed(0)}%`} tone="emerald" />
          <DetailChip label="VRAM" value={`${formatMemory(gpu.mem_used)} / ${formatMemory(gpu.mem_total)}`} tone="sky" />
          <DetailChip label="Processes" value={String(gpu.processes.length)} tone={memoryPct > 85 ? 'amber' : 'slate'} />
        </div>

        <div className="max-h-[420px] overflow-y-auto px-5 py-4">
          {gpu.processes.length === 0 ? (
            <div className="rounded-md bg-surface-overlay/60 px-4 py-8 text-center text-sm text-txt-tertiary">
              No active compute processes reported by NVIDIA for this GPU.
            </div>
          ) : (
            <div className="overflow-hidden rounded-md border border-border-subtle">
              <div className="grid grid-cols-[88px_132px_minmax(0,1fr)_120px] gap-3 border-b border-border-subtle bg-surface-overlay/70 px-4 py-2 text-2xs uppercase tracking-[0.18em] text-txt-tertiary">
                <span>PID</span>
                <span>User</span>
                <span>Process</span>
                <span className="text-right">VRAM</span>
              </div>
              {gpu.processes.map(process => (
                <div
                  key={`${process.pid}-${process.name}`}
                  className="grid grid-cols-[88px_132px_minmax(0,1fr)_120px] gap-3 border-b border-border-subtle/80 px-4 py-3 text-sm last:border-b-0"
                >
                  <span className="font-mono text-txt-secondary">{process.pid >= 0 ? process.pid : '--'}</span>
                  <span className="truncate font-mono text-xs text-txt-secondary" title={process.user || 'unknown'}>
                    {process.user || 'unknown'}
                  </span>
                  <span className="truncate text-txt-primary" title={process.name}>{process.name}</span>
                  <span className="text-right font-mono text-txt-secondary">{formatMemory(process.memory_mb)}</span>
                </div>
              ))}
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

function SummaryChip({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone: 'emerald' | 'sky' | 'slate'
}) {
  const toneClass = {
    emerald: 'bg-emerald-500/10 text-emerald-300',
    sky: 'bg-sky-500/10 text-sky-300',
    slate: 'bg-surface-overlay text-txt-secondary',
  }[tone]

  return (
    <div className={clsx('rounded-md px-3 py-2.5', toneClass)}>
      <div className="text-2xs uppercase tracking-[0.18em] text-txt-tertiary">{label}</div>
      <div className="mt-1 text-sm font-semibold">{value}</div>
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

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-start gap-2">
      <span className="w-16 flex-none text-txt-tertiary">{label}</span>
      <span className={clsx('truncate text-txt-secondary', mono && 'font-mono text-2xs')} title={value}>{value}</span>
    </div>
  )
}

function WorkspacePathTrail({
  name,
  segments,
  compact = false,
}: {
  name: string
  segments: string[]
  compact?: boolean
}) {
  const visibleSegments = compact ? segments.slice(-3) : segments.slice(-5)
  const hiddenCount = Math.max(0, segments.length - visibleSegments.length)

  return (
    <div className={clsx('flex min-w-0 flex-wrap items-center gap-1.5 text-2xs text-txt-tertiary', compact ? 'mt-1' : 'mt-2')}>
      <span className="rounded-md bg-surface-overlay px-2 py-1 font-mono text-txt-secondary">{name}</span>
      {hiddenCount > 0 && <span className="font-mono">...</span>}
      {visibleSegments.map((segment, index) => (
        <span key={`${segment}-${index}`} className="inline-flex min-w-0 items-center gap-1">
          <span className="text-txt-tertiary">/</span>
          <span className="max-w-[180px] truncate rounded-md bg-surface-overlay px-2 py-1 font-mono" title={segment}>
            {segment}
          </span>
        </span>
      ))}
    </div>
  )
}

function gpuKey(gpu: GPUMetric): string {
  return gpu.uuid || `${gpu.id}`
}

function splitPathSegments(path?: string): string[] {
  if (!path) {
    return []
  }
  return path.split(/[\\/]+/).filter(Boolean)
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
