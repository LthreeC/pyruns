import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
} from 'react'
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
  const sidebarResizePointerIdRef = useRef<number | null>(null)
  const effectiveSidebarWidth = compactSidebar ? COMPACT_SIDEBAR_WIDTH : sidebarWidth

  const startSidebarResize = useCallback((event: ReactPointerEvent<HTMLButtonElement>) => {
    event.preventDefault()
    if (compactSidebar) {
      return
    }
    sidebarResizePointerIdRef.current = event.pointerId
    setResizing(true)
  }, [compactSidebar])

  const resizeSidebarFromKeyboard = useCallback((event: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (compactSidebar) {
      return
    }
    let next = sidebarWidth
    if (event.key === 'ArrowLeft') {
      next = sidebarWidth - 8
    } else if (event.key === 'ArrowRight') {
      next = sidebarWidth + 8
    } else if (event.key === 'Home') {
      next = MIN_SIDEBAR_WIDTH
    } else if (event.key === 'End') {
      next = MAX_SIDEBAR_WIDTH
    } else {
      return
    }
    event.preventDefault()
    next = clampSidebarWidth(next)
    pendingSidebarWidthRef.current = next
    setSidebarWidth(next)
    try {
      window.localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(next))
    } catch {
      // Keep the session value when storage is unavailable.
    }
  }, [compactSidebar, sidebarWidth])

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
      if (event.pointerId !== sidebarResizePointerIdRef.current) {
        return
      }
      pendingSidebarWidthRef.current = clampSidebarWidth(event.clientX)
      if (sidebarResizeFrameRef.current == null) {
        sidebarResizeFrameRef.current = window.requestAnimationFrame(applyPendingSidebarWidth)
      }
    }

    const stopResize = (event: PointerEvent) => {
      if (event.pointerId !== sidebarResizePointerIdRef.current) {
        return
      }
      sidebarResizePointerIdRef.current = null
      if (sidebarResizeFrameRef.current != null) {
        window.cancelAnimationFrame(sidebarResizeFrameRef.current)
        sidebarResizeFrameRef.current = null
      }
      setSidebarWidth(pendingSidebarWidthRef.current)
      persistSidebarWidth(pendingSidebarWidthRef.current)
      setResizing(false)
    }

    window.addEventListener('pointermove', handlePointerMove)
    window.addEventListener('pointerup', stopResize)
    window.addEventListener('pointercancel', stopResize)

    return () => {
      window.removeEventListener('pointermove', handlePointerMove)
      window.removeEventListener('pointerup', stopResize)
      window.removeEventListener('pointercancel', stopResize)
      sidebarResizePointerIdRef.current = null
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
          role="separator"
          aria-label="Resize navigation sidebar"
          aria-orientation="vertical"
          aria-valuemin={MIN_SIDEBAR_WIDTH}
          aria-valuemax={MAX_SIDEBAR_WIDTH}
          aria-valuenow={sidebarWidth}
          aria-valuetext={`${sidebarWidth} pixels`}
          onPointerDown={startSidebarResize}
          onKeyDown={resizeSidebarFromKeyboard}
          onDoubleClick={() => {
            pendingSidebarWidthRef.current = DEFAULT_SIDEBAR_WIDTH
            setSidebarWidth(DEFAULT_SIDEBAR_WIDTH)
            try {
              window.localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(DEFAULT_SIDEBAR_WIDTH))
            } catch {
              // Keep the reset for this session when storage is unavailable.
            }
          }}
          title="Resize navigation. Use Left/Right arrow keys; double-click to reset."
          className={clsx(
            'h-screen w-2 flex-none cursor-col-resize touch-none transition-colors focus:outline-none focus:ring-2 focus:ring-accent/35',
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
