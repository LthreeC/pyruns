import { describe, expect, it, vi } from 'vitest'
import { compactTerminalHistory, createTerminalLogWriter } from './monitorTerminal'

describe('compactTerminalHistory', () => {
  it('folds fixed-width progress redraws while preserving lines and colors', () => {
    const header = '\x1b[32mStart\x1b[0m\r\n'
    const progress = '\r[█  ] 25%\r[██ ] 50%\r[███] 99%'
    const footer = '\r\nFinished\n'
    expect(compactTerminalHistory(header + progress + footer)).toBe(header + '[███] 99%' + footer)
  })

  it('keeps a partial final redraw and a carry-over from the previous full frame', () => {
    expect(compactTerminalHistory('cut\rframe01\rframe02\rfra')).toBe('frame02\rfra')
    expect(compactTerminalHistory('cut\rframe01\rframe02\r')).toBe('frame02\r')
  })

  it.each([
    'ordinary\r\nWindows text\r\n',
    'longer text\rshort\rfinal',
    '\x1b[1A\n\rframe01\rframe02',
    '\r\x1b[31mframe01\r\x1b[32mframe02\x1b[0m',
    '\r一二三\r四五六\r七八九',
  ])('preserves output whose cursor or cell widths cannot be inferred: %j', text => {
    expect(compactTerminalHistory(text)).toBe(text)
  })
})

describe('createTerminalLogWriter', () => {
  it('drops all unsent text from the previous log and waits for the replacement to finish', () => {
    const writes: { text: string; done: () => void }[] = []
    const writer = createTerminalLogWriter((text, done) => writes.push({ text, done }))
    const abandoned = vi.fn()
    const ready = vi.fn()

    writer.replace('old'.repeat(50_000))
    writer.append('queued live text')
    writer.whenIdle(abandoned)
    expect(writes).toHaveLength(1)
    expect(writer.queuedChars).toBeGreaterThan(32 * 1024)

    writer.replace('selected log')
    writer.append(' + live text')
    writer.whenIdle(ready)
    writes[0].done()
    expect(writes[1].text).toBe('\x18\x1bcselected log')
    writes[1].done()
    expect(writes[2].text).toBe(' + live text')
    expect(ready).not.toHaveBeenCalled()
    writes[2].done()

    expect(writes).toHaveLength(3)
    expect(ready).toHaveBeenCalledOnce()
    expect(abandoned).not.toHaveBeenCalled()
    expect(writer.queuedChars).toBe(0)
  })

  it('releases queued output and callbacks when the terminal unmounts', () => {
    const writes: (() => void)[] = []
    const writer = createTerminalLogWriter((_text, done) => writes.push(done))
    const ready = vi.fn()
    writer.append('first')
    writer.append('pending')
    writer.whenIdle(ready)
    writer.dispose()
    writes[0]()
    expect(writes).toHaveLength(1)
    expect(ready).not.toHaveBeenCalled()
  })
})
