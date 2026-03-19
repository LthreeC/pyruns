import { useCallback, useState, type ElementType } from 'react'
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

  const refreshDashboard = useCallback(() => {
    void fetch()
    void api.getMetrics().then(setMetrics).catch(() => {})
  }, [fetch])

  const openTaskInMonitor = useCallback((task: Task) => {
    void useMonitorStore.getState().selectTask(task.name)
    navigate('/monitor')
  }, [navigate])

  usePolling(refreshDashboard, refreshIntervalSec * 1000, true, true)

  const summary = data?.summary
  const activeGpu = metrics?.gpus.find(gpu => gpuKey(gpu) === activeGpuKey) ?? null

  return (
    <>
      <div className="h-full overflow-y-auto p-6">
        <div className="mb-6">
          <h1 className="text-lg font-semibold text-txt-primary">Dashboard</h1>
          <p className="mt-1 text-xs text-txt-tertiary">
            {workspace?.script_name ? `Workspace: ${workspace.script_name}` : 'Welcome to Pyruns'}
          </p>
        </div>

        <div className="mb-6 grid grid-cols-2 gap-3 lg:grid-cols-4">
          {STAT_CARDS.map(({ key, label, icon: Icon, color }) => (
            <div key={key} className="flex items-center gap-3 rounded-lg border border-border-subtle bg-surface-raised p-4">
              <div className={clsx('rounded-md bg-surface-overlay p-2', color)}>
                <Icon className="h-4 w-4" />
              </div>
              <div>
                <div className="text-xl font-semibold tabular-nums text-txt-primary">
                  {loading ? '—' : (summary as Record<string, number> | undefined)?.[key] ?? 0}
                </div>
                <div className="text-2xs text-txt-tertiary">{label}</div>
              </div>
            </div>
          ))}
        </div>

        <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
          <div className="rounded-lg border border-border-subtle bg-surface-raised lg:col-span-2">
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
                <div className="px-4 py-8 text-center text-xs text-txt-tertiary">No tasks yet</div>
              ) : (
                data.recent_tasks.map(task => (
                  <TaskRow key={task.name} task={task} onClick={() => openTaskInMonitor(task)} />
                ))
              )}
            </div>
          </div>

          <div className="flex flex-col gap-4">
            {metrics && (
              <div className="rounded-lg border border-border-subtle bg-surface-raised p-4">
                <h2 className="mb-3 text-sm font-medium text-txt-primary">System</h2>
                <div className="space-y-2.5">
                  <MetricBar label="CPU" value={metrics.cpu_percent} icon={Cpu} />
                  <MetricBar label="RAM" value={metrics.mem_percent} icon={MemoryStick} />
                </div>

                <div className="mt-4 space-y-2">
                  {metrics.gpus?.length ? (
                    metrics.gpus.map(gpu => (
                      <GpuMetricCard
                        key={gpuKey(gpu)}
                        gpu={gpu}
                        onClick={() => setActiveGpuKey(gpuKey(gpu))}
                      />
                    ))
                  ) : (
                    <div className="rounded-lg border border-dashed border-border-subtle bg-surface-overlay/50 px-3 py-3 text-2xs text-txt-tertiary">
                      No NVIDIA GPU metrics detected.
                    </div>
                  )}
                </div>
              </div>
            )}

            <div className="rounded-lg border border-border-subtle bg-surface-raised p-4">
              <h2 className="mb-3 text-sm font-medium text-txt-primary">Workspace</h2>
              <div className="space-y-2 text-xs">
                <InfoRow label="Script" value={workspace?.script_name || '—'} />
                <InfoRow label="Templates" value={String(data?.template_count ?? 0)} />
                <InfoRow label="Path" value={workspace?.run_root || '—'} mono />
              </div>
              <button
                type="button"
                onClick={() => navigate('/generator')}
                className="mt-4 flex w-full items-center justify-center gap-2 rounded-md bg-accent/10 px-3 py-2 text-xs font-medium text-accent transition-colors hover:bg-accent/20"
              >
                <Wand2 className="h-3.5 w-3.5" /> Generate Tasks
              </button>
            </div>
          </div>
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
      <span className="w-8 text-right text-2xs tabular-nums text-txt-secondary">{pct.toFixed(0)}%</span>
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
      className="w-full rounded-lg border border-sky-500/15 bg-[linear-gradient(135deg,rgba(14,165,233,0.12),rgba(30,41,59,0.18))] px-3 py-3 text-left transition-colors hover:border-sky-400/30 hover:bg-[linear-gradient(135deg,rgba(14,165,233,0.18),rgba(30,41,59,0.28))]"
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-xs font-semibold text-txt-primary">GPU {gpu.index}</div>
          <div className="truncate text-2xs text-sky-100/80">{gpu.name}</div>
        </div>
        <div className="flex items-center gap-1 rounded-full border border-sky-400/20 bg-sky-500/10 px-2 py-1 text-2xs text-sky-200">
          <span className="font-medium tabular-nums">{gpu.util.toFixed(0)}%</span>
          <span>util</span>
        </div>
      </div>

      <div className="mt-3 space-y-2">
        <UsageTrack label="Compute" value={gpu.util} tone="util" />
        <UsageTrack label="VRAM" value={memoryPct} tone="memory" />
      </div>

      <div className="mt-3 flex items-center justify-between text-2xs text-txt-secondary">
        <span className="tabular-nums">
          {formatMemory(gpu.mem_used)} / {formatMemory(gpu.mem_total)}
        </span>
        <span className="inline-flex items-center gap-1">
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
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-sm" onClick={onClose}>
      <div
        className="w-full max-w-2xl rounded-2xl border border-border-subtle bg-surface-raised shadow-[0_24px_80px_-48px_rgba(15,23,42,0.95)]"
        onClick={event => event.stopPropagation()}
      >
        <div className="flex items-start justify-between gap-4 border-b border-border-subtle px-5 py-4">
          <div className="min-w-0">
            <div className="text-xs uppercase tracking-[0.18em] text-txt-tertiary">GPU Detail</div>
            <div className="mt-1 truncate text-base font-semibold text-txt-primary">
              GPU {gpu.index} · {gpu.name}
            </div>
            <div className="mt-1 truncate font-mono text-2xs text-txt-tertiary">{gpu.uuid}</div>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1 text-txt-tertiary transition-colors hover:bg-surface-hover hover:text-txt-primary"
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
            <div className="rounded-xl border border-dashed border-border-subtle bg-surface-overlay/60 px-4 py-8 text-center text-sm text-txt-tertiary">
              No active compute processes reported by NVIDIA for this GPU.
            </div>
          ) : (
            <div className="overflow-hidden rounded-xl border border-border-subtle">
              <div className="grid grid-cols-[96px_minmax(0,1fr)_140px] gap-3 border-b border-border-subtle bg-surface-overlay/70 px-4 py-2 text-2xs uppercase tracking-[0.18em] text-txt-tertiary">
                <span>PID</span>
                <span>Process</span>
                <span className="text-right">VRAM</span>
              </div>
              {gpu.processes.map(process => (
                <div
                  key={`${process.pid}-${process.name}`}
                  className="grid grid-cols-[96px_minmax(0,1fr)_140px] gap-3 border-b border-border-subtle/80 px-4 py-3 text-sm last:border-b-0"
                >
                  <span className="font-mono text-txt-secondary">{process.pid >= 0 ? process.pid : '—'}</span>
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
    emerald: 'border-emerald-500/20 bg-emerald-500/10 text-emerald-300',
    sky: 'border-sky-500/20 bg-sky-500/10 text-sky-300',
    amber: 'border-amber-500/20 bg-amber-500/10 text-amber-300',
    slate: 'border-border-subtle bg-surface-overlay text-txt-secondary',
  }[tone]

  return (
    <div className={clsx('rounded-xl border px-3 py-3', toneClass)}>
      <div className="text-2xs uppercase tracking-[0.18em] text-txt-tertiary">{label}</div>
      <div className="mt-1 truncate text-sm font-semibold">{value}</div>
    </div>
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

function gpuKey(gpu: GPUMetric): string {
  return gpu.uuid || `${gpu.id}`
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
