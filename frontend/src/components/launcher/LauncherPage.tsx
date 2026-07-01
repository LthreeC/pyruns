import { useEffect, useCallback, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  FileCode, ChevronRight, Rocket, FileSearch, FolderPlus,
  CheckCircle2, AlertTriangle, Loader2, History,
} from 'lucide-react'
import clsx from 'clsx'
import { useLauncherStore, useWorkspaceStore } from '@/store'
import { useDebouncedValue } from '@/hooks/useDebouncedValue'
import type { ConfigCandidate, PathValidationResult } from '@/types'
import * as api from '@/api'

function pathName(path: string) {
  return path.split(/[\\/]/).filter(Boolean).pop() || path
}

const LAUNCH_HISTORY_LIMIT = 50
const LAUNCH_HISTORY_STORAGE_KEYS = {
  python: 'pyruns.launcher.history.python',
  shell: 'pyruns.launcher.history.shell',
  yaml: 'pyruns.launcher.history.yaml',
} as const

type LaunchHistoryKind = keyof typeof LAUNCH_HISTORY_STORAGE_KEYS

function readLaunchHistory(kind: LaunchHistoryKind): string[] {
  if (typeof window === 'undefined') {
    return []
  }
  try {
    const raw = window.localStorage.getItem(LAUNCH_HISTORY_STORAGE_KEYS[kind])
    const parsed = raw ? JSON.parse(raw) : []
    if (!Array.isArray(parsed)) {
      return []
    }
    const unique = new Set<string>()
    parsed.forEach(item => {
      const path = typeof item === 'string' ? item.trim() : ''
      if (path) {
        unique.add(path)
      }
    })
    return [...unique].slice(0, LAUNCH_HISTORY_LIMIT)
  } catch {
    return []
  }
}

function writeLaunchHistory(kind: LaunchHistoryKind, path: string): string[] {
  const normalized = path.trim()
  if (!normalized) {
    return readLaunchHistory(kind)
  }
  const next = [
    normalized,
    ...readLaunchHistory(kind).filter(item => item !== normalized),
  ].slice(0, LAUNCH_HISTORY_LIMIT)
  if (typeof window !== 'undefined') {
    window.localStorage.setItem(LAUNCH_HISTORY_STORAGE_KEYS[kind], JSON.stringify(next))
  }
  return next
}

type PathValidationState = {
  status: 'idle' | 'checking' | 'valid' | 'invalid'
  message: string
  normalizedPath: string
}

const emptyValidation: PathValidationState = {
  status: 'idle',
  message: '',
  normalizedPath: '',
}

function validationFromResult(result: PathValidationResult): PathValidationState {
  return {
    status: result.ok ? 'valid' : 'invalid',
    message: result.message,
    normalizedPath: result.normalized_path,
  }
}

export default function LauncherPage({ onClose }: { onClose: () => void }) {
  const backdropPointerStartedRef = useRef(false)
  const {
    configs, selectedScript, requiresConfigTemplate, configSource, step, loading,
    selectScript, selectConfig, reset: resetLauncher,
  } = useLauncherStore()
  const workspace = useWorkspaceStore(state => state.workspace)
  const setWorkspace = useWorkspaceStore(state => state.setWorkspace)
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()
  const [manualScriptPath, setManualScriptPath] = useState('')
  const [manualConfigPath, setManualConfigPath] = useState('')
  const [manualShellRootPath, setManualShellRootPath] = useState('')
  const [launchMode, setLaunchMode] = useState<'python' | 'shell'>('python')
  const [error, setError] = useState('')
  const [scriptValidation, setScriptValidation] = useState<PathValidationState>(emptyValidation)
  const [configValidation, setConfigValidation] = useState<PathValidationState>(emptyValidation)
  const [shellValidation, setShellValidation] = useState<PathValidationState>(emptyValidation)
  const [launchHistory, setLaunchHistory] = useState<Record<LaunchHistoryKind, string[]>>(() => ({
    python: readLaunchHistory('python'),
    shell: readLaunchHistory('shell'),
    yaml: readLaunchHistory('yaml'),
  }))
  const debouncedScriptPath = useDebouncedValue(manualScriptPath.trim(), 300)
  const debouncedConfigPath = useDebouncedValue(manualConfigPath.trim(), 300)
  const debouncedShellRootPath = useDebouncedValue(manualShellRootPath.trim(), 300)
  const scriptPathReady = manualScriptPath.trim().length > 0 && scriptValidation.status === 'valid'
  const configPathReady = manualConfigPath.trim().length > 0 && configValidation.status === 'valid'
  const shellPathReady = manualShellRootPath.trim().length > 0 && shellValidation.status === 'valid'
  const nativePickerAvailable = workspace?.native_file_picker === true
  const mustChooseConfig = requiresConfigTemplate || configSource === 'pyruns_load'

  const rememberLaunchPath = useCallback((kind: LaunchHistoryKind, path: string) => {
    const nextHistory = writeLaunchHistory(kind, path)
    setLaunchHistory(current => ({
      ...current,
      [kind]: nextHistory,
    }))
  }, [])

  const openSelectedWorkspace = useCallback(async (historyPath = '', yamlHistoryPath = '') => {
    setError('')
    try {
      await useLauncherStore.getState().openWorkspace()
      const openedWorkspace = useWorkspaceStore.getState().workspace
      rememberLaunchPath('python', openedWorkspace?.script_path || historyPath)
      rememberLaunchPath('yaml', yamlHistoryPath)
      onClose()
      navigate('/')
      return true
    } catch (err: any) {
      if (useLauncherStore.getState().step === 2) {
        useLauncherStore.setState({ step: 1 })
      }
      setError(err.message)
      return false
    }
  }, [navigate, onClose, rememberLaunchPath])

  const openPythonPath = useCallback(async (path: string) => {
    const scriptPath = path.trim()
    if (!scriptPath) {
      setError('Enter a Python script path.')
      return
    }

    setManualScriptPath(scriptPath)
    setError('')
    try {
      await selectScript(scriptPath)
      if (useLauncherStore.getState().step === 2) {
        await openSelectedWorkspace(scriptPath)
      }
    } catch (err: any) {
      setError(err.message)
    }
  }, [openSelectedWorkspace, selectScript])

  const openShellPath = useCallback(async (path: string) => {
    const shellPath = path.trim()
    if (!shellPath) {
      setError('Enter a folder path.')
      return
    }

    setManualShellRootPath(shellPath)
    setError('')
    try {
      const workspace = await api.openLauncherShellRoot(shellPath)
      setWorkspace(workspace)
      rememberLaunchPath('shell', workspace.working_root || shellPath)
      onClose()
      navigate('/generator')
    } catch (err: any) {
      setError(err.message)
    }
  }, [navigate, onClose, rememberLaunchPath, setWorkspace])

  useEffect(() => {
    const modeParam = searchParams.get('mode')
    const scriptParam = searchParams.get('script')
    const configParam = searchParams.get('config')
    const initialLaunchMode = scriptParam ? 'python' : modeParam === 'shell' ? 'shell' : 'python'

    setLaunchMode(initialLaunchMode)
    if (modeParam && !scriptParam) {
      resetLauncher()
      setManualScriptPath('')
      setManualConfigPath('')
      setManualShellRootPath('')
      setError('')
    }
    if (scriptParam) {
      setManualScriptPath(scriptParam)
      if (configParam) {
        setManualConfigPath(configParam)
      }
      void selectScript(scriptParam).then(() => {
        if (configParam) {
          selectConfig(configParam)
          void openSelectedWorkspace(scriptParam, configParam)
          return
        }
        if (useLauncherStore.getState().step === 2) {
          void openSelectedWorkspace(scriptParam)
        }
      })
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    if (!debouncedScriptPath) {
      setScriptValidation(emptyValidation)
      return () => { cancelled = true }
    }

    setScriptValidation({ status: 'checking', message: 'Checking path...', normalizedPath: '' })
    api.validateLauncherPath('python', debouncedScriptPath)
      .then(result => {
        if (!cancelled) {
          setScriptValidation(validationFromResult(result))
        }
      })
      .catch((err: any) => {
        if (!cancelled) {
          setScriptValidation({ status: 'invalid', message: err.message, normalizedPath: '' })
        }
      })
    return () => { cancelled = true }
  }, [debouncedScriptPath])

  useEffect(() => {
    let cancelled = false
    if (!debouncedConfigPath) {
      setConfigValidation(emptyValidation)
      return () => { cancelled = true }
    }

    setConfigValidation({ status: 'checking', message: 'Checking path...', normalizedPath: '' })
    api.validateLauncherPath('config', debouncedConfigPath, selectedScript)
      .then(result => {
        if (!cancelled) {
          setConfigValidation(validationFromResult(result))
        }
      })
      .catch((err: any) => {
        if (!cancelled) {
          setConfigValidation({ status: 'invalid', message: err.message, normalizedPath: '' })
        }
      })
    return () => { cancelled = true }
  }, [debouncedConfigPath, selectedScript])

  useEffect(() => {
    let cancelled = false
    if (!debouncedShellRootPath) {
      setShellValidation(emptyValidation)
      return () => { cancelled = true }
    }

    setShellValidation({ status: 'checking', message: 'Checking path...', normalizedPath: '' })
    api.validateLauncherPath('shell', debouncedShellRootPath)
      .then(result => {
        if (!cancelled) {
          setShellValidation(validationFromResult(result))
        }
      })
      .catch((err: any) => {
        if (!cancelled) {
          setShellValidation({ status: 'invalid', message: err.message, normalizedPath: '' })
        }
      })
    return () => { cancelled = true }
  }, [debouncedShellRootPath])

  const handleLaunchModeChange = useCallback((mode: 'python' | 'shell') => {
    setLaunchMode(mode)
  }, [])

  const handleSkipConfig = useCallback(async () => {
    setError('')
    if (mustChooseConfig) {
      setError('Choose or enter a YAML config path first.')
      return
    }
    selectConfig('')
    await openSelectedWorkspace(selectedScript)
  }, [mustChooseConfig, openSelectedWorkspace, selectConfig, selectedScript])

  const handleManualScript = useCallback(async () => {
    const scriptPath = manualScriptPath.trim()
    if (!scriptPath) {
      setError('Enter a Python script path.')
      return
    }

    setError('')
    await openPythonPath(scriptPath)
  }, [manualScriptPath, openPythonPath])

  const openSelectedConfig = useCallback(async (configPath: string) => {
    setError('')
    selectConfig(configPath)
    await openSelectedWorkspace(selectedScript, configPath)
  }, [openSelectedWorkspace, selectConfig, selectedScript])

  const handleSelectConfig = useCallback(async (configPath: string) => {
    await openSelectedConfig(configPath)
  }, [openSelectedConfig])

  const handleManualConfig = useCallback(async () => {
    const configPath = manualConfigPath.trim()
    if (!configPath) {
      if (mustChooseConfig) {
        setError('Choose or enter a YAML config path first.')
        return
      }
      void handleSkipConfig()
      return
    }
    setError('')
    await openSelectedConfig(configPath)
  }, [handleSkipConfig, manualConfigPath, mustChooseConfig, openSelectedConfig])

  const handlePickScript = useCallback(async () => {
    setError('')
    try {
      const selection = await api.pickLauncherScriptPath()
      setManualScriptPath(selection.script_path)
      await openPythonPath(selection.script_path)
    } catch (err: any) {
      setError(err.message)
    }
  }, [openPythonPath])

  const handlePickConfig = useCallback(async () => {
    if (!selectedScript) {
      setError('Choose a Python script first.')
      return
    }

    setError('')
    try {
      const selection = await api.pickLauncherConfigPath(selectedScript)
      setManualConfigPath(selection.path)
      await openSelectedConfig(selection.path)
    } catch (err: any) {
      setError(err.message)
    }
  }, [openSelectedConfig, selectedScript])

  const handlePickShellRoot = useCallback(async () => {
    setError('')
    try {
      const workspace = await api.pickLauncherShellRoot()
      setWorkspace(workspace)
      rememberLaunchPath('shell', workspace.working_root || workspace.project_root || '')
      onClose()
      navigate('/generator')
    } catch (err: any) {
      setError(err.message)
    }
  }, [navigate, onClose, rememberLaunchPath, setWorkspace])

  const handleManualShellRoot = useCallback(async () => {
    await openShellPath(manualShellRootPath)
  }, [manualShellRootPath, openShellPath])

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-3 sm:p-4"
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
        role="dialog"
        aria-modal="true"
        className="flex max-h-[80vh] w-full max-w-[calc(100vw-1.5rem)] sm:max-w-2xl flex-col overflow-hidden rounded-md border border-border bg-surface-raised shadow-md"
        onPointerDown={() => {
          backdropPointerStartedRef.current = false
        }}
        onClick={event => event.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center gap-3 border-b border-border-subtle px-4 py-3 sm:px-6 sm:py-4">
          <Rocket className="w-5 h-5 text-accent" />
          <div>
            <h2 className="text-sm font-semibold text-zinc-100">Launch Workspace</h2>
            <p className="text-2xs text-zinc-500 mt-0.5">Choose a workspace type</p>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-3 sm:p-4">
          {error && (
            <div className="mb-3 rounded-md border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
              {error}
            </div>
          )}

          {step === 0 && (
            <div className="space-y-3">
              <LaunchChoiceTabs launchMode={launchMode} onChange={handleLaunchModeChange} />

              {launchMode === 'python' ? (
                <>
                  <ModeActionPanel
                    launchMode={launchMode}
                    pathValue={manualScriptPath}
                    pathReady={scriptPathReady}
                    validation={scriptValidation}
                    pickerAvailable={nativePickerAvailable}
                    onPathChange={setManualScriptPath}
                    onManualOpen={handleManualScript}
                    onBrowseOpen={handlePickScript}
                    recentPaths={launchHistory.python}
                    onRecentPathOpen={openPythonPath}
                  />
                </>
              ) : (
                <ModeActionPanel
                  launchMode={launchMode}
                  pathValue={manualShellRootPath}
                  pathReady={shellPathReady}
                  validation={shellValidation}
                  pickerAvailable={nativePickerAvailable}
                  onPathChange={setManualShellRootPath}
                  onManualOpen={handleManualShellRoot}
                  onBrowseOpen={handlePickShellRoot}
                  recentPaths={launchHistory.shell}
                  onRecentPathOpen={openShellPath}
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
                  {mustChooseConfig ? 'Choose a YAML config' : 'Select a config'}
                  {' '}
                  for <span className="font-mono">{pathName(selectedScript)}</span>
                </div>
                <p className="text-2xs leading-relaxed text-zinc-500">
                  {mustChooseConfig
                    ? configSource === 'pyruns_load'
                      ? 'pyruns.load() reads the selected YAML for this workspace. Choose one below or enter a path; pyruns will save it as config_default.yaml for later runs.'
                      : 'This script needs a YAML config before first launch. Choose one below or enter a path; pyruns will save it as config_default.yaml for later runs.'
                    : 'Choose a YAML file for this launch, or open without one when the script can generate its default config.'}
                </p>
              </div>
              <ConfigActionPanel
                pathValue={manualConfigPath}
                pathReady={configPathReady}
                validation={configValidation}
                pickerAvailable={nativePickerAvailable}
                mustChooseConfig={mustChooseConfig}
                onPathChange={setManualConfigPath}
                onManualOpen={handleManualConfig}
                onBrowseOpen={handlePickConfig}
                recentPaths={launchHistory.yaml}
                onRecentPathOpen={handleSelectConfig}
              />
              {configs.length === 0 ? (
                <div className="text-center py-6">
                  <p className="text-xs text-zinc-600 mb-3">
                    {mustChooseConfig
                      ? 'No YAML configs were found near this script. Enter a config path below.'
                      : 'No config files found'}
                  </p>
                  {!mustChooseConfig && (
                    <button
                      onClick={handleSkipConfig}
                      className="text-xs text-accent transition-colors hover:text-accent-hover"
                    >
                      Open without config
                    </button>
                  )}
                </div>
              ) : (
                <div className="pt-2">
                  <div className="flex items-center justify-between border-b border-border-subtle px-1 py-2">
                    <span className="text-2xs font-semibold uppercase tracking-[0.16em] text-zinc-500">
                      Nearby YAML
                    </span>
                    <span className="text-2xs text-zinc-600">{configs.length} found</span>
                  </div>
                  {configs.map(config => (
                    <ConfigItem
                      key={config.path}
                      config={config}
                      onClick={() => void handleSelectConfig(config.path)}
                    />
                  ))}
                  {!mustChooseConfig && (
                    <button
                      onClick={handleSkipConfig}
                      className="w-full px-3 py-2 text-left text-2xs text-zinc-600 transition-colors hover:text-zinc-400"
                    >
                      Open without config
                    </button>
                  )}
                </div>
              )}
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
  validation,
  pickerAvailable,
  onPathChange,
  onManualOpen,
  onBrowseOpen,
  recentPaths = [],
  onRecentPathOpen,
}: {
  launchMode: 'python' | 'shell'
  pathValue: string
  pathReady: boolean
  validation: PathValidationState
  pickerAvailable: boolean
  onPathChange: (value: string) => void
  onManualOpen: () => void | Promise<void>
  onBrowseOpen: () => void | Promise<void>
  recentPaths?: string[]
  onRecentPathOpen?: (path: string) => void | Promise<void>
}) {
  const isPython = launchMode === 'python'
  const Icon = isPython ? FileSearch : FolderPlus
  const browseLabel = pickerAvailable ? (isPython ? 'Browse Script' : 'Browse & Open Folder') : 'Browse Unavailable'
  const manualLabel = isPython ? 'Select Script Path' : 'Open Folder Path'
  const placeholder = isPython ? 'Absolute or relative path to train.py' : 'Path to shell project folder'

  return (
    <div className="space-y-2">
      <button
        type="button"
        disabled={!pickerAvailable}
        onClick={() => void onBrowseOpen()}
        className={clsx(
          'inline-flex min-h-9 w-full items-center justify-center gap-1.5 rounded-md px-3 py-2 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50',
          isPython
            ? 'bg-accent text-white hover:bg-accent-hover'
            : 'bg-accent/10 text-accent hover:bg-accent/20',
        )}
      >
        <Icon className="h-3.5 w-3.5" />
        {browseLabel}
      </button>
      {!pickerAvailable && (
        <div className="px-1 text-2xs text-zinc-500">
          Native picker unavailable on this server; enter the path manually.
        </div>
      )}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <input
          value={pathValue}
          onChange={event => onPathChange(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter') {
              event.preventDefault()
              if (pathReady) {
                void onManualOpen()
              }
            }
          }}
          placeholder={placeholder}
          className="w-full min-w-0 flex-1 rounded-md border border-border-subtle bg-surface-raised px-2.5 py-1.5 text-xs font-mono text-zinc-200 outline-none transition-colors focus:border-border"
        />
        <button
          type="button"
          disabled={!pathReady}
          onClick={() => void onManualOpen()}
          className="w-full rounded-md border border-border-subtle px-3 py-1.5 text-xs font-medium text-zinc-300 transition-colors hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40 sm:w-auto sm:flex-none"
        >
          {manualLabel}
        </button>
      </div>
      <PathValidationHint validation={validation} />
      <RecentPathList
        kind={launchMode}
        paths={recentPaths}
        onOpen={onRecentPathOpen}
      />
    </div>
  )
}

function RecentPathList({
  kind,
  paths,
  onOpen,
}: {
  kind: LaunchHistoryKind
  paths: string[]
  onOpen?: (path: string) => void | Promise<void>
}) {
  if (!paths.length || !onOpen) {
    return null
  }

  const Icon = kind === 'python' ? FileSearch : kind === 'shell' ? FolderPlus : FileCode
  const label = kind === 'yaml' ? 'Recent YAML' : 'Recent Paths'

  return (
    <div className="space-y-1 pt-1">
      <div className="flex items-center justify-between px-1">
        <span className="inline-flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-[0.14em] text-zinc-500">
          <History className="h-3 w-3" />
          {label}
        </span>
        <span className="text-2xs text-zinc-600">{paths.length}</span>
      </div>
      <div className="max-h-60 space-y-1 overflow-y-auto pr-1">
        {paths.map(path => (
          <button
            key={path}
            type="button"
            onClick={() => void onOpen(path)}
            className="group flex min-h-10 w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-surface-overlay focus:outline-none focus:ring-2 focus:ring-accent/25"
          >
            <Icon className="h-3.5 w-3.5 flex-none text-zinc-500 transition-colors group-hover:text-accent" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-xs text-zinc-300">{pathName(path)}</span>
              <span className="block truncate font-mono text-2xs text-zinc-600" title={path}>
                {path}
              </span>
            </span>
            <ChevronRight className="h-3.5 w-3.5 flex-none text-zinc-600 transition-colors group-hover:text-zinc-400" />
          </button>
        ))}
      </div>
    </div>
  )
}

function ConfigActionPanel({
  pathValue,
  pathReady,
  validation,
  pickerAvailable,
  mustChooseConfig,
  onPathChange,
  onManualOpen,
  onBrowseOpen,
  recentPaths = [],
  onRecentPathOpen,
}: {
  pathValue: string
  pathReady: boolean
  validation: PathValidationState
  pickerAvailable: boolean
  mustChooseConfig: boolean
  onPathChange: (value: string) => void
  onManualOpen: () => void | Promise<void>
  onBrowseOpen: () => void | Promise<void>
  recentPaths?: string[]
  onRecentPathOpen?: (path: string) => void | Promise<void>
}) {
  return (
    <div className="space-y-2">
      <button
        type="button"
        disabled={!pickerAvailable}
        onClick={() => void onBrowseOpen()}
        className="inline-flex min-h-9 w-full items-center justify-center gap-1.5 rounded-md bg-accent text-white px-3 py-2 text-xs font-medium transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
      >
        <FileCode className="h-3.5 w-3.5" />
        Browse Config
      </button>
      {!pickerAvailable && (
        <div className="px-1 text-2xs text-zinc-500">
          Native picker unavailable on this server; enter the YAML path manually.
        </div>
      )}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <input
          value={pathValue}
          onChange={event => onPathChange(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter') {
              event.preventDefault()
              if (pathReady) {
                void onManualOpen()
              }
            }
          }}
          placeholder={mustChooseConfig ? 'Path to YAML config' : 'Optional path to YAML config'}
          className="w-full min-w-0 flex-1 rounded-md border border-border-subtle bg-surface-raised px-2.5 py-1.5 text-xs font-mono text-zinc-200 outline-none transition-colors focus:border-border"
        />
        <button
          type="button"
          disabled={!pathReady}
          onClick={() => void onManualOpen()}
          className="w-full rounded-md border border-border-subtle px-3 py-1.5 text-xs font-medium text-zinc-300 transition-colors hover:text-zinc-100 disabled:cursor-not-allowed disabled:opacity-40 sm:w-auto sm:flex-none"
        >
          Open Config Path
        </button>
      </div>
      <PathValidationHint validation={validation} />
      <RecentPathList
        kind="yaml"
        paths={recentPaths}
        onOpen={onRecentPathOpen}
      />
    </div>
  )
}

function PathValidationHint({ validation }: { validation: PathValidationState }) {
  if (validation.status === 'idle') {
    return null
  }

  const valid = validation.status === 'valid'
  const checking = validation.status === 'checking'
  const Icon = valid ? CheckCircle2 : checking ? Loader2 : AlertTriangle
  const text = valid && validation.normalizedPath ? validation.normalizedPath : validation.message

  return (
    <div
      className={clsx(
        'flex items-center gap-1.5 truncate px-1 text-2xs font-mono',
        valid ? 'text-emerald-300' : checking ? 'text-zinc-500' : 'text-amber-300',
      )}
      title={text}
    >
      <Icon className={clsx('h-3 w-3 flex-none', checking && 'animate-spin')} />
      <span className="truncate">{text}</span>
    </div>
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
