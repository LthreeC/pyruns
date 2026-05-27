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
    scripts, configs, selectedScript, selectedConfig, requiresConfigTemplate, step, loading,
    fetchScripts, selectScript, selectConfig, openWorkspace,
  } = useLauncherStore()
  const setWorkspace = useWorkspaceStore(state => state.setWorkspace)
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [manualScriptPath, setManualScriptPath] = useState('')
  const [manualConfigPath, setManualConfigPath] = useState('')
  const [manualShellRootPath, setManualShellRootPath] = useState('')
  const [launchMode, setLaunchMode] = useState<'python' | 'shell'>('python')
  const [error, setError] = useState('')
  const scriptPathReady = manualScriptPath.trim().length > 0
  const shellPathReady = manualShellRootPath.trim().length > 0

  useEffect(() => {
    void fetchScripts()
    // Pre-select from URL params
    const scriptParam = searchParams.get('script')
    if (scriptParam) {
      setLaunchMode('python')
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
    if (requiresConfigTemplate) {
      setError('Choose or enter a YAML config path first.')
      return
    }
    selectConfig('')
    try {
      await useLauncherStore.getState().openWorkspace()
      onClose()
      navigate('/')
    } catch (err: any) {
      setError(err.message)
    }
  }, [requiresConfigTemplate, selectConfig, onClose, navigate])

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

  const handleSelectScript = useCallback(async (scriptPath: string) => {
    setError('')
    try {
      await selectScript(scriptPath)
    } catch (err: any) {
      setError(err.message)
    }
  }, [selectScript])

  const handleSelectConfig = useCallback((configPath: string) => {
    setError('')
    selectConfig(configPath)
  }, [selectConfig])

  const handleManualConfig = useCallback(() => {
    const configPath = manualConfigPath.trim()
    if (!configPath) {
      if (requiresConfigTemplate) {
        setError('Choose or enter a YAML config path first.')
        return
      }
      void handleSkipConfig()
      return
    }
    setError('')
    handleSelectConfig(configPath)
  }, [handleSkipConfig, handleSelectConfig, manualConfigPath, requiresConfigTemplate])

  const handlePickScript = useCallback(async () => {
    setError('')
    try {
      const selection = await api.pickLauncherScriptPath()
      setManualScriptPath(selection.script_path)
      await selectScript(selection.script_path)
    } catch (err: any) {
      setError(err.message)
    }
  }, [selectScript])

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

  const handleManualShellRoot = useCallback(async () => {
    const shellPath = manualShellRootPath.trim()
    if (!shellPath) {
      setError('Enter a folder path.')
      return
    }

    setError('')
    try {
      const workspace = await api.openLauncherShellRoot(shellPath)
      setWorkspace(workspace)
      onClose()
      navigate('/generator')
    } catch (err: any) {
      setError(err.message)
    }
  }, [manualShellRootPath, navigate, onClose, setWorkspace])

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60">
      <div className="flex max-h-[80vh] w-full max-w-2xl flex-col overflow-hidden rounded-md border border-border bg-surface-raised shadow-md">
        {/* Header */}
        <div className="flex items-center gap-3 px-6 py-4 border-b border-border-subtle">
          <Rocket className="w-5 h-5 text-accent" />
          <div>
            <h2 className="text-sm font-semibold text-zinc-100">Launch Workspace</h2>
            <p className="text-2xs text-zinc-500 mt-0.5">Choose a workspace type</p>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-4">
          {error && (
            <div className="mb-3 rounded-md border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
              {error}
            </div>
          )}

          {step === 0 && (
            <div className="space-y-3">
              <LaunchChoiceTabs launchMode={launchMode} onChange={setLaunchMode} />

              {launchMode === 'python' ? (
                <>
                  <ModeActionPanel
                    launchMode={launchMode}
                    pathValue={manualScriptPath}
                    pathReady={scriptPathReady}
                    onPathChange={setManualScriptPath}
                    onManualOpen={handleManualScript}
                    onBrowseOpen={handlePickScript}
                  />

                  <div>
                    <div className="flex items-center justify-between border-b border-border-subtle px-1 py-2">
                      <span className="text-2xs font-semibold uppercase tracking-[0.16em] text-zinc-500">
                        Detected Scripts
                      </span>
                      {loading ? (
                        <span className="text-2xs text-zinc-500">Scanning current directory...</span>
                      ) : (
                        <span className="text-2xs text-zinc-600">{scripts.length} found</span>
                      )}
                    </div>
                    <div className="max-h-56 overflow-y-auto py-1">
                      {scripts.length === 0 ? (
                        <div className="px-3 py-8 text-center text-xs text-zinc-600">
                          No Python scripts found in the current directory.
                        </div>
                      ) : (
                        scripts.map(script => (
                          <ScriptItem
                            key={script.script_path || script.workspace_path}
                            script={script}
                            onClick={() => void handleSelectScript(script.script_path)}
                          />
                        ))
                      )}
                    </div>
                  </div>
                </>
              ) : (
                <ModeActionPanel
                  launchMode={launchMode}
                  pathValue={manualShellRootPath}
                  pathReady={shellPathReady}
                  onPathChange={setManualShellRootPath}
                  onManualOpen={handleManualShellRoot}
                  onBrowseOpen={handlePickShellRoot}
                />
              )}
            </div>
          )}

          {loading && step !== 0 && (
            <div className="flex items-center justify-center py-12">
              <div className="text-xs text-zinc-500 animate-pulse">Loading...</div>
            </div>
          )}

          {!loading && step === 1 && (
            <div className="space-y-1">
              <div className="mb-3 space-y-1">
                <div className="text-xs font-semibold text-zinc-300">
                  {requiresConfigTemplate ? 'Choose a YAML config' : 'Select a config'}
                  {' '}
                  for <span className="font-mono">{pathName(selectedScript)}</span>
                </div>
                <p className="text-2xs leading-relaxed text-zinc-500">
                  {requiresConfigTemplate
                    ? 'This script needs a YAML config before first launch. Choose one below or enter a path; pyruns will save it as config_default.yaml for later runs.'
                    : 'Choose a YAML file for this launch, or open without one when the script can generate its default config.'}
                </p>
              </div>
              {configs.length === 0 ? (
                <div className="text-center py-8">
                  <p className="text-xs text-zinc-600 mb-3">
                    {requiresConfigTemplate
                      ? 'No YAML configs were found near this script. Enter a config path below.'
                      : 'No config files found'}
                  </p>
                  {!requiresConfigTemplate && (
                    <button
                      onClick={handleSkipConfig}
                      className="text-xs text-accent transition-colors hover:text-accent-hover"
                    >
                      Open without config
                    </button>
                  )}
                </div>
              ) : (
                <>
                  {configs.map(config => (
                    <ConfigItem
                      key={config.path}
                      config={config}
                      onClick={() => handleSelectConfig(config.path)}
                    />
                  ))}
                  {!requiresConfigTemplate && (
                    <button
                      onClick={handleSkipConfig}
                      className="w-full px-3 py-2 text-left text-2xs text-zinc-600 transition-colors hover:text-zinc-400"
                    >
                      Open without config
                    </button>
                  )}
                </>
              )}
              <div className="mt-3 flex items-center gap-2">
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
                  placeholder={requiresConfigTemplate ? 'Path to config.yaml' : 'Optional path to config.yaml'}
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
          )}

          {!loading && step === 2 && (
            <div className="flex flex-col items-center py-8 gap-4">
              <div className="p-2 text-accent">
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

function LaunchChoiceTabs({
  launchMode,
  onChange,
}: {
  launchMode: 'python' | 'shell'
  onChange: (mode: 'python' | 'shell') => void
}) {
  return (
    <div className="grid gap-2 md:grid-cols-2">
      <button
        type="button"
        onClick={() => onChange('python')}
        className={clsx(
          'flex min-h-12 items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-semibold transition-colors',
          launchMode === 'python'
            ? 'bg-accent text-white'
            : 'text-zinc-400 hover:bg-surface-overlay hover:text-zinc-100',
        )}
      >
        <FileSearch className="h-4 w-4" />
        Python
      </button>
      <button
        type="button"
        onClick={() => onChange('shell')}
        className={clsx(
          'flex min-h-12 items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-semibold transition-colors',
          launchMode === 'shell'
            ? 'bg-accent text-white'
            : 'text-zinc-400 hover:bg-surface-overlay hover:text-zinc-100',
        )}
      >
        <FolderPlus className="h-4 w-4" />
        Shell
      </button>
    </div>
  )
}

function ModeActionPanel({
  launchMode,
  pathValue,
  pathReady,
  onPathChange,
  onManualOpen,
  onBrowseOpen,
}: {
  launchMode: 'python' | 'shell'
  pathValue: string
  pathReady: boolean
  onPathChange: (value: string) => void
  onManualOpen: () => void | Promise<void>
  onBrowseOpen: () => void | Promise<void>
}) {
  const isPython = launchMode === 'python'
  const Icon = isPython ? FileSearch : FolderPlus
  const browseLabel = isPython ? 'Browse Script' : 'Browse & Open Folder'
  const manualLabel = isPython ? 'Select Script Path' : 'Open Folder Path'
  const placeholder = isPython ? 'Absolute or relative path to train.py' : 'Path to shell project folder'

  return (
    <div className="space-y-2">
      <button
        type="button"
        onClick={() => void onBrowseOpen()}
        className={clsx(
          'inline-flex min-h-9 w-full items-center justify-center gap-1.5 rounded-md px-3 py-2 text-xs font-medium transition-colors',
          isPython
            ? 'bg-accent text-white hover:bg-accent-hover'
            : 'bg-accent/10 text-accent hover:bg-accent/20',
        )}
      >
        <Icon className="h-3.5 w-3.5" />
        {browseLabel}
      </button>
      <div className="flex items-center gap-2">
        <input
          value={pathValue}
          onChange={event => onPathChange(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter') {
              event.preventDefault()
              void onManualOpen()
            }
          }}
          placeholder={placeholder}
          className="min-w-0 flex-1 rounded-md border border-border-subtle bg-surface-raised px-2.5 py-1.5 text-xs font-mono text-zinc-200 outline-none transition-colors focus:border-border"
        />
        <button
          type="button"
          disabled={!pathReady}
          onClick={() => void onManualOpen()}
          className="rounded-md border border-border-subtle px-3 py-1.5 text-xs font-medium text-zinc-300 transition-colors hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40"
        >
          {manualLabel}
        </button>
      </div>
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
