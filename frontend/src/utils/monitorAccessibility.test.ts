import { describe, expect, it, vi } from 'vitest'

import {
  configureReadOnlyTerminalInput,
  READ_ONLY_TERMINAL_LABEL,
} from './monitorAccessibility'

describe('monitor terminal accessibility', () => {
  it('marks the xterm input as explicitly read-only and labels it as log output', () => {
    const attributes = new Map<string, string>()
    const input = {
      readOnly: false,
      setAttribute: vi.fn((name: string, value: string) => attributes.set(name, value)),
    }

    configureReadOnlyTerminalInput(input)

    expect(input.readOnly).toBe(true)
    expect(attributes.get('aria-readonly')).toBe('true')
    expect(attributes.get('aria-label')).toBe(READ_ONLY_TERMINAL_LABEL)
  })

  it('accepts a terminal that has not mounted its input yet', () => {
    expect(() => configureReadOnlyTerminalInput(undefined)).not.toThrow()
  })
})
