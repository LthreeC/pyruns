import { useCallback, useEffect, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { Outlet } from 'react-router-dom'
import clsx from 'clsx'
import Sidebar from './Sidebar'

const SIDEBAR_WIDTH_STORAGE_KEY = 'pyruns.sidebarWidth'
const DEFAULT_SIDEBAR_WIDTH = 220
const MIN_SIDEBAR_WIDTH = 180
const MAX_SIDEBAR_WIDTH = 360

function clampSidebarWidth(value: number) {
  if (!Number.isFinite(value)) {
    return DEFAULT_SIDEBAR_WIDTH
  }
  return Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, value))
}

function readStoredSidebarWidth() {
  if (typeof window === 'undefined') {
    return DEFAULT_SIDEBAR_WIDTH
  }

  try {
    const stored = Number(window.localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY))
    return clampSidebarWidth(stored || DEFAULT_SIDEBAR_WIDTH)
  } catch {
    return DEFAULT_SIDEBAR_WIDTH
  }
}

export default function AppShell() {
  const [sidebarWidth, setSidebarWidth] = useState(readStoredSidebarWidth)
  const [resizing, setResizing] = useState(false)

  const startSidebarResize = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault()
    setResizing(true)
  }, [])

  useEffect(() => {
    if (!resizing) {
      return
    }

    const previousCursor = document.body.style.cursor
    const previousUserSelect = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    const handlePointerMove = (event: PointerEvent) => {
      const next = clampSidebarWidth(event.clientX)
      setSidebarWidth(next)
      try {
        window.localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(next))
      } catch {
        // Ignore private-mode storage failures; resizing still works for this session.
      }
    }

    const stopResize = () => setResizing(false)

    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', stopResize, { once: true })

    return () => {
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', stopResize)
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousUserSelect
    }
  }, [resizing])

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-surface-base">
      <Sidebar width={sidebarWidth} />
      <button
        type="button"
        aria-label="Resize navigation sidebar"
        aria-orientation="vertical"
        onPointerDown={startSidebarResize}
        className={clsx(
          'h-screen w-1 flex-none cursor-col-resize touch-none transition-colors focus:outline-none focus:ring-2 focus:ring-accent/35',
          resizing ? 'bg-accent/45' : 'bg-transparent hover:bg-accent/25',
        )}
      />
      <main className="flex-1 min-w-0 overflow-hidden">
        <Outlet />
      </main>
    </div>
  )
}
