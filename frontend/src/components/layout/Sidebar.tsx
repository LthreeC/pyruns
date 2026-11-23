import { NavLink, useSearchParams } from 'react-router-dom'
import { Suspense, lazy, useRef, useState } from 'react'
import {
  LayoutDashboard, Wand2, ListTodo, Terminal, Rocket,
  Sun, Moon, Languages, ChevronsUpDown, FileCode, SlidersHorizontal, Settings2, Cpu,
} from 'lucide-react'
import clsx from 'clsx'
import { requestConfirmation, useRuntimeStore, useWorkspaceStore, useThemeStore, usePreferencesStore } from '@/store'
import { getWorkspaceWorkingPath } from '@/utils/workspace'
import UpdateControl from './UpdateControl'
import { setLanguage, useTranslation } from '@/i18n'

const RuntimePanel = lazy(() => import('./RuntimePanel'))
type RuntimeTool = 'runtime' | 'gpu'

const NAV_ITEMS = [
  { to: '/', icon: LayoutDashboard, label: 'Home', end: true },
  { to: '/generator', icon: Wand2, label: 'Generator' },
  { to: '/manager', icon: ListTodo, label: 'Manager' },
  { to: '/monitor', icon: Terminal, label: 'Monitor' },
]

interface SidebarProps {
  width?: number
  compact?: boolean
  onRouteIntent: (path: string) => void
}

export default function Sidebar({ width = 220, compact = false, onRouteIntent }: SidebarProps) {
  const { t, i18n } = useTranslation()
  const chinese = i18n.resolvedLanguage === 'zh-CN'
  const languageAction = chinese ? 'Switch to English' : 'Switch to Chinese'
  const workspace = useWorkspaceStore(s => s.workspace)
  const { theme, toggle } = useThemeStore()
  const openPreferences = usePreferencesStore(s => s.open)
  const [searchParams, setSearchParams] = useSearchParams()
  const [runtimeTool, setRuntimeTool] = useState<RuntimeTool | null>(null)
  const runtimeButtonRef = useRef<HTMLButtonElement>(null)
  const gpuButtonRef = useRef<HTMLButtonElement>(null)
  const scriptFileName = workspace?.script_path?.split(/[\\/]/).pop() || ''
  const workspaceReady = workspace?.workspace_ready === true
  const shellWorkspaceActive = workspaceReady && workspace?.workspace_kind === 'shell'
  const visibleWorkspacePath = getWorkspaceWorkingPath(workspace)
  const visibleWorkspaceLeaf = visibleWorkspacePath?.split(/[\\/]/).filter(Boolean).pop() || ''
  const workspaceLabel = !workspaceReady
    ? t('Choose')
    : shellWorkspaceActive
      ? (visibleWorkspaceLeaf || '_shell_')
      : (scriptFileName || t('Choose .py file'))
  const workspaceModeLabel = !workspaceReady ? t('Setup') : shellWorkspaceActive ? 'Shell' : 'Python'
  const workspacePathHint = !workspaceReady
    ? t('Workspace needed - Choose workspace')
    : visibleWorkspacePath || t('Choose a Python script or choose a shell workspace folder')
  const runtimeLabel = workspace?.settings?.python_executable
    ? t('Python path')
    : workspace?.settings?.conda_env
      ? String(workspace.settings.conda_env)
      : workspace?.settings?.global_env && Object.keys(workspace.settings.global_env).length
        ? t('Workspace Env')
        : t('Follow')

  const openWorkspaceLauncher = (mode: 'python' | 'shell') => {
    const nextParams = new URLSearchParams(searchParams)
    nextParams.set('launcher', '1')
    nextParams.set('mode', mode)
    nextParams.delete('script')
    nextParams.delete('config')
    setSearchParams(nextParams)
  }

  const openRuntimeTool = async (tool: RuntimeTool) => {
    if (tool === runtimeTool) return
    if (runtimeTool && useRuntimeStore.getState().dirty && !await requestConfirmation({
      title: t('Discard unsaved runtime changes?'),
      description: t('Switching panels will discard the current draft.'),
      confirmLabel: t('Discard'),
    })) return
    setRuntimeTool(tool)
  }

  const closeRuntime = () => {
    const trigger = runtimeTool === 'gpu' ? gpuButtonRef : runtimeButtonRef
    setRuntimeTool(null)
    window.requestAnimationFrame(() => trigger.current?.focus())
  }

  return (
    <aside
      className="app-navigation flex h-full flex-none flex-col border-r border-border-subtle"
      style={{ width }}
    >
      <div>
        <div className={clsx('flex h-12 items-center', compact ? 'justify-center gap-1 px-0' : 'gap-2 px-3')}>
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

      <nav className={clsx('flex flex-1 flex-col gap-0.5 overflow-y-auto pb-2 pt-1', compact ? 'px-2' : 'px-2.5')}>
        {NAV_ITEMS.map(({ to, icon: Icon, label, end }) => (
          <NavLink
            key={to}
            to={to}
            data-tour={to === '/' ? 'home' : to.slice(1)}
            end={end}
            onPointerEnter={() => onRouteIntent(to)}
            onFocus={() => onRouteIntent(to)}
            aria-label={t(label)}
            title={t(label)}
            className={({ isActive }) => clsx(
              'touch-target flex min-h-11 items-center gap-2.5 rounded-md py-1.5 text-sm font-medium transition-colors sm:min-h-9',
              compact ? 'justify-center px-0' : 'pl-2.5 pr-3',
              isActive
                ? 'bg-accent/10 text-accent-ink'
                : 'text-txt-secondary hover:bg-surface-hover hover:text-txt-primary'
            )}
          >
            <Icon className="h-4 w-4 flex-none" />
            {!compact && <span>{t(label)}</span>}
          </NavLink>
        ))}
      </nav>

      <div className={clsx('border-t border-border-subtle', compact ? 'p-2' : 'p-2.5')}>
        {!compact && (
          <div className="mb-1 px-2 text-[11px] font-medium text-txt-tertiary">
            {t('Workspace')}
          </div>
        )}
        <button
          data-launcher-trigger="true"
          data-tour="workspace"
          type="button"
          onClick={() => openWorkspaceLauncher(shellWorkspaceActive ? 'shell' : 'python')}
          aria-label={workspaceLabel}
          title={workspacePathHint}
          className="touch-target min-h-11 w-full rounded-md px-2 py-1.5 text-left transition-colors hover:bg-surface-hover focus:outline-none focus:ring-2 focus:ring-accent/25"
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
          data-tour="runtime"
          data-runtime-tool="runtime"
          type="button"
          onClick={() => void openRuntimeTool('runtime')}
          aria-label={t('Runtime')}
          aria-expanded={runtimeTool === 'runtime'}
          title={t('Runtime')}
          className="touch-target mt-0.5 min-h-11 w-full rounded-md px-2 py-1.5 text-left transition-colors hover:bg-surface-overlay focus:outline-none focus:ring-2 focus:ring-accent/25 sm:min-h-9"
        >
          <div className={clsx('flex items-center gap-2', compact && 'justify-center')}>
            <SlidersHorizontal className="h-4 w-4 flex-none text-txt-tertiary" />
            {!compact && (
              <>
                <span className="min-w-0 flex-1 text-sm font-medium text-txt-secondary">
                  {t('Runtime')}
                </span>
                <span className="max-w-[112px] truncate rounded-md bg-surface-overlay px-1.5 py-0.5 text-[10px] font-medium text-txt-secondary">
                  {runtimeLabel}
                </span>
              </>
            )}
          </div>
        </button>

        <button
          ref={gpuButtonRef}
          data-runtime-tool="gpu"
          type="button"
          onClick={() => void openRuntimeTool('gpu')}
          aria-label={t('GPU scheduling')}
          aria-expanded={runtimeTool === 'gpu'}
          title={t('GPU scheduling')}
          className={clsx('touch-target mt-0.5 flex min-h-11 w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm font-medium text-txt-secondary transition-colors hover:bg-surface-overlay aria-expanded:bg-accent/10 aria-expanded:text-accent-ink sm:min-h-9', compact && 'justify-center')}
        >
          <Cpu aria-hidden="true" className="size-4 flex-none text-txt-tertiary" />
          {!compact && <span className="min-w-0 truncate">{t('GPU scheduling')}</span>}
        </button>

        <div className={clsx('mt-2 flex gap-1 border-t border-border-subtle pt-2', compact && 'flex-col')}>
          <div data-tour="guide" className="min-w-0 flex-1">
            <button
              type="button"
              onClick={event => openPreferences(event.currentTarget)}
              data-tour="preferences"
              aria-label={t('Preferences')}
              aria-keyshortcuts="Control+, Meta+,"
              title={t('Preferences (Ctrl/Cmd+,)')}
              className={clsx('touch-target flex min-h-11 w-full items-center gap-2.5 rounded-lg px-2.5 text-sm text-txt-secondary transition-colors hover:bg-surface-hover hover:text-txt-primary sm:min-h-9', compact && 'justify-center px-0')}
            >
              <Settings2 aria-hidden="true" className="h-4 w-4 flex-none" />
              {!compact && <span className="min-w-0 truncate">{t('Preferences')}</span>}
            </button>
          </div>
          <button
            type="button"
            onClick={() => void setLanguage(chinese ? 'en' : 'zh-CN')}
            aria-label={t(languageAction)}
            title={t(languageAction)}
            data-testid="language-toggle"
            data-tour="language"
            className="touch-target flex h-11 w-11 flex-none items-center justify-center rounded-lg text-txt-secondary transition-colors hover:bg-surface-hover hover:text-txt-primary sm:h-9 sm:w-9"
          >
            <Languages aria-hidden="true" className="h-4 w-4" />
          </button>
          <button
            type="button"
            onClick={toggle}
            aria-label={t(theme === 'dark' ? 'Light Mode' : 'Dark Mode')}
            title={t(theme === 'dark' ? 'Light Mode' : 'Dark Mode')}
            data-testid="theme-toggle"
            data-tour="theme"
            className="touch-target flex h-11 w-11 flex-none items-center justify-center rounded-lg text-txt-tertiary transition-colors hover:bg-surface-hover hover:text-txt-primary sm:h-9 sm:w-9"
          >
            {theme === 'dark' ? <Sun aria-hidden="true" className="h-4 w-4" /> : <Moon aria-hidden="true" className="h-4 w-4" />}
          </button>
        </div>
      </div>
      {runtimeTool && (
        <Suspense fallback={null}>
          <RuntimePanel key={runtimeTool} open section={runtimeTool} left={width + 8} onClose={closeRuntime} />
        </Suspense>
      )}
    </aside>
  )
}
