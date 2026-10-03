import { NavLink, useSearchParams } from 'react-router-dom'
import { Suspense, lazy, useRef, useState } from 'react'
import {
  LayoutDashboard, Wand2, ListTodo, Terminal, Rocket,
  Sun, Moon, ChevronsUpDown, FileCode, SlidersHorizontal, Settings2,
} from 'lucide-react'
import clsx from 'clsx'
import { useWorkspaceStore, useThemeStore, usePreferencesStore } from '@/store'
import { getWorkspaceWorkingPath } from '@/utils/workspace'
import UpdateControl from './UpdateControl'

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
  const openPreferences = usePreferencesStore(s => s.open)
  const [searchParams, setSearchParams] = useSearchParams()
  const [runtimeOpen, setRuntimeOpen] = useState(false)
  const runtimeButtonRef = useRef<HTMLButtonElement>(null)
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

  const openWorkspaceLauncher = (mode: 'python' | 'shell') => {
    const nextParams = new URLSearchParams(searchParams)
    nextParams.set('launcher', '1')
    nextParams.set('mode', mode)
    nextParams.delete('script')
    nextParams.delete('config')
    setSearchParams(nextParams)
  }

  const closeRuntime = () => {
    setRuntimeOpen(false)
    window.requestAnimationFrame(() => runtimeButtonRef.current?.focus())
  }

  return (
    <aside
      className="app-navigation flex h-full flex-none flex-col border-r border-border-subtle"
      style={{ width }}
    >
      <div>
        <div className={clsx('flex h-16 items-center', compact ? 'justify-center gap-1 px-0' : 'gap-2.5 px-4')}>
          <span className={clsx('inline-flex flex-none items-center justify-center rounded-lg text-accent-ink', compact ? 'h-6 w-5' : 'h-8 w-8 bg-accent/10')}>
            <Rocket aria-hidden="true" className="h-[18px] w-[18px]" />
          </span>
          {!compact && (
            <div className="min-w-0 flex-1">
              <div className="text-[15px] font-semibold tracking-tight text-txt-primary">Pyruns</div>
            </div>
          )}
          <UpdateControl compact={compact || width < 220} />
        </div>
      </div>

      <nav className={clsx('flex flex-1 flex-col gap-1 overflow-y-auto pb-4 pt-1', compact ? 'px-2' : 'px-2.5')}>
        {NAV_ITEMS.map(({ to, icon: Icon, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            aria-label={label}
            title={label}
            className={({ isActive }) => clsx(
              'flex min-h-11 items-center gap-2.5 rounded-lg py-2 text-sm font-medium transition-colors sm:min-h-10',
              compact ? 'justify-center px-0' : 'pl-2.5 pr-3',
              isActive
                ? 'bg-accent/10 text-accent-ink'
                : 'text-txt-secondary hover:bg-surface-hover hover:text-txt-primary'
            )}
          >
            <Icon className="h-4 w-4 flex-none" />
            {!compact && <span>{label}</span>}
          </NavLink>
        ))}
      </nav>

      <div className={clsx('border-t border-border-subtle', compact ? 'p-2' : 'p-2.5')}>
        {!compact && (
          <div className="mb-2 px-2 text-[11px] font-medium text-txt-tertiary">
            Workspace
          </div>
        )}
        <button
          data-launcher-trigger="true"
          type="button"
          onClick={() => openWorkspaceLauncher(shellWorkspaceActive ? 'shell' : 'python')}
          aria-label={workspaceLabel}
          title={workspacePathHint}
          className="touch-target min-h-11 w-full rounded-lg border border-border-subtle bg-surface-raised px-2.5 py-2.5 text-left transition-colors hover:border-border-strong focus:outline-none focus:ring-2 focus:ring-accent/25"
        >
          <div className={clsx('flex items-center gap-2', compact && 'justify-center')}>
            <FileCode className="h-4 w-4 flex-none text-txt-tertiary" />
            {!compact && (
              <>
                <span
                  className="min-w-0 flex-1 truncate text-sm font-medium text-txt-primary"
                  title={workspaceLabel}
                >
                  {workspaceLabel}
                </span>
                <span className="flex-none rounded bg-accent/10 px-1.5 py-0.5 text-[10px] font-medium text-accent-ink">
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
          ref={runtimeButtonRef}
          type="button"
          onClick={() => setRuntimeOpen(true)}
          aria-label="Runtime"
          title="Runtime"
          className="touch-target mt-1 min-h-11 w-full rounded-md px-2 py-2 text-left transition-colors hover:bg-surface-overlay focus:outline-none focus:ring-2 focus:ring-accent/25 sm:min-h-10"
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

        <div className={clsx('mt-2 flex gap-1 border-t border-border-subtle pt-2', compact && 'flex-col')}>
          <button
            type="button"
            onClick={event => openPreferences(event.currentTarget)}
            aria-label="Preferences"
            aria-keyshortcuts="Control+, Meta+,"
            title="Preferences (Ctrl/Cmd+,)"
            className={clsx('touch-target flex min-h-11 min-w-0 flex-1 items-center gap-2.5 rounded-lg px-2.5 text-sm text-txt-secondary transition-colors hover:bg-surface-hover hover:text-txt-primary sm:min-h-9', compact && 'justify-center px-0')}
          >
            <Settings2 aria-hidden="true" className="h-4 w-4 flex-none" />
            {!compact && <span className="min-w-0 truncate">Preferences</span>}
          </button>
          <button
            type="button"
            onClick={toggle}
            aria-label={theme === 'dark' ? 'Light Mode' : 'Dark Mode'}
            title={theme === 'dark' ? 'Light Mode' : 'Dark Mode'}
            className="touch-target flex h-11 w-11 flex-none items-center justify-center rounded-lg text-txt-tertiary transition-colors hover:bg-surface-hover hover:text-txt-primary sm:h-9 sm:w-9"
          >
            {theme === 'dark' ? <Sun aria-hidden="true" className="h-4 w-4" /> : <Moon aria-hidden="true" className="h-4 w-4" />}
          </button>
        </div>
      </div>
      {runtimeOpen && (
        <Suspense fallback={null}>
          <RuntimePanel open={runtimeOpen} left={width + 8} onClose={closeRuntime} />
        </Suspense>
      )}
    </aside>
  )
}
