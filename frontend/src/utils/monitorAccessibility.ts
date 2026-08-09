export const READ_ONLY_TERMINAL_LABEL = 'Read-only task log output'

type TerminalInput = Pick<HTMLTextAreaElement, 'readOnly' | 'setAttribute'>

export function configureReadOnlyTerminalInput(input: TerminalInput | null | undefined) {
  if (!input) return

  input.readOnly = true
  input.setAttribute('aria-readonly', 'true')
  input.setAttribute('aria-label', READ_ONLY_TERMINAL_LABEL)
}
