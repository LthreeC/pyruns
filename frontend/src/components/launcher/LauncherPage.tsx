import { useEffect, useCallback, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { FileCode, FolderOpen, ChevronRight, Rocket, ArrowRight, FileSearch, FolderPlus } from 'lucide-react'
import clsx from 'clsx'
import { useLauncherStore, useWorkspaceStore } from '@/store'
import type { ScriptCandidate, ConfigCandidate } from '@/types'
import * as api from '@/api'

function pathName(path: string) {
  return path.split(/[\\/]/).filter(Boolean).pop() || path
}

export default function LauncherPage({ onClose }: { onClose: () => void }) {
  const {
    scripts, configs, selectedScript, selectedConfig, step, loading,
    fetchScripts, selectScript, selectConfig, openWorkspace,
  } = useLauncherStore()
  const setWorkspace = useWorkspaceStore(state => state.setWorkspace)
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [manualScriptPath, setManualScriptPath] = useState('')
  const [manualConfigPath, setManualConfigPath] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    void fetchScripts()
    // Pre-select from URL params
    const scriptParam = searchParams.get('script')
    if (scriptParam) {
      setManualScriptPath(scriptParam)
      void selectScript(scriptParam)
    }
  }, [])

  const handleOpen = useCallback(async () => {
    setError('')
    try {
      await openWorkspace()
      onClose()
      navigate('/')
    } catch (err: any) {
      setError(err.message)
    }
  }, [openWorkspace, onClose, navigate])

  const handleSkipConfig = useCallback(async () => {
    setError('')
    selectConfig('')
    try {
      await useLauncherStore.getState().openWorkspace()
      onClose()
      navigate('/')
    } catch (err: any) {
      setError(err.message)
    }
  }, [selectConfig, onClose, navigate])

  const handleManualScript = useCallback(async () => {
    const scriptPath = manualScriptPath.trim()
    if (!scriptPath) {
      setError('Enter a Python script path.')
      return
    }

    setError('')
    try {
      await selectScript(scriptPath)
    } catch (err: any) {
      setError(err.message)
    }
  }, [manualScriptPath, selectScript])

  const handleManualConfig = useCallback(() => {
    const configPath = manualConfigPath.trim()
    if (!configPath) {
      void handleSkipConfig()
      return
    }
    setError('')
    selectConfig(configPath)
  }, [handleSkipConfig, manualConfigPath, selectConfig])

  const handlePickScript = useCallback(async () => {
    setError('')
    try {
      const workspace = await api.pickLauncherScript()
      setWorkspace(workspace)
      onClose()
      navigate('/')
    } catch (err: any) {
      setError(err.message)
    }
  }, [navigate, onClose, setWorkspace])

  const handlePickShellRoot = useCallback(async () => {
    setError('')
    try {
      const workspace = await api.pickLauncherShellRoot()
      setWorkspace(workspace)
      onClose()
      navigate('/generator')
    } catch (err: any) {
      setError(err.message)
    }
  }, [navigate, onClose, setWorkspace])

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/70 backdrop-blur-sm">
      <div className="bg-surface-raised border border-border rounded-xl shadow-2xl w-full max-w-2xl max-h-[80vh] flex flex-col overflow-hidden">
        {/* Header */}
        <div className="flex items-center gap-3 px-6 py-4 border-b border-border-subtle">
          <Rocket className="w-5 h-5 text-accent" />
          <div>
            <h2 className="text-sm font-semibold text-zinc-100">Launch Workspace</h2>
            <p className="text-2xs text-zinc-500 mt-0.5">Select a script and configuration to get started</p>
          </div>
        </div>

        {/* Steps indicator */}
        <div className="flex items-center gap-2 px-6 py-3 border-b border-border-subtle">
          <StepIndicator num={1} label="Script" active={step === 0} done={step > 0} />
          <ChevronRight className="w-3 h-3 text-zinc-600" />
          <StepIndicator num={2} label="Config" active={step === 1} done={step > 1} />
          <ChevronRight className="w-3 h-3 text-zinc-600" />
          <StepIndicator num={3} label="Open" active={step === 2} done={false} />
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4">
          {error && (
            <div className="mb-3 rounded-md border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
              {error}
            </div>
          )}

          {loading && (
            <div className="flex items-center justify-center py-12">
              <div className="text-xs text-zinc-500 animate-pulse">Loading...</div>
            </div>
          )}

          {!loading && step === 0 && (
            <div className="space-y-3">
              {scripts.length === 0 ? (
                <div className="text-center py-12 text-xs text-zinc-600">
                  No Python scripts found in the current directory.
                </div>
              ) : (
                scripts.map(script => (
                  <ScriptItem
                    key={script.script_path || script.workspace_path}
                    script={script}
                    onClick={() => selectScript(script.script_path)}
                  />
                ))
              )}
              <div className="rounded-lg border border-border-subtle bg-surface-overlay/50 p-3">
                <div className="flex items-center gap-2">
                  <FileSearch className="h-4 w-4 text-zinc-500" />
                  <input
                    value={manualScriptPath}
                    onChange={event => setManualScriptPath(event.target.value)}
                    onKeyDown={event => {
                      if (event.key === 'Enter') {
                        event.preventDefault()
                        void handleManualScript()
                      }
                    }}
                    placeholder="Absolute or relative path to train.py"
                    className="min-w-0 flex-1 rounded-md border border-border-subtle bg-surface-raised px-2.5 py-1.5 text-xs font-mono text-zinc-200 outline-none transition-colors focus:border-border"
                  />
                  <button
                    onClick={() => void handleManualScript()}
                    className="rounded-md bg-accent px-3 py-1.5 text-xs font-medium text-white transition-colors hover:bg-accent-hover"
                  >
                    Use Path
                  </button>
                </div>
                <div className="mt-2 flex flex-wrap gap-2">
                  <button
                    onClick={() => void handlePickScript()}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border-subtle px-2.5 py-1.5 text-2xs text-zinc-400 transition-colors hover:text-zinc-200"
                  >
                    <FileSearch className="h-3.5 w-3.5" />
                    Browse Script
                  </button>
                  <button
                    onClick={() => void handlePickShellRoot()}
                    className="inline-flex items-center gap-1.5 rounded-md border border-border-subtle px-2.5 py-1.5 text-2xs text-zinc-400 transition-colors hover:text-zinc-200"
                  >
                    <FolderPlus className="h-3.5 w-3.5" />
                    Open Shell Folder
                  </button>
                </div>
              </div>
            </div>
          )}

          {!loading && step === 1 && (
            <div className="space-y-1">
              <div className="text-2xs text-zinc-500 mb-3">
                Select a config for <span className="text-zinc-300 font-medium">{pathName(selectedScript)}</span>
              </div>
              {configs.length === 0 ? (
                <div className="text-center py-8">
                  <p className="text-xs text-zinc-600 mb-3">No config files found</p>
                  <button
                    onClick={handleSkipConfig}
                    className="text-xs text-accent hover:text-accent-hover transition-colors"
                  >
                    Continue without config →
                  </button>
                </div>
              ) : (
                <>
                  {configs.map(config => (
                    <ConfigItem
                      key={config.path}
                      config={config}
                      onClick={() => selectConfig(config.path)}
                    />
                  ))}
                  <button
                    onClick={handleSkipConfig}
                    className="w-full text-left px-3 py-2 text-2xs text-zinc-600 hover:text-zinc-400 transition-colors"
                  >
                    Skip — use workspace default
                  </button>
                </>
              )}
              <div className="mt-3 rounded-lg border border-border-subtle bg-surface-overlay/50 p-3">
                <div className="flex items-center gap-2">
                  <FileCode className="h-4 w-4 text-zinc-500" />
                  <input
                    value={manualConfigPath}
                    onChange={event => setManualConfigPath(event.target.value)}
                    onKeyDown={event => {
                      if (event.key === 'Enter') {
                        event.preventDefault()
                        handleManualConfig()
                      }
                    }}
                    placeholder="Optional path to config.yaml"
                    className="min-w-0 flex-1 rounded-md border border-border-subtle bg-surface-raised px-2.5 py-1.5 text-xs font-mono text-zinc-200 outline-none transition-colors focus:border-border"
                  />
                  <button
                    onClick={handleManualConfig}
                    className="rounded-md border border-border-subtle px-3 py-1.5 text-xs font-medium text-zinc-300 transition-colors hover:text-zinc-100"
                  >
                    Use Config
                  </button>
                </div>
              </div>
            </div>
          )}

          {!loading && step === 2 && (
            <div className="flex flex-col items-center py-8 gap-4">
              <div className="p-3 rounded-full bg-accent/10">
                <FolderOpen className="w-6 h-6 text-accent" />
              </div>
              <div className="text-center">
                <p className="text-sm text-zinc-200 font-medium">Ready to launch</p>
                <p className="text-2xs text-zinc-500 mt-1 font-mono">{pathName(selectedScript)}</p>
                {selectedConfig && (
                  <p className="text-2xs text-zinc-600 mt-0.5 font-mono">{pathName(selectedConfig)}</p>
                )}
              </div>
              <button
                onClick={handleOpen}
                disabled={loading}
                className="flex items-center gap-2 px-6 py-2.5 rounded-md bg-accent text-white text-sm font-medium hover:bg-accent-hover transition-colors disabled:opacity-50"
              >
                Open Workspace <ArrowRight className="w-4 h-4" />
              </button>
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-between px-6 py-3 border-t border-border-subtle">
          {step > 0 ? (
            <button
              onClick={() => useLauncherStore.setState({ step: step - 1 })}
              className="text-xs text-zinc-500 hover:text-zinc-300 transition-colors"
            >
              ← Back
            </button>
          ) : <div />}
          <button
            onClick={onClose}
            className="text-xs text-zinc-600 hover:text-zinc-400 transition-colors"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}

function StepIndicator({ num, label, active, done }: { num: number; label: string; active: boolean; done: boolean }) {
  return (
    <div className="flex items-center gap-1.5">
      <span className={clsx(
        'w-5 h-5 rounded-full flex items-center justify-center text-2xs font-medium',
        active ? 'bg-accent text-white' : done ? 'bg-accent/20 text-accent' : 'bg-surface-overlay text-zinc-600'
      )}>
        {done ? '✓' : num}
      </span>
      <span className={clsx('text-xs', active ? 'text-zinc-200' : 'text-zinc-500')}>{label}</span>
    </div>
  )
}

function ScriptItem({ script, onClick }: { script: ScriptCandidate; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="w-full flex items-center gap-3 px-3 py-2.5 rounded-md hover:bg-surface-overlay transition-colors text-left group"
    >
      <FileCode className="w-4 h-4 text-zinc-500 group-hover:text-accent transition-colors flex-none" />
      <div className="flex-1 min-w-0">
        <div className="text-sm text-zinc-200 truncate">{script.label}</div>
        <div className="text-2xs text-zinc-600 font-mono truncate">{script.script_path}</div>
      </div>
      {script.source === 'workspace' || script.source === 'workspace+file' ? (
        <span className="text-2xs text-accent/60 flex-none">workspace</span>
      ) : null}
      <ChevronRight className="w-3.5 h-3.5 text-zinc-600 group-hover:text-zinc-400 flex-none" />
    </button>
  )
}

function ConfigItem({ config, onClick }: { config: ConfigCandidate; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="w-full flex items-center gap-3 px-3 py-2.5 rounded-md hover:bg-surface-overlay transition-colors text-left group"
    >
      <FileCode className="w-4 h-4 text-zinc-500 group-hover:text-accent transition-colors flex-none" />
      <div className="flex-1 min-w-0">
        <div className="text-sm text-zinc-200">{config.label}</div>
      </div>
      {config.kind === 'workspace_default' && (
        <span className="text-2xs text-zinc-600 flex-none">default</span>
      )}
      <ChevronRight className="w-3.5 h-3.5 text-zinc-600 group-hover:text-zinc-400 flex-none" />
    </button>
  )
}
