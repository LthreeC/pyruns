import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Check,
  Loader2,
  RefreshCw,
  X,
} from 'lucide-react'
import clsx from 'clsx'
import * as api from '@/api'
import type { RuntimeInfo } from '@/types'
import { useThemeStore, useToastStore, useWorkspaceStore } from '@/store'
import CodeTextEditor from '@/components/shared/CodeTextEditor'
import { errorMessage } from '@/utils/errors'

interface RuntimePanelProps {
  open: boolean
  left: number
  onClose: () => void
}

type RuntimePage = 'python' | 'env' | 'gpu'
type PythonRuntimeMode = 'follow' | 'conda' | 'python'
type GpuTaskMode = 'single' | 'multi'

function quoteEnvValue(value: string) {
  if (value === '') {
    return '""'
  }
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(value)) {
    return value
  }
  if (!value.includes("'")) {
    return `'${value}'`
  }
  return `"${value
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\$/g, '\\$')
    .replace(/`/g, '\\`')}"`
}

function formatEnv(env: Record<string, string>) {
  return Object.entries(env || {})
    .map(([key, value]) => `${key}=${quoteEnvValue(String(value))}`)
    .join('\n')
}

function runtimeLabel(runtime: RuntimeInfo | null) {
  if (!runtime) {
    return 'Loading'
  }
  if (runtime.python_executable) {
    return `Python: ${runtime.python_executable.split(/[\\/]/).pop() || 'custom'}`
  }
  if (runtime.conda_env) {
    return `Conda: ${runtime.conda_env}`
  }
  return runtime.process.conda_env ? `Follow: ${runtime.process.conda_env}` : 'Follow process'
}

function modeFromRuntime(runtime: RuntimeInfo | null): PythonRuntimeMode {
  if (runtime?.python_executable) {
    return 'python'
  }
  if (runtime?.conda_env) {
    return 'conda'
  }
  return 'follow'
}

function parseDeviceIds(value: string) {
  return value
    .split(',')
    .map(item => item.trim())
    .filter(Boolean)
    .map(item => Number(item))
    .filter(item => Number.isInteger(item) && item >= 0)
}

function formatDeviceIds(value?: number[]) {
  return (value || []).join(',')
}

function numberInputValue(value: string, fallback: number, minimum = 0) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) {
    return fallback
  }
  return Math.max(minimum, parsed)
}

function boundedNumberInputValue(value: string, fallback: number, minimum: number, maximum: number) {
  return Math.min(maximum, numberInputValue(value, fallback, minimum))
}

export default function RuntimePanel({ open, left, onClose }: RuntimePanelProps) {
  const refreshWorkspace = useWorkspaceStore(s => s.fetch)
  const theme = useThemeStore(s => s.theme)
  const panelRef = useRef<HTMLDivElement>(null)
  const notify = useToastStore(state => state.notify)
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null)
  const [envText, setEnvText] = useState('')
  const [pythonPath, setPythonPath] = useState('')
  const [condaEnv, setCondaEnv] = useState('')
  const [condaExecutable, setCondaExecutable] = useState('conda')
  const [runtimeMode, setRuntimeMode] = useState<PythonRuntimeMode>('follow')
  const [activePage, setActivePage] = useState<RuntimePage>('python')
  const [gpuSchedulerEnabled, setGpuSchedulerEnabled] = useState(false)
  const [gpuTaskMode, setGpuTaskMode] = useState<GpuTaskMode>('single')
  const [gpuCount, setGpuCount] = useState('1')
  const [gpuDeviceIds, setGpuDeviceIds] = useState('')
  const [gpuMemoryUsedPct, setGpuMemoryUsedPct] = useState('40')
  const [gpuMinFreeMemoryGb, setGpuMinFreeMemoryGb] = useState('40')
  const [gpuComputeUsedPct, setGpuComputeUsedPct] = useState('30')
  const [gpuStableSeconds, setGpuStableSeconds] = useState('15')
  const [gpuMaxWaitHours, setGpuMaxWaitHours] = useState(48)
  const [gpuMaxTasksPerGpu, setGpuMaxTasksPerGpu] = useState('1')
  const [gpuRespectCudaVisibleDevices, setGpuRespectCudaVisibleDevices] = useState(true)
  const [showCondaAdvanced, setShowCondaAdvanced] = useState(false)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const condaAvailable = !!runtime?.conda.available
  const envCount = Object.keys(runtime?.global_env || {}).length
  const currentLabel = useMemo(() => runtimeLabel(runtime), [runtime])
  const codeMirrorTheme = theme === 'dark' ? 'dark' : 'light'
  const selectedConda = useMemo(() => {
    const found = runtime?.conda.envs.find(env => env.name === condaEnv)
    if (found) {
      return found
    }
    if (condaEnv && condaEnv === runtime?.process.conda_env) {
      return {
        name: runtime.process.conda_env,
        path: runtime.process.conda_prefix,
        python_executable: runtime.process.python_executable,
        active: true,
      }
    }
    return null
  }, [runtime, condaEnv])

  const applyRuntimeState = (next: RuntimeInfo) => {
    setRuntime(next)
    setEnvText(formatEnv(next.global_env))
    setPythonPath(next.python_executable)
    setCondaEnv(next.conda_env)
    setCondaExecutable(next.conda_executable || 'conda')
    setRuntimeMode(modeFromRuntime(next))
    setGpuSchedulerEnabled(next.gpu_scheduler?.enabled ?? false)
    setGpuTaskMode(next.gpu_scheduler?.task_mode === 'multi' ? 'multi' : 'single')
    setGpuCount(String(next.gpu_scheduler?.gpus_per_task ?? 1))
    setGpuDeviceIds(formatDeviceIds(next.gpu_scheduler?.device_ids))
    setGpuMemoryUsedPct(String(next.gpu_scheduler?.memory_used_pct ?? 40))
    setGpuMinFreeMemoryGb(String(next.gpu_scheduler?.min_free_memory_gb ?? 40))
    setGpuComputeUsedPct(String(next.gpu_scheduler?.compute_used_pct ?? 30))
    setGpuStableSeconds(String(next.gpu_scheduler?.stable_seconds ?? 15))
    setGpuMaxWaitHours(Math.max(1, Math.round((next.gpu_scheduler?.max_wait_seconds ?? 172800) / 3600)))
    setGpuMaxTasksPerGpu(String(next.gpu_scheduler?.max_tasks_per_gpu ?? 1))
    setGpuRespectCudaVisibleDevices(next.gpu_scheduler?.respect_cuda_visible_devices ?? true)
  }

  const loadRuntime = async (showFeedback = false) => {
    setLoading(true)
    setError('')
    try {
      applyRuntimeState(await api.getRuntimeInfo())
    } catch (err) {
      const message = errorMessage(err, 'Could not refresh runtime.')
      setError(message)
      if (showFeedback) {
        notify({ tone: 'error', title: 'Could not refresh runtime', detail: message })
      }
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (open) {
      void loadRuntime()
    }
  }, [open])

  useEffect(() => {
    if (!open) {
      return
    }

    const handleDocumentClick = (event: MouseEvent) => {
      const panel = panelRef.current
      const target = event.target
      if (panel && target instanceof Node && panel.contains(target)) {
        return
      }
      onClose()
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
      }
    }

    const clickListenerTimer = window.setTimeout(() => {
      document.addEventListener('click', handleDocumentClick)
    }, 0)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      window.clearTimeout(clickListenerTimer)
      document.removeEventListener('click', handleDocumentClick)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open, onClose])

  const saveRuntime = async (
    payload: Parameters<typeof api.updateRuntimeInfo>[0],
    successTitle = 'Runtime saved',
  ) => {
    setSaving(true)
    setError('')
    try {
      applyRuntimeState(await api.updateRuntimeInfo(payload))
      await refreshWorkspace()
      notify({ tone: 'success', title: successTitle })
    } catch (err) {
      const message = errorMessage(err, 'Could not save runtime settings.')
      setError(message)
      notify({ tone: 'error', title: 'Could not save runtime', detail: message })
    } finally {
      setSaving(false)
    }
  }

  const savePythonRuntime = () => {
    if (runtimeMode === 'conda') {
      if (!condaEnv) {
        const message = 'Choose a conda environment before saving.'
        setError(message)
        notify({ tone: 'error', title: 'Conda environment required', detail: message })
        return
      }
      void saveRuntime({
        conda_env: condaEnv,
        conda_executable: condaExecutable,
        python_executable: '',
      }, 'Python runtime saved')
      return
    }
    if (runtimeMode === 'python') {
      void saveRuntime({
        python_executable: pythonPath,
        conda_env: '',
      }, 'Python runtime saved')
      return
    }
    void saveRuntime({
      conda_env: '',
      python_executable: '',
    }, 'Python runtime saved')
  }

  const chooseRuntimeMode = (mode: PythonRuntimeMode) => {
    setRuntimeMode(mode)
    setError('')
    if (mode === 'conda' && !condaEnv) {
      const activeConda = runtime?.conda.envs.find(env => env.active)?.name
      setCondaEnv(runtime?.conda_env || runtime?.process.conda_env || activeConda || runtime?.conda.envs[0]?.name || '')
    }
  }

  const chooseGpuTaskMode = (mode: GpuTaskMode) => {
    setGpuTaskMode(mode)
  }

  const saveGpuScheduler = () => {
    void saveRuntime({
      gpu_scheduler: {
        enabled: gpuSchedulerEnabled,
        task_mode: gpuTaskMode,
        gpus_per_task: gpuTaskMode === 'multi' ? numberInputValue(gpuCount, 1, 1) : 1,
        device_ids: parseDeviceIds(gpuDeviceIds),
        memory_used_pct: boundedNumberInputValue(gpuMemoryUsedPct, 40, 0, 100),
        min_free_memory_gb: numberInputValue(gpuMinFreeMemoryGb, 40, 0),
        compute_used_pct: boundedNumberInputValue(gpuComputeUsedPct, 30, 0, 100),
        stable_seconds: numberInputValue(gpuStableSeconds, 15, 0),
        max_wait_seconds: gpuMaxWaitHours * 3600,
        max_tasks_per_gpu: numberInputValue(gpuMaxTasksPerGpu, 1, 1),
        sample_interval_seconds: runtime?.gpu_scheduler?.sample_interval_seconds ?? 2,
        respect_cuda_visible_devices: gpuRespectCudaVisibleDevices,
      },
    }, 'GPU scheduler saved')
  }

  if (!open) {
    return null
  }

  const modeItems: Array<{
    id: PythonRuntimeMode
    title: string
  }> = [
    {
      id: 'follow',
      title: 'Follow',
    },
    {
      id: 'conda',
      title: 'Conda',
    },
    {
      id: 'python',
      title: 'Path',
    },
  ]

  return (
    <div
      ref={panelRef}
      role="dialog"
      aria-label="Runtime settings"
      className="fixed bottom-3 z-50 flex max-h-[calc(100vh-24px)] w-[620px] flex-col overflow-hidden rounded-lg border border-border bg-surface-raised shadow-xl"
      style={{ left, maxWidth: `calc(100vw - ${left + 12}px)` }}
      onClick={event => event.stopPropagation()}
    >
      <div className="flex h-10 items-center gap-2 border-b border-border-subtle px-3">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          <div className="text-sm font-semibold text-txt-primary">Runtime</div>
          <div className="truncate rounded-md bg-surface-overlay px-2 py-0.5 text-2xs text-txt-secondary">
            {currentLabel}
          </div>
        </div>
        <div className="inline-flex rounded-md bg-surface-overlay p-0.5">
          {(['python', 'env', 'gpu'] as RuntimePage[]).map(page => (
            <button
              key={page}
              type="button"
              onClick={() => setActivePage(page)}
              className={clsx(
                'inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium transition-colors',
                activePage === page
                  ? 'bg-surface-raised text-accent shadow-sm'
                  : 'text-txt-secondary hover:text-txt-primary'
              )}
            >
              {page === 'python' ? 'Python' : page === 'env' ? 'Env' : 'GPU'}
              {page === 'env' && envCount > 0 && <span className="h-1.5 w-1.5 rounded-full bg-status-completed" />}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => void loadRuntime(true)}
          disabled={loading || saving}
          className="rounded-md p-2 text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary disabled:opacity-50"
          aria-label="Reload runtime"
        >
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="rounded-md p-2 text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
          aria-label="Close runtime panel"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {error && (
          <div className="mb-3 rounded-md bg-status-failed/10 px-3 py-2 text-sm text-status-failed">
            {error}
          </div>
        )}

        {activePage === 'python' && (
          <section className="space-y-3">
            <div className="flex items-center justify-between gap-3">
              <div className="inline-flex rounded-md bg-surface-overlay p-0.5">
                {modeItems.map(item => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => chooseRuntimeMode(item.id)}
                    className={clsx(
                      'rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
                      runtimeMode === item.id
                        ? 'bg-surface-raised text-accent shadow-sm'
                        : 'text-txt-secondary hover:text-txt-primary'
                    )}
                  >
                    {item.title}
                  </button>
                ))}
              </div>
              <button
                type="button"
                onClick={savePythonRuntime}
                disabled={saving}
                className="inline-flex h-8 items-center justify-center gap-1.5 rounded-md bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
              >
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                Save
              </button>
            </div>

            {runtimeMode === 'follow' && (
              <div className="space-y-1 border-l border-border-subtle pl-3">
                <div className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Python</div>
                <div className="truncate font-mono text-sm text-txt-primary" title={runtime?.process.python_executable}>
                  {runtime?.process.python_executable || 'unknown'}
                </div>
              </div>
            )}

            {runtimeMode === 'conda' && (
              <div className="space-y-3">
                <div>
                  <label className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Conda</label>
                  <select
                    value={condaEnv}
                    disabled={saving}
                    onChange={event => setCondaEnv(event.target.value)}
                    className="h-9 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15 disabled:opacity-50"
                  >
                    <option value="">Choose environment</option>
                    {runtime?.process.conda_env && !runtime.conda.envs.some(env => env.name === runtime.process.conda_env) && (
                      <option value={runtime.process.conda_env}>
                        {runtime.process.conda_env} (current)
                      </option>
                    )}
                    {runtime?.conda.envs.map(env => (
                      <option key={env.name} value={env.name}>
                        {env.name}{env.active ? ' (active)' : ''}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-0.5 border-l border-border-subtle pl-3">
                  <div className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Python</div>
                  <div
                    className="truncate font-mono text-sm text-txt-primary"
                    title={selectedConda?.python_executable || runtime?.process.python_executable}
                  >
                    {selectedConda?.python_executable || 'Choose a conda environment to preview Python path'}
                  </div>
                  {selectedConda?.path && (
                    <div className="truncate text-2xs text-txt-tertiary" title={selectedConda.path}>
                      {selectedConda.path}
                    </div>
                  )}
                </div>
                {runtime?.conda.error && (
                  <div className="rounded-md bg-status-failed/10 px-3 py-2 text-sm text-status-failed">
                    {runtime.conda.error}
                  </div>
                )}
                <button
                  type="button"
                  onClick={() => setShowCondaAdvanced(value => !value)}
                  className="text-2xs font-medium text-txt-tertiary transition-colors hover:text-txt-primary"
                >
                  {showCondaAdvanced ? 'Hide conda command' : 'Conda command'}
                </button>
                {showCondaAdvanced && (
                  <input
                    value={condaExecutable}
                    onChange={event => setCondaExecutable(event.target.value)}
                    className="h-8 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 font-mono text-xs text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15"
                    placeholder="conda"
                  />
                )}
              </div>
            )}

            {runtimeMode === 'python' && (
              <div>
                <label className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Python path</label>
                <input
                  value={pythonPath}
                  onChange={event => setPythonPath(event.target.value)}
                  className="h-9 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 font-mono text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15"
                  placeholder={runtime?.process.python_executable || 'python path'}
                />
              </div>
            )}
          </section>
        )}

        {activePage === 'env' && (
          <section className="space-y-3">
            <CodeTextEditor
              language="shell"
              value={envText}
              onChange={setEnvText}
              theme={codeMirrorTheme}
              className="runtime-env-editor"
              wrapStorageKey="pyruns.runtime.env.wrap"
              compactToolbar
              placeholder="KEY=value"
            />
            <div className="flex items-center justify-end">
              <button
                type="button"
                onClick={() => saveRuntime({ global_env_text: envText }, 'Workspace env saved')}
                disabled={saving}
                className="inline-flex h-8 items-center justify-center gap-1.5 rounded-md bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
              >
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                Save
              </button>
            </div>
          </section>
        )}

        {activePage === 'gpu' && (
          <section className="space-y-4">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <div className="text-sm font-semibold text-txt-primary">GPU Scheduler</div>
                <div className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">
                  Queue admission
                </div>
              </div>
              <div className="flex items-center gap-2">
                <div className="inline-flex rounded-md bg-surface-overlay p-0.5">
                  {[
                    { id: false, label: 'Off' },
                    { id: true, label: 'Auto' },
                  ].map(item => (
                    <button
                      key={String(item.id)}
                      type="button"
                      onClick={() => setGpuSchedulerEnabled(item.id)}
                      className={clsx(
                        'h-7 rounded-md px-3 text-xs font-medium transition-colors',
                        gpuSchedulerEnabled === item.id
                          ? 'bg-surface-raised text-accent shadow-sm'
                          : 'text-txt-secondary hover:text-txt-primary'
                      )}
                    >
                      {item.label}
                    </button>
                  ))}
                </div>
                <button
                  type="button"
                  onClick={saveGpuScheduler}
                  disabled={saving || !runtime}
                  className="inline-flex h-8 items-center justify-center gap-1.5 rounded-md bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
                >
                  {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                  Save
                </button>
              </div>
            </div>

            <div className="space-y-2">
              <div className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Task uses</div>
              <div className="inline-flex rounded-md bg-surface-overlay p-0.5">
                {[
                  { id: 'single' as GpuTaskMode, label: 'One GPU' },
                  { id: 'multi' as GpuTaskMode, label: 'Custom Count' },
                ].map(item => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => chooseGpuTaskMode(item.id)}
                    className={clsx(
                      'h-8 rounded-md px-3 text-xs font-medium transition-colors',
                      gpuTaskMode === item.id
                        ? 'bg-surface-raised text-accent shadow-sm'
                        : 'text-txt-secondary hover:text-txt-primary'
                    )}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">GPUs per task</span>
                <input
                  type="number"
                  min={1}
                  value={gpuTaskMode === 'multi' ? gpuCount : '1'}
                  disabled={gpuTaskMode !== 'multi'}
                  onChange={event => setGpuCount(event.target.value)}
                  className="h-9 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15 disabled:opacity-50"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">GPU pool</span>
                <input
                  value={gpuDeviceIds}
                  onChange={event => setGpuDeviceIds(event.target.value)}
                  className="h-9 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 font-mono text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15"
                  placeholder="auto"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Memory used below</span>
                <div className="flex h-9 overflow-hidden rounded-md border border-border-subtle bg-surface-overlay focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/15">
                  <input
                    type="number"
                    min={0}
                    max={100}
                    value={gpuMemoryUsedPct}
                    onChange={event => setGpuMemoryUsedPct(event.target.value)}
                    className="min-w-0 flex-1 bg-transparent px-2.5 text-sm text-txt-primary outline-none"
                  />
                  <span className="flex w-10 items-center justify-center border-l border-border-subtle text-xs text-txt-tertiary">%</span>
                </div>
              </label>
              <label className="block">
                <span className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Free memory at least</span>
                <div className="flex h-9 overflow-hidden rounded-md border border-border-subtle bg-surface-overlay focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/15">
                  <input
                    type="number"
                    min={0}
                    value={gpuMinFreeMemoryGb}
                    onChange={event => setGpuMinFreeMemoryGb(event.target.value)}
                    className="min-w-0 flex-1 bg-transparent px-2.5 text-sm text-txt-primary outline-none"
                  />
                  <span className="flex w-12 items-center justify-center border-l border-border-subtle text-xs text-txt-tertiary">GiB</span>
                </div>
              </label>
              <label className="block">
                <span className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Compute below</span>
                <div className="flex h-9 overflow-hidden rounded-md border border-border-subtle bg-surface-overlay focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/15">
                  <input
                    type="number"
                    min={0}
                    max={100}
                    value={gpuComputeUsedPct}
                    onChange={event => setGpuComputeUsedPct(event.target.value)}
                    className="min-w-0 flex-1 bg-transparent px-2.5 text-sm text-txt-primary outline-none"
                  />
                  <span className="flex w-10 items-center justify-center border-l border-border-subtle text-xs text-txt-tertiary">%</span>
                </div>
              </label>
              <label className="block">
                <span className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Stable for</span>
                <div className="flex h-9 overflow-hidden rounded-md border border-border-subtle bg-surface-overlay focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/15">
                  <input
                    type="number"
                    min={0}
                    value={gpuStableSeconds}
                    onChange={event => setGpuStableSeconds(event.target.value)}
                    className="min-w-0 flex-1 bg-transparent px-2.5 text-sm text-txt-primary outline-none"
                  />
                  <span className="flex w-12 items-center justify-center border-l border-border-subtle text-xs text-txt-tertiary">sec</span>
                </div>
              </label>
              <label className="block">
                <span className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Max wait</span>
                <div className="flex h-9 overflow-hidden rounded-md border border-border-subtle bg-surface-overlay focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/15">
                  <input
                    type="number"
                    min={1}
                    value={gpuMaxWaitHours}
                    onChange={event => setGpuMaxWaitHours(numberInputValue(event.target.value, 48, 1))}
                    className="min-w-0 flex-1 bg-transparent px-2.5 text-sm text-txt-primary outline-none"
                  />
                  <span className="flex w-12 items-center justify-center border-l border-border-subtle text-xs text-txt-tertiary">hour</span>
                </div>
              </label>
              <label className="block">
                <span className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Same GPU concurrency</span>
                <input
                  type="number"
                  min={1}
                  value={gpuMaxTasksPerGpu}
                  onChange={event => setGpuMaxTasksPerGpu(event.target.value)}
                  className="h-9 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15"
                />
              </label>
            </div>

            <label className="flex items-center gap-2 text-sm text-txt-secondary">
              <input
                type="checkbox"
                checked={gpuRespectCudaVisibleDevices}
                onChange={event => setGpuRespectCudaVisibleDevices(event.target.checked)}
                className="h-4 w-4 rounded border-border-subtle bg-surface-overlay text-accent focus:ring-accent/25"
              />
              <span>Do not override task CUDA_VISIBLE_DEVICES</span>
            </label>
          </section>
        )}
      </div>
    </div>
  )
}
