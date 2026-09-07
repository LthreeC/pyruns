import { describe, expect, it } from 'vitest'
import { formatElapsedDuration, formatStoredCommand, parseTaskTimestampMillis } from './taskRuntime'

describe('task runtime formatting', () => {
  it('parses pyruns local timestamps and epoch seconds', () => {
    expect(parseTaskTimestampMillis('2026-09-07_12-34-56')).toBe(
      new Date(2026, 8, 7, 12, 34, 56).getTime(),
    )
    expect(parseTaskTimestampMillis(1_800_000_000)).toBe(1_800_000_000_000)
  })

  it('rejects empty and invalid timestamps', () => {
    expect(parseTaskTimestampMillis('')).toBeNull()
    expect(parseTaskTimestampMillis('2026-02-31_12-00-00')).toBeNull()
  })

  it('formats elapsed time without changing width each second', () => {
    expect(formatElapsedDuration(0)).toBe('00:00:00')
    expect(formatElapsedDuration(3_661.9)).toBe('01:01:01')
    expect(formatElapsedDuration(90_061)).toBe('1d 01:01:01')
  })

  it('keeps configured argv boundaries visible', () => {
    expect(formatStoredCommand(['python', 'script with spaces.py'])).toBe(
      '[\n  "python",\n  "script with spaces.py"\n]',
    )
    expect(formatStoredCommand('  echo ok  ')).toBe('echo ok')
  })
})
