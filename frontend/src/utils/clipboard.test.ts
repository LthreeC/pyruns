import { afterEach, describe, expect, it, vi } from 'vitest'
import { copyText } from './clipboard'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('copyText', () => {
  it('rejects empty values without touching the clipboard', async () => {
    expect(await copyText('')).toBe(false)
  })

  it('uses the Clipboard API when available', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })

    expect(await copyText('hello')).toBe(true)
    expect(writeText).toHaveBeenCalledWith('hello')
  })

  it('falls back to execCommand when the Clipboard API is unavailable or rejects', async () => {
    const textarea = {
      value: '',
      style: {} as Record<string, string>,
      setAttribute: vi.fn(),
      select: vi.fn(),
      setSelectionRange: vi.fn(),
      remove: vi.fn(),
    }
    const activeElement = { focus: vi.fn() }
    const body = { appendChild: vi.fn() }
    const documentStub = {
      body,
      activeElement,
      createElement: vi.fn(() => textarea),
      execCommand: vi.fn(() => true),
    }
    vi.stubGlobal('navigator', { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('permission denied')) } })
    vi.stubGlobal('document', documentStub)

    expect(await copyText('fallback')).toBe(true)
    expect(textarea.value).toBe('fallback')
    expect(documentStub.execCommand).toHaveBeenCalledWith('copy')
    expect(textarea.remove).toHaveBeenCalled()
    expect(activeElement.focus).toHaveBeenCalledWith({ preventScroll: true })
  })
})
