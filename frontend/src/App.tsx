import { Suspense, lazy, useEffect, useState } from 'react'
import { Routes, Route, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import AppShell from '@/components/layout/AppShell'
import ToastHost from '@/components/shared/ToastHost'
import { applyThemeClass, useWorkspaceStore, useThemeStore } from '@/store'

const DashboardPage = lazy(() => import('@/components/dashboard/DashboardPage'))
const GeneratorPage = lazy(() => import('@/components/generator/GeneratorPage'))
const ManagerPage = lazy(() => import('@/components/manager/ManagerPage'))
const MonitorPage = lazy(() => import('@/components/monitor/MonitorPage'))
const LauncherPage = lazy(() => import('@/components/launcher/LauncherPage'))

function RouteLoadingFallback() {
  return (
    <div className="flex h-full min-h-[16rem] items-center justify-center bg-surface-base text-sm text-txt-tertiary">
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
    if (searchParams.get('launcher') === '1' || location.pathname === '/launcher') {
      setShowLauncher(true)
    }
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
      <ToastHost />
    </>
  )
}
