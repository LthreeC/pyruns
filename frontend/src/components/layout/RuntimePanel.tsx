import { useEffect, useMemo, useState } from 'react'
import {
  Check,
  Code2,
  FileText,
  Loader2,
  RefreshCw,
  ServerCog,
  X,
} from 'lucide-react'
import clsx from 'clsx'
import * as api from '@/api'
import type { RuntimeInfo } from '@/types'
import { useThemeStore, useWorkspaceStore } from '@/store'
import CodeTextEditor from '@/components/shared/CodeTextEditor'

interface RuntimePanelProps {
  open: boolean
  left: number
  onClose: () => void
}

type RuntimePage = 'python' | 'env'
type PythonRuntimeMode = 'follow' | 'conda' | 'python'

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

export default function RuntimePanel({ open, left, onClose }: RuntimePanelProps) {
  const refreshWorkspace = useWorkspaceStore(s => s.fetch)
  const theme = useThemeStore(s => s.theme)
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(null)
  const [envText, setEnvText] = useState('')
  const [pythonPath, setPythonPath] = useState('')
  const [condaEnv, setCondaEnv] = useState('')
  const [condaExecutable, setCondaExecutable] = useState('conda')
  const [runtimeMode, setRuntimeMode] = useState<PythonRuntimeMode>('follow')
  const [activePage, setActivePage] = useState<RuntimePage>('python')
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
  }

  const loadRuntime = async () => {
    setLoading(true)
    setError('')
    try {
      applyRuntimeState(await api.getRuntimeInfo())
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (open) {
      void loadRuntime()
    }
  }, [open])

  const saveRuntime = async (payload: Parameters<typeof api.updateRuntimeInfo>[0]) => {
    setSaving(true)
    setError('')
    try {
      applyRuntimeState(await api.updateRuntimeInfo(payload))
      await refreshWorkspace()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setSaving(false)
    }
  }

  const savePythonRuntime = () => {
    if (runtimeMode === 'conda') {
      if (!condaEnv) {
        setError('Choose a conda environment before saving.')
        return
      }
      void saveRuntime({
        conda_env: condaEnv,
        conda_executable: condaExecutable,
        python_executable: '',
      })
      return
    }
    if (runtimeMode === 'python') {
      void saveRuntime({
        python_executable: pythonPath,
        conda_env: '',
      })
      return
    }
    void saveRuntime({
      conda_env: '',
      python_executable: '',
    })
  }

  const chooseRuntimeMode = (mode: PythonRuntimeMode) => {
    setRuntimeMode(mode)
    setError('')
    if (mode === 'conda' && !condaEnv) {
      const activeConda = runtime?.conda.envs.find(env => env.active)?.name
      setCondaEnv(runtime?.conda_env || runtime?.process.conda_env || activeConda || runtime?.conda.envs[0]?.name || '')
    }
  }

  if (!open) {
    return null
  }

  const navItems: Array<{
    id: RuntimePage
    label: string
    icon: typeof ServerCog
    meta: string
    active: boolean
  }> = [
    {
      id: 'python',
      label: 'Python Runtime',
      icon: ServerCog,
      meta: currentLabel,
      active: runtimeMode !== 'follow',
    },
    {
      id: 'env',
      label: 'Workspace Env',
      icon: FileText,
      meta: envCount ? `${envCount} vars` : 'Empty',
      active: envCount > 0,
    },
  ]

  const modeItems: Array<{
    id: PythonRuntimeMode
    title: string
    detail: string
  }> = [
    {
      id: 'follow',
      title: 'Follow',
      detail: runtime?.process.conda_env || 'Use server process Python',
    },
    {
      id: 'conda',
      title: 'Conda',
      detail: condaAvailable ? (condaEnv || runtime?.process.conda_env || 'Choose environment') : 'Command not found',
    },
    {
      id: 'python',
      title: 'Python Path',
      detail: pythonPath ? pythonPath.split(/[\\/]/).pop() || 'Custom Python' : 'Pin executable',
    },
  ]

  return (
    <div
      className="fixed bottom-3 z-50 flex max-h-[calc(100vh-24px)] w-[760px] flex-col overflow-hidden rounded-lg border border-border bg-surface-raised shadow-xl"
      style={{ left, maxWidth: `calc(100vw - ${left + 12}px)` }}
    >
      <div className="flex items-center gap-2 border-b border-border-subtle px-4 py-3">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-txt-primary">Runtime</div>
          <div className="truncate text-xs text-txt-tertiary">{currentLabel}</div>
        </div>
        <button
          type="button"
          onClick={loadRuntime}
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

      <div className="grid min-h-0 flex-1 grid-cols-[190px_minmax(0,1fr)] overflow-hidden">
        <div className="border-r border-border-subtle bg-surface-overlay/35 p-2.5">
          {navItems.map(item => {
            const Icon = item.icon
            return (
              <button
                key={item.id}
                type="button"
                onClick={() => setActivePage(item.id)}
                className={clsx(
                  'mb-1.5 flex w-full items-start gap-2 rounded-md px-2.5 py-2.5 text-left transition-colors',
                  item.id === activePage
                    ? 'bg-accent/10 text-accent'
                    : 'text-txt-secondary hover:bg-surface-overlay hover:text-txt-primary'
                )}
              >
                <Icon className="mt-0.5 h-4 w-4 flex-none" />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-sm font-medium">{item.label}</span>
                  <span className="block truncate text-2xs text-txt-tertiary">{item.meta}</span>
                </span>
                <span className={clsx(
                  'mt-1.5 h-1.5 w-1.5 flex-none rounded-full',
                  item.active ? 'bg-status-completed' : 'bg-txt-tertiary'
                )} />
              </button>
            )
          })}
        </div>

        <div className="min-h-0 overflow-y-auto p-4">
          {error && (
            <div className="mb-3 rounded-md border border-status-failed/25 bg-status-failed/10 px-3 py-2 text-sm text-status-failed">
              {error}
            </div>
          )}

          {activePage === 'python' && (
            <section className="space-y-4">
              <div>
                <div className="flex items-center gap-2">
                  <Code2 className="h-4 w-4 text-accent" />
                  <h3 className="text-xs font-semibold uppercase tracking-[0.14em] text-txt-tertiary">Python Runtime</h3>
                </div>
                <p className="mt-1 truncate text-sm text-txt-secondary" title={runtime?.process.python_executable}>
                  Process Python: {runtime?.process.python_executable || 'unknown'}
                </p>
              </div>

              <div className="grid grid-cols-3 gap-2">
                {modeItems.map(item => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => chooseRuntimeMode(item.id)}
                    className={clsx(
                      'rounded-md border px-3 py-2.5 text-left transition-colors',
                      runtimeMode === item.id
                        ? 'border-accent bg-accent/10 text-accent'
                        : 'border-border-subtle bg-surface-raised text-txt-secondary hover:border-border hover:bg-surface-overlay hover:text-txt-primary'
                    )}
                  >
                    <span className="block text-sm font-medium">{item.title}</span>
                    <span className="mt-0.5 block truncate text-2xs text-txt-tertiary">{item.detail}</span>
                  </button>
                ))}
              </div>

              {runtimeMode === 'follow' && (
                <div className="rounded-md border border-border-subtle bg-surface-overlay/35 px-3 py-3 text-sm text-txt-secondary">
                  New tasks will use the Python environment that started the Pyruns server.
                </div>
              )}

              {runtimeMode === 'conda' && (
                <div className="space-y-3 rounded-md border border-border-subtle bg-surface-overlay/25 p-3">
                  <div className="space-y-1.5">
                    <label className="text-xs font-medium text-txt-secondary">Conda environment</label>
                    <select
                      value={condaEnv}
                      disabled={saving}
                      onChange={event => setCondaEnv(event.target.value)}
                      className="h-10 w-full rounded-md border border-border-subtle bg-surface-raised px-3 text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20 disabled:opacity-50"
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
                  <div className="rounded-md border border-border-subtle bg-surface-raised px-3 py-2.5">
                    <div className="text-xs font-medium text-txt-secondary">Resolved Python</div>
                    <div
                      className="mt-1 truncate font-mono text-sm text-txt-primary"
                      title={selectedConda?.python_executable || runtime?.process.python_executable}
                    >
                      {selectedConda?.python_executable || 'Choose a conda environment to preview Python path'}
                    </div>
                    {selectedConda?.path && (
                      <div className="mt-1 truncate text-2xs text-txt-tertiary" title={selectedConda.path}>
                        {selectedConda.path}
                      </div>
                    )}
                  </div>
                  {runtime?.conda.error && (
                    <div className="rounded-md border border-status-failed/25 bg-status-failed/10 px-3 py-2 text-sm text-status-failed">
                      {runtime.conda.error}
                    </div>
                  )}
                  <button
                    type="button"
                    onClick={() => setShowCondaAdvanced(value => !value)}
                    className="text-xs font-medium text-txt-tertiary transition-colors hover:text-txt-primary"
                  >
                    {showCondaAdvanced ? 'Hide advanced' : 'Advanced'}
                  </button>
                  {showCondaAdvanced && (
                    <div className="space-y-1.5 rounded-md border border-border-subtle bg-surface-raised p-3">
                      <label className="text-xs font-medium text-txt-secondary">Conda command</label>
                      <input
                        value={condaExecutable}
                        onChange={event => setCondaExecutable(event.target.value)}
                        className="h-10 w-full rounded-md border border-border-subtle bg-surface-raised px-3 font-mono text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
                        placeholder="conda"
                      />
                      <div className="text-2xs text-txt-tertiary">
                        Only change this when env discovery fails or conda is not on PATH.
                      </div>
                    </div>
                  )}
                </div>
              )}

              {runtimeMode === 'python' && (
                <div className="space-y-1.5 rounded-md border border-border-subtle bg-surface-overlay/25 p-3">
                  <label className="text-xs font-medium text-txt-secondary">Python executable path</label>
                  <input
                    value={pythonPath}
                    onChange={event => setPythonPath(event.target.value)}
                    className="h-10 w-full rounded-md border border-border-subtle bg-surface-raised px-3 font-mono text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/20"
                    placeholder={runtime?.process.python_executable || 'python path'}
                  />
                </div>
              )}

              <button
                type="button"
                onClick={savePythonRuntime}
                disabled={saving}
                className="inline-flex h-10 w-full items-center justify-center gap-1.5 rounded-md bg-accent px-3 text-sm font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
              >
                {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                Save Python Runtime
              </button>
            </section>
          )}

          {activePage === 'env' && (
            <section className="space-y-4">
              <div>
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <FileText className="h-4 w-4 text-accent" />
                    <h3 className="text-xs font-semibold uppercase tracking-[0.14em] text-txt-tertiary">Workspace Env</h3>
                  </div>
                  <span className="rounded-md bg-surface-overlay px-2 py-1 text-2xs text-txt-tertiary">
                    terminal &lt; workspace &lt; task
                  </span>
                </div>
                <p className="mt-1 text-xs leading-relaxed text-txt-secondary">
                  Safe .bashrc-style lines: KEY=value, export KEY=value, quotes, escaped spaces, and comments.
                </p>
              </div>
              <CodeTextEditor
                language="shell"
                value={envText}
                onChange={setEnvText}
                theme={codeMirrorTheme}
                className="runtime-env-editor"
                wrapStorageKey="pyruns.runtime.env.wrap"
                placeholder={'# CUDA_VISIBLE_DEVICES=0    export HF_HOME="/data/hf cache"'}
              />
              <div className="flex items-center justify-between gap-3">
                <div className="text-2xs text-txt-tertiary">
                  Saved to this workspace and reused after refresh.
                </div>
                <button
                  type="button"
                  onClick={() => saveRuntime({ global_env_text: envText })}
                  disabled={saving}
                  className="inline-flex h-9 items-center justify-center gap-1.5 rounded-md bg-accent px-4 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
                >
                  {saving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Check className="h-4 w-4" />}
                  Save Env
                </button>
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  )
}
