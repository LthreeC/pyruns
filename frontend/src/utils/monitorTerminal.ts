// Cancel a partial escape sequence before resetting the terminal in write order.
const TERMINAL_RESET = '\x18\x1bc'
const WRITE_CHARS = 32 * 1024
const MAX_PROGRESS_COLUMNS = 500
const PLAIN_FRAME = /^[\x20-\x7e\u2580-\u259f]*$/
const UNSUPPORTED_CONTROLS = /[\x00-\x09\x0b\x0c\x0e-\x1f\x7f-\x9f]/

/**
 * Retain the visible state of plain, fixed-width CR redraws in a history preview.
 * A frame may wrap at the Monitor's width even though it fit on the original
 * terminal. Show the latest redraw once instead of retaining those extra wraps.
 */
export function compactTerminalHistory(text: string) {
  if (!/\r[^\n]/.test(text)) return text
  // Colors can span lines. Other controls can move the cursor or change modes.
  if (UNSUPPORTED_CONTROLS.test(text.replace(/\x1b\[[0-9:;]*m/g, ''))) return text

  return text.split('\n').map(line => {
    const firstReturn = line.indexOf('\r')
    if (firstReturn < 0 || firstReturn === line.lastIndexOf('\r')) return line

    const frames = line.split('\r')
    const latest = frames[frames.length - 1]
    const previous = frames[frames.length - 2]
    const width = previous.length
    if (width === 0 || width > MAX_PROGRESS_COLUMNS || frames[0].length > width) return line
    if (!frames.every((frame, index) => (
      (index === 0 || index === frames.length - 1 || frame.length === width) && PLAIN_FRAME.test(frame)
    ))) return line

    // The file may end in the middle of a redraw. Preserve the previous full
    // frame and the partial write so a later live chunk resumes at its column.
    return latest.length >= width ? latest : previous + '\r' + latest
  }).join('\n')
}

export function createTerminalLogWriter(write: (text: string, done: () => void) => void) {
  const queue: { text: string; offset: number }[] = []
  const idleCallbacks = new Set<() => void>()
  let queuedChars = 0
  let writing = false
  let disposed = false

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
    append,
    replace(text: string) {
      // One chunk may already be in xterm. Nothing else from the old log should
      // remain ahead of the replacement, even after expanding large histories.
      queue.length = 0
      queuedChars = 0
      idleCallbacks.clear()
      append(TERMINAL_RESET + text)
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
