import {
  Component,
  Suspense,
  lazy,
  useEffect,
  useState,
  type ComponentType,
  type ReactNode,
} from 'react'
import { Routes, Route, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import AppShell from '@/components/layout/AppShell'
import ToastHost from '@/components/shared/ToastHost'
import { applyThemeClass, useWorkspaceStore, useThemeStore } from '@/store'

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
      <div role="alert" className="flex min-h-screen items-center justify-center bg-surface-base p-6">
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

export default function App() {
  const location = useLocation()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [showLauncher, setShowLauncher] = useState(false)
  const fetchWorkspace = useWorkspaceStore(s => s.fetch)
  const theme = useThemeStore(s => s.theme)

  useEffect(() => {
    applyThemeClass(theme)
  }, [theme])

  useEffect(() => {
    fetchWorkspace()
  }, [])

  useEffect(() => {
    setShowLauncher(searchParams.get('launcher') === '1' || location.pathname === '/launcher')
  }, [location.pathname, searchParams])

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
      <RouteErrorBoundary key={`${location.pathname}${location.search}`}>
        <Suspense fallback={<RouteLoadingFallback />}>
          <Routes>
            <Route element={<AppShell />}>
              <Route index element={<DashboardPage />} />
              <Route path="launcher" element={<DashboardPage />} />
              <Route path="generator" element={<GeneratorPage />} />
              <Route path="manager" element={<ManagerPage />} />
              <Route path="monitor" element={<MonitorPage />} />
            </Route>
          </Routes>
          {showLauncher && <LauncherPage onClose={closeLauncher} />}
        </Suspense>
      </RouteErrorBoundary>
      <ToastHost />
    </>
  )
}
