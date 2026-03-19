import { useEffect, useRef, useCallback } from 'react'
import { createLogStream } from '@/api'
import type { LogStreamMessage } from '@/types'

interface UseLogStreamOptions {
  taskName: string | null
  onChunk: (message: LogStreamMessage) => void
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
      if (wsRef.current !== ws) {
        return
      }
      try {
        const msg = JSON.parse(ev.data) as LogStreamMessage
        if (msg.type === 'chunk' && msg.content) {
          onChunkRef.current(msg)
        }
      } catch { /* ignore parse errors */ }
    }

    ws.onerror = () => ws.close()

    return () => {
      ws.onmessage = null
      ws.onerror = null
      if (wsRef.current === ws) {
        wsRef.current = null
      }
      ws.close()
    }
  }, [taskName, enabled, disconnect])

  return { disconnect }
}
