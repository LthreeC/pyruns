import { useEffect, useRef, useCallback } from 'react'
import { createLogStream } from '@/api'

interface UseLogStreamOptions {
  taskName: string | null
  onChunk: (text: string) => void
  enabled?: boolean
}

export function useLogStream({ taskName, onChunk, enabled = true }: UseLogStreamOptions) {
  const wsRef = useRef<WebSocket | null>(null)
  const onChunkRef = useRef(onChunk)
  onChunkRef.current = onChunk

  const disconnect = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close()
      wsRef.current = null
    }
  }, [])

  useEffect(() => {
    if (!taskName || !enabled) { disconnect(); return }

    const ws = createLogStream(taskName)
    wsRef.current = ws

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data)
        if (msg.type === 'chunk' && msg.content) {
          onChunkRef.current(msg.content)
        }
      } catch { /* ignore parse errors */ }
    }

    ws.onerror = () => ws.close()

    return () => {
      ws.close()
      wsRef.current = null
    }
  }, [taskName, enabled, disconnect])

  return { disconnect }
}
