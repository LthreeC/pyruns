export const DEFAULT_MONITOR_CHUNK_SIZE = 50000
export const DEFAULT_MONITOR_SCROLLBACK = 100000
export const DEFAULT_MONITOR_LINE_HEIGHT = 1

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
  return resolveIntegerSetting(settings, 'monitor_chunk_size', DEFAULT_MONITOR_CHUNK_SIZE, 1, 50_000_000)
}

export function resolveMonitorScrollback(settings: Record<string, any> | null | undefined) {
  return resolveIntegerSetting(settings, 'monitor_scrollback', DEFAULT_MONITOR_SCROLLBACK, 0, 1_000_000)
}

export function resolveMonitorLineHeight(settings: Record<string, any> | null | undefined) {
  return resolveNumberSetting(settings, 'monitor_line_height', DEFAULT_MONITOR_LINE_HEIGHT, 1, 2.5)
}
