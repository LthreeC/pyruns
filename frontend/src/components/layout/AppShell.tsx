import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { Outlet } from 'react-router-dom'
import clsx from 'clsx'
import Sidebar from './Sidebar'

const SIDEBAR_WIDTH_STORAGE_KEY = 'pyruns.sidebarWidth'
const DEFAULT_SIDEBAR_WIDTH = 220
const COMPACT_SIDEBAR_WIDTH = 64
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

function readCompactSidebar() {
  if (typeof window === 'undefined') {
    return false
  }
  return window.matchMedia('(max-width: 700px)').matches
}

export default function AppShell() {
  const [sidebarWidth, setSidebarWidth] = useState(readStoredSidebarWidth)
  const [compactSidebar, setCompactSidebar] = useState(readCompactSidebar)
  const [resizing, setResizing] = useState(false)
  const pendingSidebarWidthRef = useRef(sidebarWidth)
  const sidebarResizeFrameRef = useRef<number | null>(null)
  const effectiveSidebarWidth = compactSidebar ? COMPACT_SIDEBAR_WIDTH : sidebarWidth

  const startSidebarResize = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault()
    if (compactSidebar) {
      return
    }
    setResizing(true)
  }, [compactSidebar])

  useEffect(() => {
    if (typeof window === 'undefined') {
      return
    }

    const query = window.matchMedia('(max-width: 700px)')
    const handleChange = () => setCompactSidebar(query.matches)
    handleChange()
    query.addEventListener('change', handleChange)
    return () => query.removeEventListener('change', handleChange)
  }, [])

  useEffect(() => {
    if (!resizing) {
      return
    }

    const previousCursor = document.body.style.cursor
    const previousUserSelect = document.body.style.userSelect
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'

    const persistSidebarWidth = (next: number) => {
      try {
        window.localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(next))
      } catch {
        // Ignore private-mode storage failures; resizing still works for this session.
      }
    }

    const applyPendingSidebarWidth = () => {
      sidebarResizeFrameRef.current = null
      setSidebarWidth(pendingSidebarWidthRef.current)
    }

    const handlePointerMove = (event: PointerEvent) => {
      pendingSidebarWidthRef.current = clampSidebarWidth(event.clientX)
      if (sidebarResizeFrameRef.current == null) {
        sidebarResizeFrameRef.current = window.requestAnimationFrame(applyPendingSidebarWidth)
      }
    }

    const stopResize = () => {
      if (sidebarResizeFrameRef.current != null) {
        window.cancelAnimationFrame(sidebarResizeFrameRef.current)
        sidebarResizeFrameRef.current = null
      }
      setSidebarWidth(pendingSidebarWidthRef.current)
      persistSidebarWidth(pendingSidebarWidthRef.current)
      setResizing(false)
    }

    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', stopResize, { once: true })
    window.addEventListener('pointercancel', stopResize, { once: true })

    return () => {
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', stopResize)
      window.removeEventListener('pointercancel', stopResize)
      if (sidebarResizeFrameRef.current != null) {
        window.cancelAnimationFrame(sidebarResizeFrameRef.current)
        sidebarResizeFrameRef.current = null
      }
      document.body.style.cursor = previousCursor
      document.body.style.userSelect = previousUserSelect
    }
  }, [resizing])

  return (
    <div className="flex h-screen w-screen max-w-full overflow-hidden bg-surface-base">
      <Sidebar width={effectiveSidebarWidth} compact={compactSidebar} />
      {!compactSidebar && (
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
      )}
      <main className="min-w-0 flex-1 overflow-x-hidden overflow-y-auto">
        <Outlet />
      </main>
    </div>
  )
}
