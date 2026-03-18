import { NavLink } from 'react-router-dom'
import { LayoutDashboard, Wand2, ListTodo, Terminal, Rocket, Sun, Moon } from 'lucide-react'
import clsx from 'clsx'
import { useWorkspaceStore, useThemeStore } from '@/store'

const NAV_ITEMS = [
  { to: '/', icon: LayoutDashboard, label: 'Home', end: true },
  { to: '/generator', icon: Wand2, label: 'Generator' },
  { to: '/manager', icon: ListTodo, label: 'Manager' },
  { to: '/monitor', icon: Terminal, label: 'Monitor' },
]

export default function Sidebar() {
  const workspace = useWorkspaceStore(s => s.workspace)
  const { theme, toggle } = useThemeStore()
  const scriptName = workspace?.script_name || ''

  return (
    <aside className="w-sidebar h-screen flex flex-col border-r border-border-subtle bg-surface-raised flex-none">
      {/* Brand */}
      <div className="flex items-center gap-2 px-4 h-12 border-b border-border-subtle flex-none">
        <Rocket className="w-4 h-4 text-accent" />
        <span className="text-sm font-semibold tracking-tight">Pyruns</span>
      </div>

      {/* Navigation */}
      <nav className="flex-1 py-2 px-2 flex flex-col gap-0.5 overflow-y-auto">
        {NAV_ITEMS.map(({ to, icon: Icon, label, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) => clsx(
              'flex items-center gap-2.5 px-3 py-1.5 rounded-md text-sm transition-colors duration-150',
              isActive
                ? 'bg-accent/10 text-accent font-medium'
                : 'text-txt-secondary hover:text-txt-primary hover:bg-surface-overlay'
            )}
          >
            <Icon className="w-4 h-4 flex-none" />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>

      {/* Bottom section */}
      <div className="border-t border-border-subtle flex-none">
        {/* Theme toggle */}
        <button
          onClick={toggle}
          className="w-full flex items-center gap-2.5 px-5 py-2.5 text-sm text-txt-secondary hover:text-txt-primary hover:bg-surface-overlay transition-colors"
        >
          {theme === 'dark' ? <Sun className="w-4 h-4" /> : <Moon className="w-4 h-4" />}
          <span>{theme === 'dark' ? 'Light Mode' : 'Dark Mode'}</span>
        </button>

        {/* Workspace info */}
        {scriptName && (
          <div className="px-4 py-3 border-t border-border-subtle">
            <div className="text-2xs text-txt-tertiary uppercase tracking-wider mb-1">Workspace</div>
            <div className="text-xs text-txt-secondary truncate" title={workspace?.run_root}>
              {scriptName}
            </div>
          </div>
        )}
      </div>
    </aside>
  )
}
