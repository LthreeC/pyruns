import { useEffect, useState } from 'react'
import { Routes, Route, useSearchParams } from 'react-router-dom'
import AppShell from '@/components/layout/AppShell'
import DashboardPage from '@/components/dashboard/DashboardPage'
import GeneratorPage from '@/components/generator/GeneratorPage'
import ManagerPage from '@/components/manager/ManagerPage'
import MonitorPage from '@/components/monitor/MonitorPage'
import LauncherPage from '@/components/launcher/LauncherPage'
import { applyThemeClass, useWorkspaceStore, useThemeStore } from '@/store'

export default function App() {
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
    if (searchParams.get('launcher') === '1') {
      setShowLauncher(true)
    }
  }, [searchParams])

  const closeLauncher = () => {
    setShowLauncher(false)
    searchParams.delete('launcher')
    searchParams.delete('script')
    searchParams.delete('config')
    setSearchParams(searchParams, { replace: true })
  }

  return (
    <>
      <Routes>
        <Route element={<AppShell />}>
          <Route index element={<DashboardPage />} />
          <Route path="generator" element={<GeneratorPage />} />
          <Route path="manager" element={<ManagerPage />} />
          <Route path="monitor" element={<MonitorPage />} />
        </Route>
      </Routes>
      {showLauncher && <LauncherPage onClose={closeLauncher} />}
    </>
  )
}
