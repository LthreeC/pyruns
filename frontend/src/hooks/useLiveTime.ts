import { useState } from 'react'
import { usePolling } from './usePolling'

export function useLiveTime(enabled = true) {
  const [now, setNow] = useState(Date.now)
  usePolling(() => setNow(Date.now()), 1000, enabled)
  return now
}
