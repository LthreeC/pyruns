import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Activity, CheckCircle2, XCircle, Layers, Wand2, ArrowRight, Cpu, MemoryStick } from 'lucide-react'
import clsx from 'clsx'
import { useDashboardStore, useWorkspaceStore } from '@/store'
import StatusBadge from '@/components/shared/StatusBadge'
import type { Task, SystemMetrics } from '@/types'
import type { TaskStatus } from '@/theme/tokens'
import * as api from '@/api'

const STAT_CARDS: { key: string; label: string; icon: React.ElementType; color: string }[] = [
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

  useEffect(() => {
    fetch()
    api.getMetrics().then(setMetrics).catch(() => {})
  }, [])

  const summary = data?.summary

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mb-6">
        <h1 className="text-lg font-semibold text-txt-primary">Dashboard</h1>
        <p className="text-xs text-txt-tertiary mt-1">
          {workspace?.script_name ? `Workspace: ${workspace.script_name}` : 'Welcome to Pyruns'}
        </p>
      </div>

      {/* Summary Cards */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3 mb-6">
        {STAT_CARDS.map(({ key, label, icon: Icon, color }) => (
          <div key={key} className="bg-surface-raised border border-border-subtle rounded-lg p-4 flex items-center gap-3">
            <div className={clsx('p-2 rounded-md bg-surface-overlay', color)}>
              <Icon className="w-4 h-4" />
            </div>
            <div>
              <div className="text-xl font-semibold text-txt-primary tabular-nums">
                {loading ? '—' : (summary as any)?.[key] ?? 0}
              </div>
              <div className="text-2xs text-txt-tertiary">{label}</div>
            </div>
          </div>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* Recent Tasks */}
        <div className="lg:col-span-2 bg-surface-raised border border-border-subtle rounded-lg">
          <div className="flex items-center justify-between px-4 py-3 border-b border-border-subtle">
            <h2 className="text-sm font-medium text-txt-primary">Recent Tasks</h2>
            <button
              type="button"
              onClick={() => navigate('/manager')}
              className="text-2xs text-txt-tertiary hover:text-accent flex items-center gap-1 transition-colors"
            >
              View all <ArrowRight className="w-3 h-3" />
            </button>
          </div>
          <div className="divide-y divide-border-subtle">
            {!data?.recent_tasks?.length ? (
              <div className="px-4 py-8 text-center text-xs text-txt-tertiary">No tasks yet</div>
            ) : (
              data.recent_tasks.map(task => (
                <TaskRow key={task.name} task={task} onClick={() => navigate('/monitor')} />
              ))
            )}
          </div>
        </div>

        {/* Right column */}
        <div className="flex flex-col gap-4">
          {metrics && (
            <div className="bg-surface-raised border border-border-subtle rounded-lg p-4">
              <h2 className="text-sm font-medium text-txt-primary mb-3">System</h2>
              <div className="space-y-2.5">
                <MetricBar label="CPU" value={metrics.cpu_percent} icon={Cpu} />
                <MetricBar label="RAM" value={metrics.mem_percent} icon={MemoryStick} />
                {metrics.gpus?.map(gpu => (
                  <MetricBar key={gpu.id} label={`GPU ${gpu.id}`} value={gpu.util} icon={Activity} />
                ))}
              </div>
            </div>
          )}

          <div className="bg-surface-raised border border-border-subtle rounded-lg p-4">
            <h2 className="text-sm font-medium text-txt-primary mb-3">Workspace</h2>
            <div className="space-y-2 text-xs">
              <InfoRow label="Script" value={workspace?.script_name || '—'} />
              <InfoRow label="Templates" value={String(data?.template_count ?? 0)} />
              <InfoRow label="Path" value={workspace?.run_root || '—'} mono />
            </div>
            <button
              type="button"
              onClick={() => navigate('/generator')}
              className="mt-4 w-full flex items-center justify-center gap-2 px-3 py-2 rounded-md bg-accent/10 text-accent text-xs font-medium hover:bg-accent/20 transition-colors"
            >
              <Wand2 className="w-3.5 h-3.5" /> Generate Tasks
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

function TaskRow({ task, onClick }: { task: Task; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-surface-overlay transition-colors text-left"
    >
      <StatusBadge status={task.status as TaskStatus} />
      <span className="text-sm text-txt-primary truncate flex-1">{task.name}</span>
      <span className="text-2xs text-txt-tertiary font-mono flex-none">{task.created_at}</span>
    </button>
  )
}

function MetricBar({ label, value, icon: Icon }: { label: string; value: number; icon: React.ElementType }) {
  const pct = Math.min(100, Math.max(0, value))
  const color = pct > 90 ? 'bg-rose-500' : pct > 70 ? 'bg-amber-500' : 'bg-emerald-500'
  return (
    <div className="flex items-center gap-2">
      <Icon className="w-3.5 h-3.5 text-txt-tertiary flex-none" />
      <span className="text-2xs text-txt-secondary w-8 flex-none">{label}</span>
      <div className="flex-1 h-1.5 bg-surface-overlay rounded-full overflow-hidden">
        <div className={clsx('h-full rounded-full transition-all duration-500', color)} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-2xs text-txt-secondary tabular-nums w-8 text-right">{pct.toFixed(0)}%</span>
    </div>
  )
}

function InfoRow({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-start gap-2">
      <span className="text-txt-tertiary w-16 flex-none">{label}</span>
      <span className={clsx('text-txt-secondary truncate', mono && 'font-mono text-2xs')} title={value}>{value}</span>
    </div>
  )
}
