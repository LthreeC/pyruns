import { useEffect, useRef, useCallback } from 'react'
import { createLogStream, createTaskEventStream } from '@/api'
import type { LogStreamMessage, TaskEventMessage } from '@/types'

export type LogStreamStatus = 'idle' | 'connecting' | 'live' | 'reconnecting'
export type TaskEventStreamStatus = LogStreamStatus

interface UseLogStreamOptions {
  taskName: string | null
  onChunk: (message: LogStreamMessage) => void
  onDisconnect?: () => void
  onStatusChange?: (status: LogStreamStatus) => void
  enabled?: boolean
  logFileName?: string
  offset?: number
  logIdentity?: string
  generationKey?: string
}

const LOG_STREAM_RECONNECT_BASE_MS = 750
const LOG_STREAM_RECONNECT_MAX_MS = 10_000
const TASK_EVENT_RECONNECT_BASE_MS = 750
const TASK_EVENT_RECONNECT_MAX_MS = 15_000

interface UseTaskEventsOptions {
  onInvalidate: () => void
  onStatusChange?: (status: TaskEventStreamStatus) => void
  enabled?: boolean
  generationKey?: string
}

export function useTaskEvents({
  onInvalidate,
  onStatusChange,
  enabled = true,
  generationKey = '',
}: UseTaskEventsOptions) {
  const wsRef = useRef<WebSocket | null>(null)
  const onInvalidateRef = useRef(onInvalidate)
  const onStatusChangeRef = useRef(onStatusChange)
  const generationKeyRef = useRef(generationKey)
  onInvalidateRef.current = onInvalidate
  onStatusChangeRef.current = onStatusChange
  generationKeyRef.current = generationKey

  useEffect(() => {
    if (!enabled) {
      onStatusChangeRef.current?.('idle')
      return
    }

    let disposed = false
    let retryTimer: number | null = null
    let reconnectAttempt = 0
    const connectedGenerationKey = generationKey

    const connect = () => {
      if (disposed || generationKeyRef.current !== connectedGenerationKey) {
        return
      }

      onStatusChangeRef.current?.(reconnectAttempt > 0 ? 'reconnecting' : 'connecting')
      const ws = createTaskEventStream()
      wsRef.current = ws

      ws.onmessage = (event) => {
        if (
          disposed
          || wsRef.current !== ws
          || generationKeyRef.current !== connectedGenerationKey
        ) {
          return
        }
        try {
          const message = JSON.parse(event.data) as TaskEventMessage
          if (message.type === 'ready') {
            reconnectAttempt = 0
            onStatusChangeRef.current?.('live')
            onInvalidateRef.current()
          } else if (message.type === 'changed') {
            onInvalidateRef.current()
          }
        } catch { /* Ignore malformed task event messages. */ }
      }

      ws.onerror = () => ws.close()
      ws.onclose = () => {
        if (
          disposed
          || wsRef.current !== ws
          || generationKeyRef.current !== connectedGenerationKey
        ) {
          return
        }
        wsRef.current = null
        reconnectAttempt += 1
        onStatusChangeRef.current?.('reconnecting')
        const retryDelay = Math.min(
          TASK_EVENT_RECONNECT_MAX_MS,
          TASK_EVENT_RECONNECT_BASE_MS * (2 ** Math.min(reconnectAttempt - 1, 5)),
        )
        retryTimer = window.setTimeout(connect, retryDelay)
      }
    }

    connect()

    return () => {
      disposed = true
      if (retryTimer !== null) {
        window.clearTimeout(retryTimer)
      }
      const ws = wsRef.current
      if (ws) {
        ws.onopen = null
        ws.onmessage = null
        ws.onerror = null
        ws.onclose = null
        wsRef.current = null
        ws.close()
      }
    }
  }, [enabled, generationKey])
}

export function useLogStream({
  taskName,
  onChunk,
  onDisconnect,
  onStatusChange,
  enabled = true,
  logFileName,
  offset,
  logIdentity,
  generationKey = '',
}: UseLogStreamOptions) {
  const wsRef = useRef<WebSocket | null>(null)
  const onChunkRef = useRef(onChunk)
  const onDisconnectRef = useRef(onDisconnect)
  const onStatusChangeRef = useRef(onStatusChange)
  const offsetRef = useRef(offset)
  const logIdentityRef = useRef(logIdentity)
  const generationKeyRef = useRef(generationKey)
  onChunkRef.current = onChunk
  onDisconnectRef.current = onDisconnect
  onStatusChangeRef.current = onStatusChange
  offsetRef.current = offset
  logIdentityRef.current = logIdentity
  generationKeyRef.current = generationKey

  const disconnect = useCallback(() => {
    const ws = wsRef.current
    if (ws) {
      ws.onopen = null
      ws.onmessage = null
      ws.onerror = null
      ws.onclose = null
      wsRef.current = null
      ws.close()
    }
  }, [])

  useEffect(() => {
    if (!taskName || !enabled) {
      disconnect()
      onStatusChangeRef.current?.('idle')
      return
    }

    let disposed = false
    let retryTimer: number | null = null
    let reconnectAttempt = 0
    const connectedGenerationKey = generationKey

    const connect = () => {
      if (disposed || generationKeyRef.current !== connectedGenerationKey) {
        return
      }

      onStatusChangeRef.current?.(reconnectAttempt > 0 ? 'reconnecting' : 'connecting')
      const ws = createLogStream(taskName, {
        logFileName,
        offset: offsetRef.current,
        logIdentity: logIdentityRef.current,
      })
      wsRef.current = ws

      ws.onopen = () => {
        if (
          disposed
          || wsRef.current !== ws
          || generationKeyRef.current !== connectedGenerationKey
        ) {
          return
        }
        reconnectAttempt = 0
        onStatusChangeRef.current?.('live')
      }

      ws.onmessage = (ev) => {
        if (
          disposed
          || wsRef.current !== ws
          || generationKeyRef.current !== connectedGenerationKey
        ) {
          return
        }
        try {
          const msg = JSON.parse(ev.data) as LogStreamMessage
          if ((msg.type === 'chunk' && msg.content) || msg.type === 'reset') {
            onChunkRef.current(msg)
          }
        } catch { /* Ignore malformed third-party log messages. */ }
      }

      ws.onerror = () => ws.close()
      ws.onclose = () => {
        if (
          disposed
          || wsRef.current !== ws
          || generationKeyRef.current !== connectedGenerationKey
        ) {
          return
        }
        wsRef.current = null
        onDisconnectRef.current?.()
        reconnectAttempt += 1
        onStatusChangeRef.current?.('reconnecting')
        const retryDelay = Math.min(
          LOG_STREAM_RECONNECT_MAX_MS,
          LOG_STREAM_RECONNECT_BASE_MS * (2 ** Math.min(reconnectAttempt - 1, 4)),
        )
        retryTimer = window.setTimeout(connect, retryDelay)
      }
    }

    connect()

    return () => {
      disposed = true
      if (retryTimer !== null) {
        window.clearTimeout(retryTimer)
      }
      const ws = wsRef.current
      if (ws) {
        ws.onopen = null
        ws.onmessage = null
        ws.onerror = null
        ws.onclose = null
        wsRef.current = null
        ws.close()
      }
    }
  }, [taskName, enabled, disconnect, generationKey, logFileName])

  return { disconnect }
}
