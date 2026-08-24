export const TERMINAL_SEARCH_DEBOUNCE_MS = 150
export const TERMINAL_SEARCH_HIGHLIGHT_LIMIT = 250
export const TERMINAL_SEARCH_DECORATION_ROW_LIMIT = 10_000

export function shouldDecorateTerminalSearch(bufferRows: number) {
  return Number.isFinite(bufferRows) && bufferRows <= TERMINAL_SEARCH_DECORATION_ROW_LIMIT
}

export function splitTaskSearchSnippet(
  snippet: string,
  matchStart: number,
  matchEnd: number,
): [string, string, string] {
  const characters = Array.from(snippet)
  const start = Math.max(0, Math.min(characters.length, Number(matchStart) || 0))
  const end = Math.max(start, Math.min(characters.length, Number(matchEnd) || 0))
  return [
    characters.slice(0, start).join(''),
    characters.slice(start, end).join(''),
    characters.slice(end).join(''),
  ]
}

export function terminalSearchResultLabel({
  found,
  decorated,
  resultIndex = -1,
  resultCount = 0,
}: {
  found: boolean
  decorated: boolean
  resultIndex?: number
  resultCount?: number
}) {
  if (!found) {
    return 'No match'
  }
  if (!decorated || resultCount <= 0) {
    return 'Match'
  }

  const cappedCount = resultCount >= TERMINAL_SEARCH_HIGHLIGHT_LIMIT
    ? `${resultCount}+`
    : String(resultCount)
  return resultIndex >= 0 ? `${resultIndex + 1}/${cappedCount}` : cappedCount
}
