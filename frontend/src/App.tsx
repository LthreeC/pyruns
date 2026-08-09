import {
  Component,
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useState,
  type ComponentType,
  type ReactNode,
} from 'react'
import {
  Routes,
  Route,
  type BlockerFunction,
  useBlocker,
  useLocation,
  useNavigate,
  useSearchParams,
} from 'react-router-dom'
import AppShell from '@/components/layout/AppShell'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import ConfirmationHost from '@/components/shared/ConfirmationHost'
import ToastHost from '@/components/shared/ToastHost'
import {
  applyThemeClass,
  useGeneratorStore,
  useLauncherStore,
  useRuntimeStore,
  useTaskDetailDraftStore,
  useWorkspaceStore,
  useThemeStore,
} from '@/store'

function lazyWithReload<T extends ComponentType<any>>(
  key: string,
  loader: () => Promise<{ default: T }>,
) {
  const storageKey = `pyruns.chunk-reload.${key}`
  return lazy(async () => {
    try {
      const module = await loader()
      try {
        window.sessionStorage.removeItem(storageKey)
      } catch {
        // Session storage is optional; successful loading needs no recovery state.
      }
      return module
    } catch (error) {
      let shouldReload = false
      try {
        shouldReload = window.sessionStorage.getItem(storageKey) !== '1'
        if (shouldReload) {
          window.sessionStorage.setItem(storageKey, '1')
        }
      } catch {
        // Fall through to the visible recovery screen when storage is unavailable.
      }
      if (shouldReload) {
        await new Promise(resolve => window.setTimeout(resolve, 250))
        window.location.reload()
        return new Promise<never>(() => {})
      }
      throw error
    }
  })
}

const DashboardPage = lazyWithReload('dashboard', () => import('@/components/dashboard/DashboardPage'))
const GeneratorPage = lazyWithReload('generator', () => import('@/components/generator/GeneratorPage'))
const ManagerPage = lazyWithReload('manager', () => import('@/components/manager/ManagerPage'))
const MonitorPage = lazyWithReload('monitor', () => import('@/components/monitor/MonitorPage'))
const LauncherPage = lazyWithReload('launcher', () => import('@/components/launcher/LauncherPage'))

class RouteErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (!this.state.failed) {
      return this.props.children
    }
    return (
      <div role="alert" className="flex min-h-dvh items-center justify-center bg-surface-base p-6">
        <div className="w-full max-w-md rounded-lg border border-border-default bg-surface-raised p-6 text-center shadow-lg">
          <h1 className="text-base font-semibold text-txt-primary">Reconnect to the interface</h1>
          <p className="mt-2 text-sm leading-6 text-txt-secondary">
            This screen could not finish loading. The interface may have been updated while it was open.
          </p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="mt-5 inline-flex min-h-10 items-center justify-center rounded-md bg-accent px-4 text-sm font-semibold text-white transition-colors hover:bg-accent-hover"
          >
            Reload interface
          </button>
        </div>
      </div>
    )
  }
}

function RouteLoadingFallback() {
  return (
    <div role="status" aria-live="polite" className="flex h-full min-h-[16rem] items-center justify-center bg-surface-base text-sm text-txt-tertiary">
      Loading workspace...
    </div>
  )
}

const ROUTE_TITLES: Record<string, string> = {
  '/': 'Dashboard',
  '/launcher': 'Choose Workspace',
  '/generator': 'Generator',
  '/manager': 'Task Manager',
  '/monitor': 'Monitor',
}

const LAUNCHER_SEARCH_PARAMS = ['launcher', 'mode', 'script', 'config']

function searchWithoutLauncherState(search: string) {
  const params = new URLSearchParams(search)
  LAUNCHER_SEARCH_PARAMS.forEach(key => params.delete(key))
  return params.toString()
}

function NotFoundPage() {
  const navigate = useNavigate()
  return (
    <div className="flex h-full min-h-[20rem] items-center justify-center bg-surface-base p-6 text-center">
      <div>
        <h1 className="text-lg font-semibold text-txt-primary">Page not found</h1>
        <p className="mt-2 text-sm text-txt-secondary">This Pyruns page does not exist.</p>
        <button type="button" onClick={() => navigate('/')} className="mt-4 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-white">
          Go to Dashboard
        </button>
      </div>
    </div>
  )
}

export default function App() {
  const location = useLocation()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [showLauncher, setShowLauncher] = useState(false)
  const fetchWorkspace = useWorkspaceStore(s => s.fetch)
  const theme = useThemeStore(s => s.theme)
  const runtimeDirty = useRuntimeStore(s => s.dirty)
  const generatorDirty = useGeneratorStore(s => s.dirty)
  const launcherLoading = useLauncherStore(s => s.loading)
  const taskDetailDirty = useTaskDetailDraftStore(s => s.dirty)
  const shouldWarnBeforeUnload = runtimeDirty || generatorDirty || taskDetailDirty || launcherLoading
  const shouldBlockNavigation = useCallback<BlockerFunction>(
    ({ currentLocation, nextLocation }) => {
      if (useLauncherStore.getState().loading) {
        return true
      }
      if (!useTaskDetailDraftStore.getState().dirty) {
        return false
      }
      if (currentLocation.pathname !== nextLocation.pathname) {
        return true
      }
      return searchWithoutLauncherState(currentLocation.search)
        !== searchWithoutLauncherState(nextLocation.search)
    },
    [],
  )
  const navigationBlocker = useBlocker(shouldBlockNavigation)

  useEffect(() => {
    applyThemeClass(theme)
  }, [theme])

  useEffect(() => {
    fetchWorkspace()
  }, [])

  useEffect(() => {
    if (!shouldWarnBeforeUnload) {
      return
    }
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', handleBeforeUnload)
    return () => window.removeEventListener('beforeunload', handleBeforeUnload)
  }, [shouldWarnBeforeUnload])

  useEffect(() => {
    if (navigationBlocker.state === 'blocked' && !launcherLoading && !taskDetailDirty) {
      navigationBlocker.reset()
    }
  }, [launcherLoading, navigationBlocker, taskDetailDirty])

  useEffect(() => {
    setShowLauncher(searchParams.get('launcher') === '1' || location.pathname === '/launcher')
  }, [location.pathname, searchParams])

  useEffect(() => {
    const pageTitle = ROUTE_TITLES[location.pathname] || 'Page not found'
    document.title = `${pageTitle} · Pyruns`
    const frame = window.requestAnimationFrame(() => {
      document.getElementById('route-heading')?.focus()
    })
    return () => window.cancelAnimationFrame(frame)
  }, [location.pathname])

  const closeLauncher = () => {
    setShowLauncher(false)
    if (location.pathname === '/launcher') {
      navigate('/', { replace: true })
      return
    }
    const nextParams = new URLSearchParams(searchParams)
    nextParams.delete('launcher')
    nextParams.delete('mode')
    nextParams.delete('script')
    nextParams.delete('config')
    setSearchParams(nextParams, { replace: true })
  }

  return (
    <>
      <RouteErrorBoundary key={location.pathname}>
        <Suspense fallback={<RouteLoadingFallback />}>
          <Routes>
            <Route element={<AppShell />}>
              <Route index element={<DashboardPage />} />
              <Route path="launcher" element={<DashboardPage />} />
              <Route path="generator" element={<GeneratorPage />} />
              <Route path="manager" element={<ManagerPage />} />
              <Route path="monitor" element={<MonitorPage />} />
              <Route path="*" element={<NotFoundPage />} />
            </Route>
          </Routes>
          {showLauncher && <LauncherPage onClose={closeLauncher} />}
        </Suspense>
      </RouteErrorBoundary>
      <ConfirmDialog
        open={navigationBlocker.state === 'blocked' && !launcherLoading && taskDetailDirty}
        title="Discard unsaved task details?"
        description="Your Notes, Env, or rename draft will be lost when you leave this page."
        confirmLabel="Discard and Leave"
        confirmVariant="danger"
        onConfirm={() => {
          if (navigationBlocker.state === 'blocked') {
            navigationBlocker.proceed()
          }
        }}
        onCancel={() => {
          if (navigationBlocker.state === 'blocked') {
            navigationBlocker.reset()
          }
        }}
      />
      <ConfirmationHost />
      <ToastHost />
    </>
  )
}
