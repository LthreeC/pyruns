import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react'
import CodeMirror from '@uiw/react-codemirror'
import { yaml as yamlLanguage } from '@codemirror/lang-yaml'
import { HighlightStyle, StreamLanguage, syntaxHighlighting } from '@codemirror/language'
import type { Extension } from '@codemirror/state'
import { EditorView, keymap, placeholder as editorPlaceholder, type Command } from '@codemirror/view'
import { oneDark } from '@codemirror/theme-one-dark'
import { tags as t } from '@lezer/highlight'
import { Keyboard, WrapText } from 'lucide-react'
import clsx from 'clsx'
import CopyButton from '@/components/shared/CopyButton'
import { useTranslation } from '@/i18n'

type CodeEditorLanguage = 'yaml' | 'shell'
type CodeEditorTheme = 'light' | 'dark'

interface CodeTextEditorProps {
  value: string
  onChange: (value: string) => void
  language: CodeEditorLanguage
  shellKind?: string
  theme: CodeEditorTheme
  className?: string
  wrapStorageKey?: string
  defaultWrap?: boolean
  placeholder?: string
  compactToolbar?: boolean
  ariaLabel?: string
}

const YAML_EXTENSION = yamlLanguage()

const LIGHT_EDITOR_THEME = EditorView.theme({
  '&': {
    color: '#1f2937',
    backgroundColor: 'transparent',
  },
  '.cm-content': {
    caretColor: '#2563eb',
  },
  '.cm-cursor, .cm-dropCursor': {
    borderLeftColor: '#2563eb',
  },
  '.cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection': {
    backgroundColor: 'rgba(37, 99, 235, 0.18)',
  },
  '.cm-activeLine': {
    backgroundColor: 'rgba(15, 23, 42, 0.04)',
  },
  '.cm-gutters': {
    color: '#94a3b8',
    backgroundColor: 'rgba(248, 250, 252, 0.92)',
    borderRight: '1px solid rgba(203, 213, 225, 0.7)',
  },
  '.cm-activeLineGutter': {
    backgroundColor: 'rgba(226, 232, 240, 0.65)',
    color: '#64748b',
  },
})

const DARK_EDITOR_THEME = [
  oneDark,
  EditorView.theme({
    '&': {
      backgroundColor: 'transparent',
    },
    '.cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection': {
      backgroundColor: 'rgba(38, 79, 120, 0.55)',
    },
    '.cm-activeLine': {
      backgroundColor: 'rgba(255, 255, 255, 0.04)',
    },
    '.cm-gutters': {
      backgroundColor: 'rgba(15, 23, 42, 0.78)',
      borderRight: '1px solid rgba(51, 65, 85, 0.95)',
    },
    '.cm-activeLineGutter': {
      backgroundColor: 'rgba(30, 41, 59, 0.82)',
    },
  }),
]

const CMD_LINE_COMMENT = /^@?rem(?:[ \t]|$)/i

function shellLanguage(cmd: boolean) {
  return StreamLanguage.define({
    languageData: cmd ? {} : { commentTokens: { line: '#' } },
    startState: () => ({ inString: null as '"' | "'" | null }),
    token(stream, state) {
      if (stream.sol()) {
        state.inString = null
        if (cmd && CMD_LINE_COMMENT.test(stream.string.trimStart())) {
          stream.skipToEnd()
          return 'comment'
        }
      }
      if (stream.eatSpace()) {
        return null
      }
      if (state.inString) {
        let escaped = false
        while (!stream.eol()) {
          const next = stream.next()
          if (escaped) {
            escaped = false
            continue
          }
          if (next === '\\') {
            escaped = true
            continue
          }
          if (next === state.inString) {
            state.inString = null
            break
          }
        }
        return 'string'
      }
      if (!cmd && stream.peek() === '#') {
        stream.skipToEnd()
        return 'comment'
      }
      const quote = stream.peek()
      if (quote === '"' || quote === "'") {
        state.inString = quote
        stream.next()
        return 'string'
      }
      if (stream.match(/^\$\{[^}]+\}/) || stream.match(/^\$[A-Za-z_][\w]*/)) {
        return 'variableName'
      }
      if (stream.match(/^--?[A-Za-z][\w-]*/)) {
        return 'attributeName'
      }
      if (stream.match(/^(?:&&|\|\||<<|>>|[|&;<>])/)) {
        return 'operator'
      }
      if (stream.match(/^(?:\d+\.\d+|\d+)/)) {
        return 'number'
      }
      if (stream.match(/^(?:if|then|elif|else|fi|for|while|until|do|done|case|esac|function|in)\b/)) {
        return 'keyword'
      }
      if (stream.match(/^(?:alias|cd|conda|echo|eval|export|git|ls|mamba|mkdir|module|pip|python|pwd|set|source|test|touch|unset|uv)\b/)) {
        return 'builtin'
      }
      if (stream.match(/^[A-Za-z_][\w-]*(?==)/)) {
        return 'variableName.definition'
      }
      stream.next()
      return null
    },
  })
}

// CMD recognizes REM case-insensitively and only as a complete command.
const toggleCmdLineComment: Command = view => {
  const { state } = view
  if (state.readOnly) return false

  const lines = new Map<number, { from: number; commentLength: number }>()
  for (const range of state.selection.ranges) {
    const first = state.doc.lineAt(range.from).number
    const last = state.doc.lineAt(range.empty ? range.to : range.to - 1).number
    for (let n = first; n <= last; n++) {
      const line = state.doc.line(n)
      const content = line.text.trimStart()
      if (!content && first !== last) continue
      lines.set(n, {
        from: line.to - content.length,
        commentLength: content.match(CMD_LINE_COMMENT)?.[0].length ?? 0,
      })
    }
  }
  if (!lines.size) return false

  const selected = Array.from(lines.values())
  const uncomment = selected.every(line => line.commentLength > 0)
  const changes = state.changes(selected.map(({ from, commentLength }) => (
    uncomment ? { from, to: from + commentLength } : { from, insert: 'REM ' }
  )))
  view.dispatch({ changes, selection: state.selection.map(changes, 1), userEvent: 'input.comment' })
  return true
}

const SHELL_LANGUAGE = shellLanguage(false)
const CMD_EXTENSIONS = [
  shellLanguage(true),
  keymap.of([{ key: 'Mod-/', run: toggleCmdLineComment, preventDefault: true }]),
]

const LIGHT_EDITOR_HIGHLIGHT = syntaxHighlighting(HighlightStyle.define([
  { tag: [t.keyword, t.controlKeyword], color: '#0000ff' },
  { tag: [t.name, t.variableName], color: '#001080' },
  { tag: [t.propertyName, t.attributeName, t.definition(t.variableName)], color: '#795e26' },
  { tag: [t.number, t.integer, t.float], color: '#098658' },
  { tag: [t.bool, t.null], color: '#0000ff' },
  { tag: [t.string, t.special(t.string)], color: '#a31515' },
  { tag: [t.comment, t.lineComment], color: '#008000', fontStyle: 'italic' },
  { tag: [t.operator, t.separator], color: '#111827' },
  { tag: [t.brace, t.squareBracket, t.paren], color: '#111827' },
]), { fallback: false })

const DARK_EDITOR_HIGHLIGHT = syntaxHighlighting(HighlightStyle.define([
  { tag: [t.keyword, t.controlKeyword], color: '#569cd6' },
  { tag: [t.name, t.variableName], color: '#9cdcfe' },
  { tag: [t.propertyName, t.attributeName, t.definition(t.variableName)], color: '#dcdcaa' },
  { tag: [t.number, t.integer, t.float], color: '#b5cea8' },
  { tag: [t.bool, t.null], color: '#569cd6' },
  { tag: [t.string, t.special(t.string)], color: '#ce9178' },
  { tag: [t.comment, t.lineComment], color: '#6a9955', fontStyle: 'italic' },
  { tag: [t.operator, t.separator], color: '#d4d4d4' },
  { tag: [t.brace, t.squareBracket, t.paren], color: '#d4d4d4' },
]), { fallback: false })

function getStoredWrap(storageKey: string | undefined, fallback: boolean) {
  if (!storageKey || typeof window === 'undefined') {
    return fallback
  }
  const stored = window.localStorage.getItem(storageKey)
  if (stored === '0') return false
  if (stored === '1') return true
  return fallback
}

function buildEditorExtensions(
  language: CodeEditorLanguage,
  shellKind: string | undefined,
  theme: CodeEditorTheme,
  wrap: boolean,
  placeholderText?: string,
  ariaLabel?: string,
): Extension[] {
  const baseTheme = theme === 'dark' ? DARK_EDITOR_THEME : [LIGHT_EDITOR_THEME]
  const highlighting = theme === 'dark' ? DARK_EDITOR_HIGHLIGHT : LIGHT_EDITOR_HIGHLIGHT
  const languageExtension = language === 'yaml' ? YAML_EXTENSION : shellKind === 'cmd' ? CMD_EXTENSIONS : SHELL_LANGUAGE
  const extras: Extension[] = []
  if (placeholderText) {
    extras.push(editorPlaceholder(placeholderText))
  }
  if (ariaLabel) {
    extras.push(EditorView.contentAttributes.of({ 'aria-label': ariaLabel }))
  }
  return wrap
    ? [...baseTheme, languageExtension, highlighting, EditorView.lineWrapping, ...extras]
    : [...baseTheme, languageExtension, highlighting, ...extras]
}

function ShellEditorShortcuts() {
  const { t: translate } = useTranslation()
  const apple = /Mac|iPhone|iPad|iPod/.test(navigator.platform)
  const mod = apple ? '⌘' : 'Ctrl'
  const alt = apple ? '⌥' : 'Alt'
  const shortcuts = [
    ['Toggle line comment', `${mod} + /`],
    ['Find and replace', `${mod} + F`],
    ['Move lines', `${alt} + ↑ / ↓`],
    ['Duplicate lines', `${alt} + Shift + ↑ / ↓`],
    ['Delete lines', `${mod} + Shift + K`],
    ['Indent / outdent', 'Tab / Shift + Tab'],
    ['Undo / redo', apple ? '⌘ + Z / ⇧⌘ + Z' : 'Ctrl + Z / Ctrl + Y'],
    ['Leave the editor', 'Esc, Tab'],
  ]

  return (
    <details
      className="group/shortcuts"
      onBlur={event => {
        if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget)) {
          event.currentTarget.open = false
        }
      }}
      onKeyDown={event => {
        if (event.key === 'Escape' && event.currentTarget.open) {
          event.currentTarget.open = false
          event.stopPropagation()
        }
      }}
    >
      <summary
        aria-label={translate('Shell editor shortcuts')}
        title={translate('Shell editor shortcuts')}
        className="touch-target flex size-6 cursor-pointer list-none items-center justify-center rounded-md text-txt-secondary hover:bg-surface-overlay group-open/shortcuts:bg-surface-overlay group-open/shortcuts:text-accent-ink"
      >
        <Keyboard aria-hidden="true" className="size-4" />
      </summary>
      <dl className="absolute right-2 top-full z-20 max-h-[42dvh] w-[19rem] max-w-[calc(100%_-_1rem)] divide-y divide-border-subtle overflow-y-auto border border-border bg-surface-raised px-2.5 text-xs shadow-md">
        {shortcuts.map(([label, keys]) => (
          <div key={label} className="flex flex-wrap items-baseline justify-between gap-x-2 py-1.5">
            <dt className="text-txt-primary">{translate(label)}</dt>
            <dd><kbd className="whitespace-nowrap font-mono text-2xs text-txt-secondary">{keys}</kbd></dd>
          </div>
        ))}
      </dl>
    </details>
  )
}

export default function CodeTextEditor({
  value,
  onChange,
  language,
  shellKind,
  theme,
  className,
  wrapStorageKey,
  defaultWrap = true,
  placeholder,
  compactToolbar = false,
  ariaLabel,
}: CodeTextEditorProps) {
  const { t: translate } = useTranslation()
  const [wrap, setWrap] = useState(() => getStoredWrap(wrapStorageKey, defaultWrap))
  const editorViewRef = useRef<EditorView | null>(null)
  const extensions = useMemo(
    () => buildEditorExtensions(language, shellKind, theme, wrap, placeholder, ariaLabel),
    [ariaLabel, language, placeholder, shellKind, theme, wrap],
  )

  useEffect(() => {
    if (wrapStorageKey && typeof window !== 'undefined') {
      window.localStorage.setItem(wrapStorageKey, wrap ? '1' : '0')
    }
  }, [wrap, wrapStorageKey])

  const focusEditorFromBlankArea = useCallback((event: ReactMouseEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement | null
    if (!target || target.closest('.cm-content') || target.closest('.cm-gutters') || target.closest('button')) {
      return
    }

    const view = editorViewRef.current
    if (!view) {
      return
    }

    event.preventDefault()
    view.dispatch({ selection: { anchor: view.state.doc.length }, scrollIntoView: true })
    view.focus()
  }, [])

  return (
    <div
      className={clsx(
        'code-text-editor group relative flex flex-col rounded-md border border-border-subtle bg-surface-raised transition-colors focus-within:border-accent/50 focus-within:ring-1 focus-within:ring-accent/10',
        className,
      )}
    >
      {!compactToolbar && (
        <div className="relative flex min-h-8 flex-none items-center gap-2 border-b border-border-subtle bg-surface-overlay/25 px-2.5">
          <span className="mr-auto rounded-md bg-surface-raised px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-txt-tertiary">
            {language === 'yaml' ? 'YAML' : 'Shell'}
          </span>
          {language === 'shell' && <ShellEditorShortcuts />}
          <CopyButton
            value={value}
            label={translate('Copy {{language}} content', { language: language === 'yaml' ? 'YAML' : 'shell' })}
            size="xs"
            onMouseDown={event => event.preventDefault()}
          />
          <button
            type="button"
            onMouseDown={event => event.preventDefault()}
            onClick={() => setWrap(current => !current)}
            aria-pressed={wrap}
            aria-label={translate(wrap ? 'Disable line wrapping' : 'Enable line wrapping')}
            title={translate(wrap ? 'Disable line wrapping' : 'Enable line wrapping')}
            className={clsx(
              'touch-target inline-flex h-6 w-6 items-center justify-center rounded-md transition-colors focus:outline-none focus:ring-2 focus:ring-accent/25',
              wrap
                ? 'bg-accent/10 text-accent'
                : 'text-txt-tertiary hover:bg-surface-raised hover:text-txt-primary',
            )}
          >
            <WrapText className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
      {compactToolbar && (
        <div className="absolute right-1.5 top-1.5 z-10 flex gap-1.5">
          <CopyButton
            value={value}
            label={translate('Copy {{language}} content', { language: language === 'yaml' ? 'YAML' : 'shell' })}
            size="xs"
            onMouseDown={event => event.preventDefault()}
            className="border border-border-subtle bg-surface-raised/95 shadow-sm"
          />
          <button
            type="button"
            onMouseDown={event => event.preventDefault()}
            onClick={() => setWrap(current => !current)}
            aria-pressed={wrap}
            aria-label={translate(wrap ? 'Disable line wrapping' : 'Enable line wrapping')}
            title={translate(wrap ? 'Disable line wrapping' : 'Enable line wrapping')}
            className={clsx(
              'touch-target inline-flex h-6 w-6 items-center justify-center rounded-md border border-border-subtle bg-surface-raised/95 text-txt-tertiary shadow-sm transition-colors focus:outline-none focus:ring-2 focus:ring-accent/25',
              wrap ? 'text-accent' : 'hover:bg-surface-overlay hover:text-txt-primary',
            )}
          >
            <WrapText className="h-3.5 w-3.5" />
          </button>
        </div>
      )}
      <div className="relative min-h-0 flex-1 cursor-text overflow-hidden" onMouseDown={focusEditorFromBlankArea}>
        <CodeMirror
          value={value}
          height="100%"
          className="absolute inset-0 overflow-hidden"
          theme={theme}
          extensions={extensions}
          onCreateEditor={view => { editorViewRef.current = view }}
          onChange={nextValue => onChange(nextValue)}
        />
      </div>
    </div>
  )
}
