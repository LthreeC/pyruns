import { useState } from 'react'
import { NavLink, useNavigate, useSearchParams } from 'react-router-dom'
import {
  LayoutDashboard, Wand2, ListTodo, Terminal, Rocket,
  Sun, Moon, ChevronsUpDown, Loader2, FileCode,
} from 'lucide-react'
import clsx from 'clsx'
import { useLauncherStore, useMonitorStore, useWorkspaceStore, useThemeStore } from '@/store'
import * as api from '@/api'

const NAV_ITEMS = [
  { to: '/', icon: LayoutDashboard, label: 'Home', end: true },
  { to: '/generator', icon: Wand2, label: 'Generator' },
  { to: '/manager', icon: ListTodo, label: 'Manager' },
  { to: '/monitor', icon: Terminal, label: 'Monitor' },
]

const PICKER_CANCEL_MESSAGES = new Set(['No script selected.', 'No directory selected.'])

function getPickerErrorMessage(err: unknown) {
  return err instanceof Error ? err.message : String(err || '')
}

interface SidebarProps {
  width?: number
}

export default function Sidebar({ width = 220 }: SidebarProps) {
  const workspace = useWorkspaceStore(s => s.workspace)
  const setWorkspace = useWorkspaceStore(s => s.setWorkspace)
  const openShellWorkspace = useWorkspaceStore(s => s.openShellWorkspace)
  const exitShellWorkspace = useWorkspaceStore(s => s.exitShellWorkspace)
  const selectLauncherScript = useLauncherStore(s => s.selectScript)
  const openLauncherWorkspace = useLauncherStore(s => s.openWorkspace)
  const { theme, toggle } = useThemeStore()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [picking, setPicking] = useState(false)
  const [openingShell, setOpeningShell] = useState(false)
  const [pickerError, setPickerError] = useState('')
  const scriptFileName = workspace?.script_path?.split(/[\\/]/).pop() || ''
  const shellWorkspaceActive = workspace?.workspace_kind === 'shell'
  const workspaceLabel = shellWorkspaceActive ? '_shell_' : (scriptFileName || 'Choose .py file')
  const visibleWorkspacePath = shellWorkspaceActive
    ? (workspace?.project_root || workspace?.run_root)
    : workspace?.run_root

  const clearMonitorSelection = () => {
    useMonitorStore.setState({
      selectedTaskName: null,
      logContent: '',
      logOffset: 0,
      availableLogs: [],
      selectedLog: '',
    })
  }

  const showPickerError = (err: unknown) => {
    const message = getPickerErrorMessage(err)
    if (!message || PICKER_CANCEL_MESSAGES.has(message)) {
      return
    }
    setPickerError(message)
  }

  const openLauncherForConfig = (scriptPath: string) => {
    const nextParams = new URLSearchParams(searchParams)
    nextParams.set('launcher', '1')
    nextParams.set('script', scriptPath)
    setSearchParams(nextParams)
  }

  const openSelectedScriptWorkspace = async () => {
    const selection = await api.pickLauncherScriptPath()
    await selectLauncherScript(selection.script_path)
    const launcherState = useLauncherStore.getState()

    if (launcherState.requiresConfigTemplate && !launcherState.selectedConfig) {
      openLauncherForConfig(selection.script_path)
      return false
    }

    try {
      await openLauncherWorkspace()
      return true
    } catch (err) {
      if (getPickerErrorMessage(err).includes('needs a YAML template')) {
        openLauncherForConfig(selection.script_path)
        return false
      }
      throw err
    }
  }

  const handlePickScript = async () => {
    setPickerError('')
    setPicking(true)
    try {
      if (shellWorkspaceActive) {
        const nextWorkspace = await api.pickLauncherShellRoot()
        setWorkspace(nextWorkspace)
        navigate('/generator')
      } else {
        const opened = await openSelectedScriptWorkspace()
        if (opened) {
          navigate('/')
        }
      }
    } catch (err) {
      showPickerError(err)
    } finally {
      setPicking(false)
    }
  }

  const handlePickScriptWorkspace = async () => {
    setPickerError('')
    setPicking(true)
    try {
      const opened = await openSelectedScriptWorkspace()
      if (opened) {
        navigate('/')
      }
    } catch (err) {
      showPickerError(err)
    } finally {
      setPicking(false)
    }
  }

  const handleToggleShellWorkspace = async () => {
    setPickerError('')
    setOpeningShell(true)
    try {
      if (shellWorkspaceActive) {
        const nextWorkspace = await exitShellWorkspace()
        if (!nextWorkspace) {
          await handlePickScriptWorkspace()
          return
        }
      } else {
        await openShellWorkspace()
      }
      navigate('/generator')
    } catch (err) {
      showPickerError(err)
    } finally {
      setOpeningShell(false)
    }
  }

  return (
    <aside
      className="flex h-screen flex-none flex-col border-r border-border-subtle bg-surface-raised"
      style={{ width }}
    >
      <div className="flex h-12 items-center gap-2 border-b border-border-subtle px-4">
        <Rocket className="h-4 w-4 text-accent" />
        <div className="min-w-0">
          <div className="text-sm font-semibold tracking-tight text-txt-primary">Pyruns</div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-txt-tertiary">
            {shellWorkspaceActive ? 'shell workspace' : 'script workspace'}
          </div>
        </div>
      </div>

      <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-2 py-3">
        {NAV_ITEMS.map(({ to, icon: Icon, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            onClick={to === '/monitor' ? clearMonitorSelection : undefined}
            className={({ isActive }) => clsx(
              'flex items-center gap-2.5 rounded-md py-2 pl-2.5 pr-3 text-sm transition-colors',
              isActive
                ? 'border-l-2 border-accent bg-accent/10 text-accent'
                : 'border-l-2 border-transparent text-txt-secondary hover:bg-surface-overlay hover:text-txt-primary'
            )}
          >
            <Icon className="h-4 w-4 flex-none" />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>

      <div className="border-t border-border-subtle p-2.5">
        <button
          type="button"
          onClick={handlePickScript}
          disabled={picking}
          className="w-full rounded-md px-2.5 py-2.5 text-left transition-colors hover:bg-surface-overlay disabled:opacity-60"
        >
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.18em] text-txt-tertiary">
            <FileCode className="h-3.5 w-3.5" />
            <span>{shellWorkspaceActive ? 'Shell Folder' : 'Script File'}</span>
            <div className="flex-1" />
            {picking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ChevronsUpDown className="h-3.5 w-3.5" />}
          </div>
          <div
            className="mt-2 truncate font-mono text-sm font-medium text-txt-primary"
            title={workspaceLabel}
          >
            {workspaceLabel}
          </div>

          <div
            className="mt-1 truncate text-2xs text-txt-tertiary"
            title={visibleWorkspacePath || 'Choose a Python script or choose a shell workspace folder'}
          >
            {visibleWorkspacePath || 'Choose a Python script or choose a shell workspace folder'}
          </div>
          {shellWorkspaceActive && (
            <div className="mt-2 rounded-md bg-accent/10 px-2 py-1 text-2xs font-medium text-accent">
              Shell mode active
            </div>
          )}
        </button>

        {pickerError && (
          <div
            role="alert"
            title={pickerError}
            className="mt-2 rounded-md bg-amber-500/10 px-2.5 py-2 text-2xs leading-relaxed text-amber-300"
          >
            {pickerError}
          </div>
        )}

        <button
          type="button"
          onClick={() => void handleToggleShellWorkspace()}
          disabled={openingShell}
          className={clsx(
            'mt-1 flex w-full items-center justify-center gap-2 rounded-md px-2.5 py-2 text-sm font-medium transition-colors disabled:opacity-60',
            shellWorkspaceActive
              ? 'bg-accent/10 text-accent'
              : 'text-txt-secondary hover:bg-surface-overlay hover:text-txt-primary'
          )}
          title={shellWorkspaceActive ? 'Exit shell mode' : 'Open the shared shell workspace'}
        >
          {openingShell ? <Loader2 className="h-4 w-4 animate-spin" /> : <Terminal className="h-4 w-4" />}
          <span>{shellWorkspaceActive ? 'Exit Shell Mode' : 'Open Shell Mode'}</span>
        </button>

        <button
          type="button"
          onClick={toggle}
          className="mt-1 flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
        >
          {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          <span>{theme === 'dark' ? 'Light Mode' : 'Dark Mode'}</span>
        </button>
      </div>
    </aside>
  )
}
