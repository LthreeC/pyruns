export async function copyText(value: string): Promise<boolean> {
  const text = String(value ?? '')
  if (!text) {
    return false
  }

  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // Fall through to the DOM fallback for insecure contexts and denied permissions.
    }
  }

  if (typeof document === 'undefined' || !document.body) {
    return false
  }

  const textarea = document.createElement('textarea')
  textarea.value = text
  textarea.setAttribute('readonly', '')
  textarea.setAttribute('aria-hidden', 'true')
  textarea.style.position = 'fixed'
  textarea.style.top = '0'
  textarea.style.left = '-9999px'
  textarea.style.opacity = '0'

  const activeElement = document.activeElement as HTMLElement | null
  document.body.appendChild(textarea)

  let copied = false
  try {
    textarea.select()
    textarea.setSelectionRange(0, text.length)
    copied = typeof document.execCommand === 'function' && document.execCommand('copy')
  } catch {
    copied = false
  } finally {
    if (typeof textarea.remove === 'function') {
      textarea.remove()
    } else {
      textarea.parentNode?.removeChild(textarea)
    }
    if (activeElement) {
      try {
        activeElement.focus({ preventScroll: true })
      } catch {
        activeElement.focus()
      }
    }
  }
  return copied
}
