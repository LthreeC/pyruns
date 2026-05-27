import { useEffect, useRef } from 'react'

export function usePolling(
  callback: () => void | Promise<void>,
  intervalMs: number,
  enabled = true,
  immediate = true,
) {
  const savedCallback = useRef(callback)
  const inFlightRef = useRef(false)
  const ticketRef = useRef(0)
  savedCallback.current = callback

  useEffect(() => {
    if (!enabled) return

    const tick = () => {
      if (inFlightRef.current) {
        return
      }
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        return
      }

      inFlightRef.current = true
      const ticket = ++ticketRef.current
      let result: void | Promise<void>
      try {
        result = savedCallback.current()
      } catch {
        inFlightRef.current = false
        return
      }

      void Promise.resolve(result)
        .catch(() => {})
        .finally(() => {
          if (ticketRef.current === ticket) {
            inFlightRef.current = false
          }
        })
    }

    if (immediate) {
      tick()
    }

    const id = setInterval(tick, intervalMs)
    return () => {
      clearInterval(id)
      ticketRef.current += 1
      inFlightRef.current = false
    }
  }, [intervalMs, enabled, immediate])
}
