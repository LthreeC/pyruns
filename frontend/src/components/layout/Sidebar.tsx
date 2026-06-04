import { NavLink, useSearchParams } from 'react-router-dom'
import { Suspense, lazy, useState } from 'react'
import {
  LayoutDashboard, Wand2, ListTodo, Terminal, Rocket,
  Sun, Moon, ChevronsUpDown, FileCode, SlidersHorizontal,
} from 'lucide-react'
import clsx from 'clsx'
import { useMonitorStore, useWorkspaceStore, useThemeStore } from '@/store'
import { getWorkspaceWorkingPath } from '@/utils/workspace'

const RuntimePanel = lazy(() => import('./RuntimePanel'))

const NAV_ITEMS = [
  { to: '/', icon: LayoutDashboard, label: 'Home', end: true },
  { to: '/generator', icon: Wand2, label: 'Generator' },
  { to: '/manager', icon: ListTodo, label: 'Manager' },
  { to: '/monitor', icon: Terminal, label: 'Monitor' },
]

interface SidebarProps {
  width?: number
  compact?: boolean
}

export default function Sidebar({ width = 220, compact = false }: SidebarProps) {
  const workspace = useWorkspaceStore(s => s.workspace)
  const { theme, toggle } = useThemeStore()
  const [searchParams, setSearchParams] = useSearchParams()
  const [runtimeOpen, setRuntimeOpen] = useState(false)
  const scriptFileName = workspace?.script_path?.split(/[\\/]/).pop() || ''
  const workspaceReady = workspace?.workspace_ready === true
  const shellWorkspaceActive = workspaceReady && workspace?.workspace_kind === 'shell'
  const visibleWorkspacePath = getWorkspaceWorkingPath(workspace)
  const visibleWorkspaceLeaf = visibleWorkspacePath?.split(/[\\/]/).filter(Boolean).pop() || ''
  const workspaceLabel = !workspaceReady
    ? 'Choose'
    : shellWorkspaceActive
      ? (visibleWorkspaceLeaf || '_shell_')
      : (scriptFileName || 'Choose .py file')
  const workspaceModeLabel = !workspaceReady ? 'Setup' : shellWorkspaceActive ? 'Shell' : 'Python'
  const workspacePathHint = !workspaceReady
    ? 'Workspace needed - Choose workspace'
    : visibleWorkspacePath || 'Choose a Python script or choose a shell workspace folder'
  const runtimeLabel = workspace?.settings?.python_executable
    ? 'Python path'
    : workspace?.settings?.conda_env
      ? String(workspace.settings.conda_env)
      : workspace?.settings?.global_env && Object.keys(workspace.settings.global_env).length
        ? 'Workspace Env'
        : 'Follow'

  const clearMonitorSelection = () => {
    useMonitorStore.setState({
      selectedTaskName: null,
      logContent: '',
      logOffset: 0,
      availableLogs: [],
      selectedLog: '',
    })
  }

  const openWorkspaceLauncher = (mode: 'python' | 'shell') => {
    const nextParams = new URLSearchParams(searchParams)
    nextParams.set('launcher', '1')
    nextParams.set('mode', mode)
    nextParams.delete('script')
    nextParams.delete('config')
    setSearchParams(nextParams)
  }

  return (
    <aside
      className="flex h-screen flex-none flex-col border-r border-border-subtle bg-surface-raised"
      style={{ width }}
    >
      <div className={clsx('flex h-12 items-center border-b border-border-subtle', compact ? 'justify-center px-0' : 'gap-2 px-4')}>
        <Rocket className="h-4 w-4 text-accent" />
        {!compact && (
          <div className="min-w-0">
            <div className="text-sm font-semibold tracking-tight text-txt-primary">Pyruns</div>
            <div className="text-[10px] uppercase tracking-[0.18em] text-txt-tertiary">
              {!workspaceReady ? 'workspace needed' : shellWorkspaceActive ? 'shell workspace' : 'script workspace'}
            </div>
          </div>
        )}
      </div>

      <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-2 py-3">
        {NAV_ITEMS.map(({ to, icon: Icon, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            onClick={to === '/monitor' ? clearMonitorSelection : undefined}
            aria-label={label}
            title={label}
            className={({ isActive }) => clsx(
              'flex items-center gap-2.5 rounded-md py-2 text-sm transition-colors',
              compact ? 'justify-center px-0' : 'pl-2.5 pr-3',
              isActive
                ? 'border-l-2 border-accent bg-accent/10 text-accent'
                : 'border-l-2 border-transparent text-txt-secondary hover:bg-surface-overlay hover:text-txt-primary'
            )}
          >
            <Icon className="h-4 w-4 flex-none" />
            {!compact && <span>{label}</span>}
          </NavLink>
        ))}
      </nav>

      <div className="border-t border-border-subtle p-2.5">
        {!compact && (
          <div className="mb-1.5 px-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-txt-tertiary">
            Workspace
          </div>
        )}
        <button
          type="button"
          onClick={() => openWorkspaceLauncher(shellWorkspaceActive ? 'shell' : 'python')}
          aria-label={workspaceLabel}
          title={workspacePathHint}
          className="w-full rounded-md px-2 py-2 text-left transition-colors hover:bg-surface-overlay focus:outline-none focus:ring-2 focus:ring-accent/25"
        >
          <div className={clsx('flex items-center gap-2', compact && 'justify-center')}>
            <FileCode className="h-4 w-4 flex-none text-txt-tertiary" />
            {!compact && (
              <>
                <span
                  className="min-w-0 flex-1 truncate font-mono text-sm font-medium text-txt-primary"
                  title={workspaceLabel}
                >
                  {workspaceLabel}
                </span>
                <span className="flex-none rounded-md bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-accent">
                  {workspaceModeLabel}
                </span>
                <ChevronsUpDown className="h-3.5 w-3.5 flex-none text-txt-tertiary" />
              </>
            )}
          </div>
          {!compact && (
            <div
              className="ml-6 mt-0.5 truncate text-2xs text-txt-tertiary"
              title={workspacePathHint}
            >
              {workspacePathHint}
            </div>
          )}
        </button>

        <button
          type="button"
          onClick={() => setRuntimeOpen(true)}
          aria-label="Runtime"
          title="Runtime"
          className="mt-1 w-full rounded-md px-2 py-2 text-left transition-colors hover:bg-surface-overlay focus:outline-none focus:ring-2 focus:ring-accent/25"
        >
          <div className={clsx('flex items-center gap-2', compact && 'justify-center')}>
            <SlidersHorizontal className="h-4 w-4 flex-none text-txt-tertiary" />
            {!compact && (
              <>
                <span className="min-w-0 flex-1 text-sm font-medium text-txt-secondary">
                  Runtime
                </span>
                <span className="max-w-[112px] truncate rounded-md bg-surface-overlay px-1.5 py-0.5 text-[10px] font-medium text-txt-secondary">
                  {runtimeLabel}
                </span>
              </>
            )}
          </div>
        </button>

        <button
          type="button"
          onClick={toggle}
          aria-label={theme === 'dark' ? 'Light Mode' : 'Dark Mode'}
          title={theme === 'dark' ? 'Light Mode' : 'Dark Mode'}
          className={clsx(
            'mt-2 flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary',
            compact && 'justify-center px-0',
          )}
        >
          {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          {!compact && <span>{theme === 'dark' ? 'Light Mode' : 'Dark Mode'}</span>}
        </button>
      </div>
      {runtimeOpen && (
        <Suspense fallback={null}>
          <RuntimePanel open={runtimeOpen} left={width + 8} onClose={() => setRuntimeOpen(false)} />
        </Suspense>
      )}
    </aside>
  )
}
