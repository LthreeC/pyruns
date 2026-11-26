// Cancel a partial escape sequence before resetting the terminal in write order.
const TERMINAL_RESET = '\x18\x1bc'
const WRITE_CHARS = 32 * 1024
const MAX_PROGRESS_COLUMNS = 500
const NON_PROGRESS_CHARACTER = /[^\r\x20-\x7e\u2580-\u259f]/
const UNSUPPORTED_CONTROLS = /[\x00-\x09\x0b\x0c\x0e-\x1f\x7f-\x9f]/

/**
 * Retain the visible state of plain CR redraws in a history preview.
 * A frame may wrap at the Monitor's width even though it fit on the original
 * terminal. Show the latest redraw once instead of retaining those extra wraps.
 */
export function compactTerminalHistory(text: string, columns = 80) {
  if (!/\r[^\n]/.test(text)) return text
  // Colors can span lines. Other controls can move the cursor or change modes.
  if (UNSUPPORTED_CONTROLS.test(text.replace(/\x1b\[[0-9:;]*m/g, ''))) return text

  return text.split('\n').map(line => {
    const firstReturn = line.indexOf('\r')
    if (firstReturn < 0) return line
    if (firstReturn === line.lastIndexOf('\r')) return line
    if (firstReturn > MAX_PROGRESS_COLUMNS || NON_PROGRESS_CHARACTER.test(line)) return line

    // A single terminal row can contain hundreds of thousands of redraws.
    // Check their widths in place and allocate only the visible suffix.
    let widest = firstReturn
    let narrowestRedraw = Infinity
    let keepFrom = 0
    for (let start = firstReturn + 1;;) {
      const next = line.indexOf('\r', start)
      const width = (next === -1 ? line.length : next) - start
      if (next !== -1) {
        if (width > MAX_PROGRESS_COLUMNS) return line
        narrowestRedraw = Math.min(narrowestRedraw, width)
      } else if (widest > columns && narrowestRedraw !== widest) {
        // Variable-width writes may have wrapped and left earlier rows on
        // screen. Keep the existing narrow-screen fold for fixed-width bars.
        return line
      }
      // A redraw covering all earlier cells supersedes them. Keep subsequent
      // shorter writes verbatim for their residual cells and final cursor.
      if (width > 0 && width >= widest) {
        keepFrom = start
        widest = width
      }
      if (next === -1) return line.slice(keepFrom)
      start = next + 1
    }
  }).join('\n')
}

export function createTerminalLogWriter(write: (text: string, done: () => void) => void) {
  const queue: { text: string; offset: number }[] = []
  const idleCallbacks = new Set<() => void>()
  let queuedChars = 0
  let writing = false
  let disposed = false
  let lastWriteWasReset = false

  function drain() {
    if (writing || disposed) return
    const next = queue[0]
    if (!next) {
      for (const callback of idleCallbacks) {
        idleCallbacks.delete(callback)
        callback()
      }
      return
    }
    const chunk = next.text.slice(next.offset, next.offset + WRITE_CHARS)
    next.offset += chunk.length
    queuedChars -= chunk.length
    if (next.offset === next.text.length) queue.shift()
    writing = true
    lastWriteWasReset = chunk === TERMINAL_RESET
    write(chunk, () => {
      writing = false
      drain()
    })
  }

  function append(text: string) {
    if (!text || disposed) return
    queue.push({ text, offset: 0 })
    queuedChars += text.length
    drain()
  }

  return {
    get queuedChars() { return queuedChars },
    get busy() { return writing || queue.length > 0 },
    append,
    replace(text: string) {
      // One chunk may already be in xterm. Nothing else from the old log should
      // remain ahead of the replacement, even after expanding large histories.
      queue.length = 0
      queuedChars = 0
      idleCallbacks.clear()
      append(lastWriteWasReset ? text : TERMINAL_RESET + text)
    },
    whenIdle(callback: () => void) {
      idleCallbacks.add(callback)
      drain()
      return () => { idleCallbacks.delete(callback) }
    },
    dispose() {
      disposed = true
      queue.length = 0
      queuedChars = 0
      idleCallbacks.clear()
    },
  }
}
