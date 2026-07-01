import { useEffect, useRef, useCallback } from 'react'
import { createLogStream } from '@/api'
import type { LogStreamMessage } from '@/types'

interface UseLogStreamOptions {
  taskName: string | null
  onChunk: (message: LogStreamMessage) => void
  onDisconnect?: () => void
  enabled?: boolean
  logFileName?: string
  offset?: number
}

export function useLogStream({ taskName, onChunk, onDisconnect, enabled = true, logFileName, offset }: UseLogStreamOptions) {
  const wsRef = useRef<WebSocket | null>(null)
  const onChunkRef = useRef(onChunk)
  const onDisconnectRef = useRef(onDisconnect)
  const offsetRef = useRef(offset)
  onChunkRef.current = onChunk
  onDisconnectRef.current = onDisconnect
  offsetRef.current = offset

  const disconnect = useCallback(() => {
    const ws = wsRef.current
    if (ws) {
      ws.onmessage = null
      ws.onerror = null
      ws.onclose = null
      wsRef.current = null
      ws.close()
    }
  }, [])

  useEffect(() => {
    if (!taskName || !enabled) { disconnect(); return }

    const ws = createLogStream(taskName, { logFileName, offset: offsetRef.current })
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
    ws.onclose = () => {
      if (wsRef.current !== ws) {
        return
      }
      wsRef.current = null
      onDisconnectRef.current?.()
    }

    return () => {
      ws.onmessage = null
      ws.onerror = null
      ws.onclose = null
      if (wsRef.current === ws) {
        wsRef.current = null
      }
      ws.close()
    }
  }, [taskName, enabled, disconnect, logFileName])

  return { disconnect }
}
