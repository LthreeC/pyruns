import { NavLink, useSearchParams } from 'react-router-dom'
import {
  LayoutDashboard, Wand2, ListTodo, Terminal, Rocket,
  Sun, Moon, ChevronsUpDown, FileCode,
} from 'lucide-react'
import clsx from 'clsx'
import { useMonitorStore, useWorkspaceStore, useThemeStore } from '@/store'
import { getWorkspaceWorkingPath } from '@/utils/workspace'

const NAV_ITEMS = [
  { to: '/', icon: LayoutDashboard, label: 'Home', end: true },
  { to: '/generator', icon: Wand2, label: 'Generator' },
  { to: '/manager', icon: ListTodo, label: 'Manager' },
  { to: '/monitor', icon: Terminal, label: 'Monitor' },
]

interface SidebarProps {
  width?: number
}

export default function Sidebar({ width = 220 }: SidebarProps) {
  const workspace = useWorkspaceStore(s => s.workspace)
  const { theme, toggle } = useThemeStore()
  const [searchParams, setSearchParams] = useSearchParams()
  const scriptFileName = workspace?.script_path?.split(/[\\/]/).pop() || ''
  const shellWorkspaceActive = workspace?.workspace_kind === 'shell'
  const visibleWorkspacePath = getWorkspaceWorkingPath(workspace)
  const visibleWorkspaceLeaf = visibleWorkspacePath?.split(/[\\/]/).filter(Boolean).pop() || ''
  const workspaceLabel = shellWorkspaceActive
    ? (visibleWorkspaceLeaf || '_shell_')
    : (scriptFileName || 'Choose .py file')
  const workspaceModeLabel = shellWorkspaceActive ? 'Shell' : 'Python'

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
          onClick={() => openWorkspaceLauncher(shellWorkspaceActive ? 'shell' : 'python')}
          className="w-full rounded-md border border-border-subtle bg-surface-overlay/55 px-2.5 py-2.5 text-left transition-colors hover:border-border hover:bg-surface-overlay"
        >
          <div className="flex items-center gap-2">
            <FileCode className="h-3.5 w-3.5 flex-none text-txt-tertiary" />
            <span className="min-w-0 flex-1 text-[10px] font-semibold uppercase tracking-[0.18em] text-txt-tertiary">
              Workspace
            </span>
            <span className="rounded-md bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-accent">
              {workspaceModeLabel}
            </span>
            <ChevronsUpDown className="h-3.5 w-3.5 flex-none text-txt-tertiary" />
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
        </button>

        <button
          type="button"
          onClick={toggle}
          className="mt-2 flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-sm text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
        >
          {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          <span>{theme === 'dark' ? 'Light Mode' : 'Dark Mode'}</span>
        </button>
      </div>
    </aside>
  )
}
