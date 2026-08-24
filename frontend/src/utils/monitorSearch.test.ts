import { describe, expect, it } from 'vitest'

import {
  splitTaskSearchSnippet,
  shouldDecorateTerminalSearch,
  TERMINAL_SEARCH_DECORATION_ROW_LIMIT,
  TERMINAL_SEARCH_HIGHLIGHT_LIMIT,
  terminalSearchResultLabel,
} from './monitorSearch'

describe('monitor terminal search policy', () => {
  it('keeps all-match decorations only for bounded terminal buffers', () => {
    expect(shouldDecorateTerminalSearch(TERMINAL_SEARCH_DECORATION_ROW_LIMIT)).toBe(true)
    expect(shouldDecorateTerminalSearch(TERMINAL_SEARCH_DECORATION_ROW_LIMIT + 1)).toBe(false)
    expect(shouldDecorateTerminalSearch(Number.POSITIVE_INFINITY)).toBe(false)
  })

  it('reports active-only and decorated search results clearly', () => {
    expect(terminalSearchResultLabel({ found: false, decorated: false })).toBe('No match')
    expect(terminalSearchResultLabel({ found: true, decorated: false })).toBe('Match')
    expect(terminalSearchResultLabel({
      found: true,
      decorated: true,
      resultIndex: 2,
      resultCount: 8,
    })).toBe('3/8')
  })

  it('marks a highlight-limited result count as capped', () => {
    expect(terminalSearchResultLabel({
      found: true,
      decorated: true,
      resultIndex: 0,
      resultCount: TERMINAL_SEARCH_HIGHLIGHT_LIMIT,
    })).toBe(`1/${TERMINAL_SEARCH_HIGHLIGHT_LIMIT}+`)
    expect(terminalSearchResultLabel({
      found: true,
      decorated: true,
      resultCount: TERMINAL_SEARCH_HIGHLIGHT_LIMIT,
    })).toBe(`${TERMINAL_SEARCH_HIGHLIGHT_LIMIT}+`)
  })

  it('splits task search highlights by Unicode code point offsets', () => {
    expect(splitTaskSearchSnippet('😀 foo', 2, 5)).toEqual([
      '😀 ',
      'foo',
      '',
    ])
  })
})
