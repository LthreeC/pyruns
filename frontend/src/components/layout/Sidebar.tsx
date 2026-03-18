import { useState } from 'react'
import { NavLink, useNavigate } from 'react-router-dom'
import {
  LayoutDashboard, Wand2, ListTodo, Terminal, Rocket,
  Sun, Moon, ChevronsUpDown, Loader2, FileCode,
} from 'lucide-react'
import clsx from 'clsx'
import { useWorkspaceStore, useThemeStore } from '@/store'
import * as api from '@/api'

const NAV_ITEMS = [
  { to: '/', icon: LayoutDashboard, label: 'Home', end: true },
  { to: '/generator', icon: Wand2, label: 'Generator' },
  { to: '/manager', icon: ListTodo, label: 'Manager' },
  { to: '/monitor', icon: Terminal, label: 'Monitor' },
]

export default function Sidebar() {
  const workspace = useWorkspaceStore(s => s.workspace)
  const setWorkspace = useWorkspaceStore(s => s.setWorkspace)
  const { theme, toggle } = useThemeStore()
  const navigate = useNavigate()
  const [picking, setPicking] = useState(false)
  const scriptFileName = workspace?.script_path?.split(/[\\/]/).pop() || ''

  const handlePickScript = async () => {
    setPicking(true)
    try {
      const nextWorkspace = await api.pickLauncherScript()
      setWorkspace(nextWorkspace)
      navigate('/')
    } catch {
      // User-cancelled picker should stay quiet.
    } finally {
      setPicking(false)
    }
  }

  return (
    <aside className="flex h-screen w-sidebar flex-none flex-col border-r border-border-subtle bg-surface-raised">
      <div className="flex h-12 items-center gap-2 border-b border-border-subtle px-4">
        <Rocket className="h-4 w-4 text-accent" />
        <div className="min-w-0">
          <div className="text-sm font-semibold tracking-tight text-txt-primary">Pyruns</div>
          <div className="text-[10px] uppercase tracking-[0.18em] text-txt-tertiary">workspace console</div>
        </div>
      </div>

      <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-2 py-3">
        {NAV_ITEMS.map(({ to, icon: Icon, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) => clsx(
              'flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors',
              isActive
                ? 'border border-border-subtle bg-surface-overlay text-txt-primary'
                : 'border border-transparent text-txt-secondary hover:bg-surface-overlay hover:text-txt-primary'
            )}
          >
            <Icon className="h-4 w-4 flex-none" />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>

      <div className="border-t border-border-subtle p-3">
        <button
          type="button"
          onClick={handlePickScript}
          disabled={picking}
          className="w-full rounded-xl border border-border-subtle bg-surface-overlay/60 px-3 py-3.5 text-left transition-colors hover:border-border hover:bg-surface-overlay disabled:opacity-60"
        >
          <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.18em] text-txt-tertiary">
            <FileCode className="h-3.5 w-3.5" />
            <span>Script File</span>
            <div className="flex-1 border-t border-border-subtle/80" />
            {picking ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <ChevronsUpDown className="h-3.5 w-3.5" />}
          </div>
          <div
            className="mt-3 truncate rounded-lg border border-border-subtle bg-surface-raised px-3 py-2 font-mono text-sm font-medium text-txt-primary"
            title={scriptFileName || 'Choose a Python script'}
          >
            {scriptFileName || 'Choose .py file'}
          </div>

          <div className="mt-3 text-[10px] uppercase tracking-[0.18em] text-txt-tertiary">Workspace</div>
          <div
            className="mt-1 truncate text-2xs text-txt-secondary"
            title={workspace?.run_root || 'Click to choose a Python script and switch workspace'}
          >
            {workspace?.run_root || 'Click to choose a Python script and switch workspace'}
          </div>
        </button>

        <button
          type="button"
          onClick={toggle}
          className="mt-2 flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
        >
          {theme === 'dark' ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
          <span>{theme === 'dark' ? 'Light Mode' : 'Dark Mode'}</span>
        </button>
      </div>
    </aside>
  )
}
