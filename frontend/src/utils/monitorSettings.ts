export const DEFAULT_MONITOR_CHUNK_SIZE = 50000
export const DEFAULT_MONITOR_SCROLLBACK = 10_000
export const DEFAULT_MONITOR_LINE_HEIGHT = 1
export const MAX_MONITOR_CHUNK_SIZE = 4 * 1024 * 1024
export const MAX_MONITOR_LOG_CHARS = 4 * 1024 * 1024
export const MONITOR_INITIAL_TAIL_LINES = 1_000
export const MAX_MONITOR_SCROLLBACK = 1_000_000
export const MAX_MONITOR_LINE_HEIGHT = 2.5
// Keep a small redraw window even when xterm's history is disabled.
export const MIN_MONITOR_VISIBLE_LINES = 100

export function monitorLogTailLines(scrollback: number) {
  return scrollback === 0 ? MIN_MONITOR_VISIBLE_LINES : scrollback
}

export function monitorPreviewTailLines(scrollback: number, expanded = false) {
  const limit = monitorLogTailLines(scrollback)
  return expanded ? limit : Math.min(limit, MONITOR_INITIAL_TAIL_LINES)
}

function resolveIntegerSetting(
  settings: Record<string, any> | null | undefined,
  key: string,
  fallback: number,
  min: number,
  max: number,
) {
  const value = Number(settings?.[key])
  const normalized = Number.isFinite(value) ? Math.trunc(value) : fallback
  return Math.min(max, Math.max(min, normalized))
}

function resolveNumberSetting(
  settings: Record<string, any> | null | undefined,
  key: string,
  fallback: number,
  min: number,
  max: number,
) {
  const value = Number(settings?.[key])
  const normalized = Number.isFinite(value) ? value : fallback
  return Math.min(max, Math.max(min, normalized))
}

export function resolveMonitorChunkSize(settings: Record<string, any> | null | undefined) {
  return resolveIntegerSetting(settings, 'monitor_chunk_size', DEFAULT_MONITOR_CHUNK_SIZE, 1, MAX_MONITOR_CHUNK_SIZE)
}

export function resolveMonitorScrollback(settings: Record<string, any> | null | undefined) {
  return resolveIntegerSetting(settings, 'monitor_scrollback', DEFAULT_MONITOR_SCROLLBACK, 0, MAX_MONITOR_SCROLLBACK)
}

export function resolveMonitorLineHeight(settings: Record<string, any> | null | undefined) {
  return resolveNumberSetting(settings, 'monitor_line_height', DEFAULT_MONITOR_LINE_HEIGHT, 1, MAX_MONITOR_LINE_HEIGHT)
}
