import { describe, expect, it } from 'vitest'
import {
  DEFAULT_MONITOR_CHUNK_SIZE,
  DEFAULT_MONITOR_LINE_HEIGHT,
  DEFAULT_MONITOR_SCROLLBACK,
  MAX_MONITOR_CHUNK_SIZE,
  MAX_MONITOR_LINE_HEIGHT,
  MAX_MONITOR_SCROLLBACK,
  resolveMonitorChunkSize,
  resolveMonitorLineHeight,
  resolveMonitorScrollback,
} from './monitorSettings'

describe('monitor settings', () => {
  it('uses safe defaults for absent and non-finite values', () => {
    expect(resolveMonitorChunkSize(undefined)).toBe(DEFAULT_MONITOR_CHUNK_SIZE)
    expect(resolveMonitorScrollback({ monitor_scrollback: Number.NaN })).toBe(DEFAULT_MONITOR_SCROLLBACK)
    expect(resolveMonitorLineHeight({ monitor_line_height: Number.POSITIVE_INFINITY })).toBe(DEFAULT_MONITOR_LINE_HEIGHT)
  })

  it('clamps resource-sensitive settings to their supported bounds', () => {
    expect(resolveMonitorChunkSize({ monitor_chunk_size: 0 })).toBe(1)
    expect(resolveMonitorChunkSize({ monitor_chunk_size: 1_000_000_000 })).toBe(MAX_MONITOR_CHUNK_SIZE)
    expect(resolveMonitorScrollback({ monitor_scrollback: -1 })).toBe(0)
    expect(resolveMonitorScrollback({ monitor_scrollback: 2_000_000 })).toBe(MAX_MONITOR_SCROLLBACK)
    expect(resolveMonitorLineHeight({ monitor_line_height: 0.1 })).toBe(1)
    expect(resolveMonitorLineHeight({ monitor_line_height: 9 })).toBe(MAX_MONITOR_LINE_HEIGHT)
  })
})
