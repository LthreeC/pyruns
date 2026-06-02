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
import { EditorView, placeholder as editorPlaceholder } from '@codemirror/view'
import { oneDark } from '@codemirror/theme-one-dark'
import { tags as t } from '@lezer/highlight'
import { WrapText } from 'lucide-react'
import clsx from 'clsx'

type CodeEditorLanguage = 'yaml' | 'shell'
type CodeEditorTheme = 'light' | 'dark'

interface CodeTextEditorProps {
  value: string
  onChange: (value: string) => void
  language: CodeEditorLanguage
  theme: CodeEditorTheme
  className?: string
  wrapStorageKey?: string
  defaultWrap?: boolean
  placeholder?: string
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

const SHELL_LANGUAGE = StreamLanguage.define({
  startState: () => ({ inString: null as '"' | "'" | null }),
  token(stream, state) {
    if (stream.sol()) {
      state.inString = null
    }
    if (stream.eatSpace()) {
      return null
    }
    if (stream.peek() === '#') {
      stream.skipToEnd()
      return 'comment'
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
      return 'definition'
    }
    stream.next()
    return null
  },
})

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
  theme: CodeEditorTheme,
  wrap: boolean,
  placeholderText?: string,
): Extension[] {
  const baseTheme = theme === 'dark' ? DARK_EDITOR_THEME : [LIGHT_EDITOR_THEME]
  const highlighting = theme === 'dark' ? DARK_EDITOR_HIGHLIGHT : LIGHT_EDITOR_HIGHLIGHT
  const languageExtension = language === 'yaml' ? YAML_EXTENSION : SHELL_LANGUAGE
  const extras = placeholderText ? [editorPlaceholder(placeholderText)] : []
  return wrap
    ? [...baseTheme, languageExtension, highlighting, EditorView.lineWrapping, ...extras]
    : [...baseTheme, languageExtension, highlighting, ...extras]
}

export default function CodeTextEditor({
  value,
  onChange,
  language,
  theme,
  className,
  wrapStorageKey,
  defaultWrap = true,
  placeholder,
}: CodeTextEditorProps) {
  const [wrap, setWrap] = useState(() => getStoredWrap(wrapStorageKey, defaultWrap))
  const editorViewRef = useRef<EditorView | null>(null)
  const lineCount = useMemo(() => Math.max(1, value.split(/\r\n|\r|\n/).length), [value])
  const extensions = useMemo(
    () => buildEditorExtensions(language, theme, wrap, placeholder),
    [language, placeholder, theme, wrap],
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
        'code-text-editor group flex flex-col rounded-md border border-border-subtle bg-surface-raised transition-colors focus-within:border-accent/70 focus-within:ring-2 focus-within:ring-accent/12',
        className,
      )}
    >
      <div className="flex h-8 flex-none items-center gap-2 border-b border-border-subtle bg-surface-overlay/35 px-2.5">
        <span className="rounded-md bg-surface-raised px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-txt-tertiary">
          {language === 'yaml' ? 'YAML' : 'Shell'}
        </span>
        <span className="font-mono text-[10px] text-txt-tertiary">
          {lineCount} line{lineCount > 1 ? 's' : ''}
        </span>
        <button
          type="button"
          onMouseDown={event => event.preventDefault()}
          onClick={() => setWrap(current => !current)}
          aria-pressed={wrap}
          title={wrap ? 'Disable line wrapping' : 'Enable line wrapping'}
          className={clsx(
            'ml-auto inline-flex h-6 items-center gap-1 rounded-md border px-2 text-[10px] font-medium transition-colors focus:outline-none focus:ring-2 focus:ring-accent/25',
            wrap
              ? 'border-accent/25 bg-accent/10 text-accent'
              : 'border-border-subtle bg-surface-raised text-txt-tertiary hover:text-txt-primary',
          )}
        >
          <WrapText className="h-3 w-3" />
          <span>{wrap ? 'Wrap' : 'No wrap'}</span>
        </button>
      </div>
      <div className="min-h-0 flex-1 cursor-text" onMouseDown={focusEditorFromBlankArea}>
        <CodeMirror
          value={value}
          height="100%"
          theme={theme}
          extensions={extensions}
          onCreateEditor={view => { editorViewRef.current = view }}
          onChange={nextValue => onChange(nextValue)}
        />
      </div>
    </div>
  )
}
