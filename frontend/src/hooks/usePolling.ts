import { useEffect, useRef } from 'react'

export function usePolling(
  callback: () => void,
  intervalMs: number,
  enabled = true,
  immediate = true,
) {
  const savedCallback = useRef(callback)
  savedCallback.current = callback

  useEffect(() => {
    if (!enabled) return

    const tick = () => {
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        return
      }
      savedCallback.current()
    }

    if (immediate) {
      tick()
    }

    const id = setInterval(tick, intervalMs)
    return () => clearInterval(id)
  }, [intervalMs, enabled, immediate])
}
