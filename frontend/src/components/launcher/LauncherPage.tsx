import { useEffect, useCallback, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  FileCode, ChevronRight, Rocket, FileSearch, FolderPlus,
  CheckCircle2, AlertTriangle, Loader2, History,
} from 'lucide-react'
import clsx from 'clsx'
import {
  confirmDiscardWorkspaceChanges,
  useLauncherStore,
  useWorkspaceStore,
} from '@/store'
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
    try {
      window.localStorage.setItem(LAUNCH_HISTORY_STORAGE_KEYS[kind], JSON.stringify(next))
    } catch {
      // Opening a workspace must not fail just because recent-path storage is unavailable.
    }
  }
  return next
}

type PathValidationState = {
  status: 'idle' | 'checking' | 'valid' | 'invalid'
  message: string
  normalizedPath: string
  validatedPath: string
}

const emptyValidation: PathValidationState = {
  status: 'idle',
  message: '',
  normalizedPath: '',
  validatedPath: '',
}

function validationFromResult(result: PathValidationResult, validatedPath: string): PathValidationState {
  return {
    status: result.ok ? 'valid' : 'invalid',
    message: result.message,
    normalizedPath: result.normalized_path,
    validatedPath,
  }
}

export default function LauncherPage({ onClose }: { onClose: () => void }) {
  const backdropPointerStartedRef = useRef(false)
  const launcherActionInFlightRef = useRef(false)
  const launcherMountedRef = useRef(true)
  const modalRef = useRef<HTMLDivElement>(null)
  const previousFocusRef = useRef<HTMLElement | null>(null)
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
  const scriptPathReady = manualScriptPath.trim().length > 0
    && scriptValidation.status === 'valid'
    && scriptValidation.validatedPath === manualScriptPath.trim()
  const configPathReady = manualConfigPath.trim().length > 0
    && configValidation.status === 'valid'
    && configValidation.validatedPath === manualConfigPath.trim()
  const shellPathReady = manualShellRootPath.trim().length > 0
    && shellValidation.status === 'valid'
    && shellValidation.validatedPath === manualShellRootPath.trim()
  const nativePickerAvailable = workspace?.native_file_picker === true
  const mustChooseConfig = requiresConfigTemplate || configSource === 'pyruns_load'

  const requestClose = useCallback(() => {
    if (launcherActionInFlightRef.current || useLauncherStore.getState().loading) {
      return
    }
    onClose()
  }, [onClose])

  useEffect(() => {
    launcherMountedRef.current = true
    return () => {
      launcherMountedRef.current = false
      useLauncherStore.getState().reset()
    }
  }, [])

  const rememberLaunchPath = useCallback((kind: LaunchHistoryKind, path: string) => {
    const nextHistory = writeLaunchHistory(kind, path)
    setLaunchHistory(current => ({
      ...current,
      [kind]: nextHistory,
    }))
  }, [])

  const openSelectedWorkspace = useCallback(async (historyPath = '', yamlHistoryPath = '') => {
    if (!(await confirmDiscardWorkspaceChanges())) {
      return false
    }
    setError('')
    try {
      const opened = await useLauncherStore.getState().openWorkspace()
      if (!opened || !launcherMountedRef.current) {
        return false
      }
      const openedWorkspace = useWorkspaceStore.getState().workspace
      rememberLaunchPath('python', openedWorkspace?.script_path || historyPath)
      rememberLaunchPath('yaml', yamlHistoryPath)
      onClose()
      navigate('/')
      return true
    } catch (err: any) {
      if (!launcherMountedRef.current) {
        return false
      }
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
      if (!launcherMountedRef.current) {
        return
      }
      if (useLauncherStore.getState().step === 2) {
        await openSelectedWorkspace(scriptPath)
      }
    } catch (err: any) {
      if (launcherMountedRef.current) {
        setError(err.message)
      }
    }
  }, [openSelectedWorkspace, selectScript])

  const openShellPath = useCallback(async (path: string) => {
    const shellPath = path.trim()
    if (!shellPath) {
      setError('Enter a folder path.')
      return
    }
    if (!(await confirmDiscardWorkspaceChanges())) {
      return
    }
    if (launcherActionInFlightRef.current) {
      return
    }

    launcherActionInFlightRef.current = true
    useLauncherStore.setState({ loading: true })
    setManualShellRootPath(shellPath)
    setError('')
    try {
      const workspace = await api.openLauncherShellRoot(shellPath)
      if (!launcherMountedRef.current) {
        return
      }
      launcherActionInFlightRef.current = false
      useLauncherStore.setState({ loading: false })
      setWorkspace(workspace)
      rememberLaunchPath('shell', workspace.working_root || shellPath)
      onClose()
      navigate('/generator')
    } catch (err: any) {
      if (launcherMountedRef.current) {
        setError(err.message)
      }
    } finally {
      launcherActionInFlightRef.current = false
      if (launcherMountedRef.current) {
        useLauncherStore.setState({ loading: false })
      }
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
      resetLauncher()
      setManualScriptPath(scriptParam)
      setManualConfigPath(configParam || '')
      setError('Review the prefilled path, then choose Open to switch workspaces.')
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    if (!debouncedScriptPath) {
      setScriptValidation(emptyValidation)
      return () => { cancelled = true }
    }

    setScriptValidation({ status: 'checking', message: 'Checking path...', normalizedPath: '', validatedPath: debouncedScriptPath })
    api.validateLauncherPath('python', debouncedScriptPath)
      .then(result => {
        if (!cancelled) {
          setScriptValidation(validationFromResult(result, debouncedScriptPath))
        }
      })
      .catch((err: any) => {
        if (!cancelled) {
          setScriptValidation({ status: 'invalid', message: err.message, normalizedPath: '', validatedPath: debouncedScriptPath })
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

    setConfigValidation({ status: 'checking', message: 'Checking path...', normalizedPath: '', validatedPath: debouncedConfigPath })
    api.validateLauncherPath('config', debouncedConfigPath, selectedScript)
      .then(result => {
        if (!cancelled) {
          setConfigValidation(validationFromResult(result, debouncedConfigPath))
        }
      })
      .catch((err: any) => {
        if (!cancelled) {
          setConfigValidation({ status: 'invalid', message: err.message, normalizedPath: '', validatedPath: debouncedConfigPath })
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

    setShellValidation({ status: 'checking', message: 'Checking path...', normalizedPath: '', validatedPath: debouncedShellRootPath })
    api.validateLauncherPath('shell', debouncedShellRootPath)
      .then(result => {
        if (!cancelled) {
          setShellValidation(validationFromResult(result, debouncedShellRootPath))
        }
      })
      .catch((err: any) => {
        if (!cancelled) {
          setShellValidation({ status: 'invalid', message: err.message, normalizedPath: '', validatedPath: debouncedShellRootPath })
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
    if (launcherActionInFlightRef.current) {
      return
    }
    launcherActionInFlightRef.current = true
    useLauncherStore.setState({ loading: true })
    setError('')
    try {
      const selection = await api.pickLauncherScriptPath()
      if (!launcherMountedRef.current) {
        return
      }
      setManualScriptPath(selection.script_path)
      await openPythonPath(selection.script_path)
    } catch (err: any) {
      if (launcherMountedRef.current) {
        setError(err.message)
      }
    } finally {
      launcherActionInFlightRef.current = false
      if (launcherMountedRef.current) {
        useLauncherStore.setState({ loading: false })
      }
    }
  }, [openPythonPath])

  const handlePickConfig = useCallback(async () => {
    if (!selectedScript) {
      setError('Choose a Python script first.')
      return
    }
    if (launcherActionInFlightRef.current) {
      return
    }

    launcherActionInFlightRef.current = true
    useLauncherStore.setState({ loading: true })
    setError('')
    try {
      const selection = await api.pickLauncherConfigPath(selectedScript)
      if (!launcherMountedRef.current) {
        return
      }
      setManualConfigPath(selection.path)
      await openSelectedConfig(selection.path)
    } catch (err: any) {
      if (launcherMountedRef.current) {
        setError(err.message)
      }
    } finally {
      launcherActionInFlightRef.current = false
      if (launcherMountedRef.current) {
        useLauncherStore.setState({ loading: false })
      }
    }
  }, [openSelectedConfig, selectedScript])

  const handlePickShellRoot = useCallback(async () => {
    if (!(await confirmDiscardWorkspaceChanges())) {
      return
    }
    if (launcherActionInFlightRef.current) {
      return
    }
    launcherActionInFlightRef.current = true
    useLauncherStore.setState({ loading: true })
    setError('')
    try {
      const workspace = await api.pickLauncherShellRoot()
      if (!launcherMountedRef.current) {
        return
      }
      launcherActionInFlightRef.current = false
      useLauncherStore.setState({ loading: false })
      setWorkspace(workspace)
      rememberLaunchPath('shell', workspace.working_root || workspace.project_root || '')
      onClose()
      navigate('/generator')
    } catch (err: any) {
      if (launcherMountedRef.current) {
        setError(err.message)
      }
    } finally {
      launcherActionInFlightRef.current = false
      if (launcherMountedRef.current) {
        useLauncherStore.setState({ loading: false })
      }
    }
  }, [navigate, onClose, rememberLaunchPath, setWorkspace])

  const handleManualShellRoot = useCallback(async () => {
    await openShellPath(manualShellRootPath)
  }, [manualShellRootPath, openShellPath])

  useEffect(() => {
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null
    const focusFrame = window.requestAnimationFrame(() => {
      const firstControl = modalRef.current?.querySelector<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )
      ;(firstControl || modalRef.current)?.focus()
    })

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        requestClose()
        return
      }
      if (event.key !== 'Tab') {
        return
      }

      const focusable = Array.from(modalRef.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      ) || []).filter(element => element.offsetParent !== null)
      if (focusable.length === 0) {
        event.preventDefault()
        modalRef.current?.focus()
        return
      }

      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement
      if (event.shiftKey && (active === first || !modalRef.current?.contains(active))) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && (active === last || !modalRef.current?.contains(active))) {
        event.preventDefault()
        first.focus()
      }
    }

    window.addEventListener('keydown', handleKeyDown)
    return () => {
      window.cancelAnimationFrame(focusFrame)
      window.removeEventListener('keydown', handleKeyDown)
      const previousFocus = previousFocusRef.current
      if (previousFocus?.isConnected && previousFocus !== document.body) {
        previousFocus.focus()
      } else {
        document.querySelector<HTMLElement>('[data-launcher-trigger="true"]')?.focus()
      }
    }
  }, [requestClose])

  return (
    <div
      className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60 p-3 sm:p-4"
      onPointerDown={event => {
        backdropPointerStartedRef.current = event.target === event.currentTarget
      }}
      onClick={event => {
        if (backdropPointerStartedRef.current && event.target === event.currentTarget) {
          requestClose()
        }
        backdropPointerStartedRef.current = false
      }}
    >
      <div
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-busy={loading || undefined}
        aria-labelledby="launcher-dialog-title"
        aria-describedby="launcher-dialog-description"
        tabIndex={-1}
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
            <h2 id="launcher-dialog-title" className="text-sm font-semibold text-txt-primary">Launch Workspace</h2>
            <p id="launcher-dialog-description" className="mt-0.5 text-2xs text-txt-tertiary">Choose a workspace type</p>
          </div>
        </div>

        {/* Content */}
        <div className="flex-1 overflow-y-auto p-3 sm:p-4">
          {error && (
            <div role="alert" className="mb-3 rounded-md border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-xs text-rose-700 dark:text-rose-300">
              {error}
            </div>
          )}

          {step === 0 && (
            <div className="space-y-3">
              <LaunchChoiceTabs launchMode={launchMode} busy={loading} onChange={handleLaunchModeChange} />

              {launchMode === 'python' ? (
                <>
                  <ModeActionPanel
                    launchMode={launchMode}
                    pathValue={manualScriptPath}
                    pathReady={scriptPathReady}
                    busy={loading}
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
                  busy={loading}
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
              <div role="status" className="animate-pulse text-xs text-txt-tertiary">Loading...</div>
            </div>
          )}

          {!loading && step === 1 && (
            <div className="space-y-1">
              <div className="mb-3 space-y-1">
                <div className="text-xs font-semibold text-txt-secondary">
                  {mustChooseConfig ? 'Choose a YAML config' : 'Select a config'}
                  {' '}
                  for <span className="font-mono">{pathName(selectedScript)}</span>
                </div>
                <p className="text-2xs leading-relaxed text-txt-tertiary">
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
                  <p className="mb-3 text-xs text-txt-tertiary">
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
                    <span className="text-2xs font-semibold uppercase tracking-[0.16em] text-txt-tertiary">
                      Nearby YAML
                    </span>
                    <span className="text-2xs text-txt-tertiary">{configs.length} found</span>
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
                      className="w-full px-3 py-2 text-left text-2xs text-txt-tertiary transition-colors hover:text-txt-secondary"
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
              type="button"
              disabled={loading}
              onClick={() => useLauncherStore.setState({ step: step - 1 })}
              className="text-xs text-txt-tertiary transition-colors hover:text-txt-secondary disabled:cursor-wait disabled:opacity-50"
            >
              ← Back
            </button>
          ) : <div />}
          <button
            type="button"
            disabled={loading}
            onClick={requestClose}
            className="text-xs text-txt-tertiary transition-colors hover:text-txt-secondary disabled:cursor-wait disabled:opacity-50"
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
  busy,
  onChange,
}: {
  launchMode: 'python' | 'shell'
  busy: boolean
  onChange: (mode: 'python' | 'shell') => void
}) {
  return (
    <div className="grid gap-2 md:grid-cols-2" role="group" aria-label="Workspace type">
      <button
        type="button"
        aria-pressed={launchMode === 'python'}
        disabled={busy}
        onClick={() => onChange('python')}
        className={clsx(
          'flex min-h-12 items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-semibold transition-colors disabled:cursor-wait disabled:opacity-70',
          launchMode === 'python'
            ? 'bg-accent text-white'
            : 'text-txt-secondary hover:bg-surface-overlay hover:text-txt-primary',
        )}
      >
        <FileSearch className="h-4 w-4" />
        Python
      </button>
      <button
        type="button"
        aria-pressed={launchMode === 'shell'}
        disabled={busy}
        onClick={() => onChange('shell')}
        className={clsx(
          'flex min-h-12 items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-semibold transition-colors disabled:cursor-wait disabled:opacity-70',
          launchMode === 'shell'
            ? 'bg-accent text-white'
            : 'text-txt-secondary hover:bg-surface-overlay hover:text-txt-primary',
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
  busy,
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
  busy: boolean
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
  const inputId = isPython ? 'launcher-python-path' : 'launcher-shell-path'
  const validationId = `${inputId}-validation`

  return (
    <div className="space-y-2">
      <button
        type="button"
        disabled={!pickerAvailable || busy}
        onClick={() => void onBrowseOpen()}
        className={clsx(
          'inline-flex min-h-9 w-full items-center justify-center gap-1.5 rounded-md px-3 py-2 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50',
          isPython
            ? 'bg-accent text-white hover:bg-accent-hover'
            : 'bg-accent/10 text-accent hover:bg-accent/20',
        )}
      >
        {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Icon className="h-3.5 w-3.5" />}
        {busy ? 'Preparing…' : browseLabel}
      </button>
      {!pickerAvailable && (
        <div className="px-1 text-2xs text-txt-tertiary">
          Native picker unavailable on this server; enter the path manually.
        </div>
      )}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <label htmlFor={inputId} className="sr-only">{isPython ? 'Python script path' : 'Shell workspace folder path'}</label>
        <input
          id={inputId}
          value={pathValue}
          disabled={busy}
          aria-describedby={validationId}
          aria-invalid={validation.validatedPath === pathValue.trim() && validation.status === 'invalid' ? true : undefined}
          onChange={event => onPathChange(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter') {
              event.preventDefault()
              if (pathReady && !busy) {
                void onManualOpen()
              }
            }
          }}
          placeholder={placeholder}
          className="w-full min-w-0 flex-1 rounded-md border border-border-subtle bg-surface-raised px-2.5 py-1.5 font-mono text-xs text-txt-primary outline-none transition-colors focus:border-border"
        />
        <button
          type="button"
          disabled={!pathReady || busy}
          onClick={() => void onManualOpen()}
          className="w-full rounded-md border border-border-subtle px-3 py-1.5 text-xs font-medium text-txt-secondary transition-colors hover:text-txt-primary disabled:cursor-not-allowed disabled:opacity-40 sm:w-auto sm:flex-none"
        >
          {busy ? 'Preparing...' : manualLabel}
        </button>
      </div>
      <PathValidationHint id={validationId} validation={validation} pathValue={pathValue} />
      <RecentPathList
        kind={launchMode}
        paths={recentPaths}
        busy={busy}
        onOpen={onRecentPathOpen}
      />
    </div>
  )
}

function RecentPathList({
  kind,
  paths,
  busy = false,
  onOpen,
}: {
  kind: LaunchHistoryKind
  paths: string[]
  busy?: boolean
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
        <span className="inline-flex items-center gap-1.5 text-2xs font-semibold uppercase tracking-[0.14em] text-txt-tertiary">
          <History className="h-3 w-3" />
          {label}
        </span>
        <span className="text-2xs text-txt-tertiary">{paths.length}</span>
      </div>
      <div className="max-h-60 space-y-1 overflow-y-auto pr-1">
        {paths.map(path => (
          <button
            key={path}
            type="button"
            disabled={busy}
            onClick={() => void onOpen(path)}
            className="group flex min-h-10 w-full items-center gap-2 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-surface-overlay focus:outline-none focus:ring-2 focus:ring-accent/25 disabled:cursor-wait disabled:opacity-60"
          >
            <Icon className="h-3.5 w-3.5 flex-none text-txt-tertiary transition-colors group-hover:text-accent" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-xs text-txt-secondary">{pathName(path)}</span>
              <span className="block truncate font-mono text-2xs text-txt-tertiary" title={path}>
                {path}
              </span>
            </span>
            <ChevronRight className="h-3.5 w-3.5 flex-none text-txt-tertiary transition-colors group-hover:text-txt-secondary" />
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
  const inputId = 'launcher-config-path'
  const validationId = `${inputId}-validation`
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
        <div className="px-1 text-2xs text-txt-tertiary">
          Native picker unavailable on this server; enter the YAML path manually.
        </div>
      )}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <label htmlFor={inputId} className="sr-only">YAML config path</label>
        <input
          id={inputId}
          value={pathValue}
          aria-describedby={validationId}
          aria-invalid={validation.validatedPath === pathValue.trim() && validation.status === 'invalid' ? true : undefined}
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
          className="w-full min-w-0 flex-1 rounded-md border border-border-subtle bg-surface-raised px-2.5 py-1.5 font-mono text-xs text-txt-primary outline-none transition-colors focus:border-border"
        />
        <button
          type="button"
          disabled={!pathReady}
          onClick={() => void onManualOpen()}
          className="w-full rounded-md border border-border-subtle px-3 py-1.5 text-xs font-medium text-txt-secondary transition-colors hover:text-txt-primary disabled:cursor-not-allowed disabled:opacity-40 sm:w-auto sm:flex-none"
        >
          Open Config Path
        </button>
      </div>
      <PathValidationHint id={validationId} validation={validation} pathValue={pathValue} />
      <RecentPathList
        kind="yaml"
        paths={recentPaths}
        onOpen={onRecentPathOpen}
      />
    </div>
  )
}

function PathValidationHint({
  id,
  validation,
  pathValue,
}: {
  id: string
  validation: PathValidationState
  pathValue: string
}) {
  const currentPath = pathValue.trim()
  if (!currentPath) {
    return null
  }

  const waitingForCurrentPath = validation.validatedPath !== currentPath
  const valid = !waitingForCurrentPath && validation.status === 'valid'
  const checking = waitingForCurrentPath || validation.status === 'checking' || validation.status === 'idle'
  const Icon = valid ? CheckCircle2 : checking ? Loader2 : AlertTriangle
  const text = waitingForCurrentPath
    ? 'Waiting to check path...'
    : valid && validation.normalizedPath
      ? validation.normalizedPath
      : validation.message

  return (
    <div
      id={id}
      role="status"
      aria-live="polite"
      className={clsx(
        'flex items-center gap-1.5 truncate px-1 text-2xs font-mono',
        valid ? 'text-emerald-700 dark:text-emerald-300' : checking ? 'text-txt-tertiary' : 'text-amber-700 dark:text-amber-300',
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
      <FileCode className="h-4 w-4 flex-none text-txt-tertiary transition-colors group-hover:text-accent" />
      <div className="flex-1 min-w-0">
        <div className="text-sm text-txt-primary">{config.label}</div>
      </div>
      {config.kind === 'workspace_default' && (
        <span className="flex-none text-2xs text-txt-tertiary">default</span>
      )}
      <ChevronRight className="h-3.5 w-3.5 flex-none text-txt-tertiary transition-colors group-hover:text-txt-secondary" />
    </button>
  )
}
