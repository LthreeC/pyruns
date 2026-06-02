import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from 'react'
import { useNavigate } from 'react-router-dom'
import CodeMirror from '@uiw/react-codemirror'
import { yaml as yamlLanguage } from '@codemirror/lang-yaml'
import { HighlightStyle, StreamLanguage, syntaxHighlighting } from '@codemirror/language'
import type { Extension } from '@codemirror/state'
import { EditorView } from '@codemirror/view'
import { oneDark } from '@codemirror/theme-one-dark'
import { tags as t } from '@lezer/highlight'
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  CheckCircle2,
  FileCode,
  Hash,
  LayoutGrid,
  ListChecks,
  Loader2,
  Pin,
  Sparkles,
  Terminal,
  Workflow,
} from 'lucide-react'
import clsx from 'clsx'
import { parse as yamlParse, stringify as yamlStringify } from 'yaml'
import { useGeneratorStore, useThemeStore, useWorkspaceStore } from '@/store'
import EmptyState from '@/components/shared/EmptyState'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import ActionButton from '@/components/shared/ActionButton'
import CompactSection from '@/components/shared/CompactSection'
import { PARAM_TYPE_STYLES } from '@/theme/tokens'
import * as api from '@/api'
import type { GeneratorPreview, PreviewItem, ShellRuntimeInfo } from '@/types'

const DEFAULT_SHELL_TEMPLATE = ''
type GenerationStatus = 'idle' | 'previewing' | 'creating' | 'created' | 'error'
type FormLayoutMode = 'grid' | 'tree'
const TREE_TOP_LEVEL_COLUMN_STYLE = {
  columnWidth: 'clamp(520px, 42vw, 680px)',
  columnGap: '1rem',
}

interface CreatedTaskResult {
  count: number
  taskKind: string
  firstTaskName: string
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
    if (stream.match(/^(?:echo|cd|export|set|unset|pwd|test|source|cat|python|conda|pip|git|ls|cp|mv|rm|mkdir|touch|exit)\b/)) {
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

function getEditorExtensions(mode: 'yaml' | 'shell', theme: 'light' | 'dark'): Extension[] {
  const baseTheme = theme === 'dark' ? DARK_EDITOR_THEME : [LIGHT_EDITOR_THEME]
  const highlighting = theme === 'dark' ? DARK_EDITOR_HIGHLIGHT : LIGHT_EDITOR_HIGHLIGHT
  const language = mode === 'yaml' ? YAML_EXTENSION : SHELL_LANGUAGE
  return [...baseTheme, language, highlighting, EditorView.lineWrapping]
}

function hasBatchExpression(text: string) {
  return text.includes('|') || /-?\d+\s*:\s*-?\d+(?:\s*:\s*-?\d+)?/.test(text)
}

function getBatchTriggerKind(text: string) {
  const trimmed = text.trim()
  if (/^-?\d+\s*:\s*-?\d+(?:\s*:\s*-?\d+)?$/.test(trimmed)) {
    return 'range'
  }
  if (trimmed.includes('|')) {
    return trimmed.startsWith('(') && trimmed.endsWith(')') ? 'zip' : 'product'
  }
  return 'batch'
}

function formatBatchKind(kind: string) {
  return kind.charAt(0).toUpperCase() + kind.slice(1)
}

function compactPreviewText(text: string) {
  return String(text || '').replace(/,\s+/g, '  ·  ')
}

function formatFullTaskTooltip(item: PreviewItem) {
  try {
    const yaml = yamlStringify(item.config || {}).trim()
    return yaml || item.preview
  } catch {
    return item.preview
  }
}

function getShellConfigFilename(runtime?: ShellRuntimeInfo) {
  const kind = String(runtime?.terminal_kind || '').toLowerCase()
  if (kind === 'powershell') {
    return 'config.ps1'
  }
  if (kind === 'cmd') {
    return 'config.cmd'
  }
  if (kind === 'fish') {
    return 'config.fish'
  }
  return 'config.sh'
}

function pathLeaf(path?: string) {
  const parts = String(path || '').split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] || ''
}

type ParamType = keyof typeof PARAM_TYPE_STYLES

interface PinnedParamRow {
  name: string
  fullKey: string
  value: any
  declaredType?: ParamType
  batchActive: boolean
}

interface BatchTriggerDetail {
  key: string
  value: string
  kind: string
}

function isNestedGroup(value: any) {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function inferParamType(value: any): ParamType {
  if (value === null || value === undefined) {
    return 'null'
  }
  if (typeof value === 'boolean') {
    return 'bool'
  }
  if (typeof value === 'number') {
    return Number.isInteger(value) ? 'int' : 'float'
  }
  if (Array.isArray(value)) {
    return 'list'
  }
  return 'str'
}

function buildTypeMap(config: Record<string, any> | null, prefix = ''): Record<string, ParamType> {
  if (!config) {
    return {}
  }

  const result: Record<string, keyof typeof PARAM_TYPE_STYLES> = {}
  for (const [key, value] of Object.entries(config)) {
    if (key.startsWith('_meta')) {
      continue
    }
    const fullKey = prefix ? `${prefix}.${key}` : key
    if (isNestedGroup(value)) {
      Object.assign(result, buildTypeMap(value, fullKey))
      continue
    }
    result[fullKey] = inferParamType(value)
  }
  return result
}

function getValueAtPath(data: Record<string, any>, fullKey: string) {
  const parts = fullKey.split('.').filter(Boolean)
  let current: any = data

  for (const part of parts) {
    if (!isNestedGroup(current) || !Object.prototype.hasOwnProperty.call(current, part)) {
      return undefined
    }
    current = current[part]
  }

  return current
}

function updateValueAtPath(data: Record<string, any>, fullKey: string, value: any): Record<string, any> {
  const parts = fullKey.split('.').filter(Boolean)
  if (parts.length === 0) {
    return data
  }

  const [head, ...tail] = parts
  if (tail.length === 0) {
    return { ...data, [head]: value }
  }

  const current = data[head]
  if (!isNestedGroup(current)) {
    return data
  }

  return {
    ...data,
    [head]: updateValueAtPath(current, tail.join('.'), value),
  }
}

function collectPinnedRows(
  data: Record<string, any>,
  pinnedParams: string[],
  declaredTypeMap: Record<string, ParamType>,
  batchParams: string[],
) {
  const rows: PinnedParamRow[] = []
  const seen = new Set<string>()
  const batchSet = new Set(batchParams)

  for (const fullKey of pinnedParams) {
    if (seen.has(fullKey) || fullKey.split('.').some(part => part.startsWith('_meta'))) {
      continue
    }
    seen.add(fullKey)

    const value = getValueAtPath(data, fullKey)
    if (value === undefined || isNestedGroup(value)) {
      continue
    }

    const parts = fullKey.split('.')
    rows.push({
      name: parts[parts.length - 1] || fullKey,
      fullKey,
      value,
      declaredType: declaredTypeMap[fullKey],
      batchActive: batchSet.has(fullKey),
    })
  }

  return rows
}

export default function GeneratorPage() {
  const navigate = useNavigate()
  const workspace = useWorkspaceStore(state => state.workspace)
  const theme = useThemeStore(state => state.theme)
  const {
    templates,
    selectedTemplate,
    templateContent,
    viewMode,
    yamlText,
    shellText,
    namePrefix,
    appendTimestamp,
    columns,
    pinnedParams,
    loading,
    fetchTemplates,
    loadTemplate,
    clearTemplate,
    setViewMode,
    setYamlText,
    setShellText,
    setNamePrefix,
    setAppendTimestamp,
    setColumns,
    togglePin,
  } = useGeneratorStore()

  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewData, setPreviewData] = useState<GeneratorPreview | null>(null)
  const [generating, setGenerating] = useState(false)
  const [generationStatus, setGenerationStatus] = useState<GenerationStatus>('idle')
  const [createdSummary, setCreatedSummary] = useState<CreatedTaskResult | null>(null)
  const [error, setError] = useState('')
  const [formLayoutMode, setFormLayoutMode] = useState<FormLayoutMode>('tree')
  const [treeOpenSignal, setTreeOpenSignal] = useState(0)
  const [treeOpenValue, setTreeOpenValue] = useState(true)
  const lastWorkspaceDefaultKeyRef = useRef('')
  const lastShellRootRef = useRef('')

  const isShellWorkspace = workspace?.workspace_kind === 'shell'
  const shellRuntime = workspace?.shell_runtime
  const editorMode = isShellWorkspace ? 'shell' : viewMode === 'shell' ? 'form' : viewMode
  const codeMirrorTheme = theme === 'dark' ? 'dark' : 'light'
  const yamlEditorExtensions = useMemo(() => getEditorExtensions('yaml', codeMirrorTheme), [codeMirrorTheme])
  const shellEditorExtensions = useMemo(() => getEditorExtensions('shell', codeMirrorTheme), [codeMirrorTheme])

  useEffect(() => {
    if (isShellWorkspace) {
      const shellRoot = workspace?.run_root || ''
      if (shellRoot && lastShellRootRef.current !== shellRoot) {
        lastShellRootRef.current = shellRoot
        clearTemplate()
        void fetchTemplates()
      }
      if (viewMode !== 'shell') {
        setViewMode('shell')
      }
      if (DEFAULT_SHELL_TEMPLATE && !shellText.trim()) {
        setShellText(DEFAULT_SHELL_TEMPLATE)
      }
      return
    }

    lastShellRootRef.current = ''
    if (viewMode === 'shell') {
      setViewMode('form')
    }
  }, [clearTemplate, fetchTemplates, isShellWorkspace, setShellText, setViewMode, shellText, viewMode, workspace?.run_root])

  useEffect(() => {
    if (!workspace?.run_root || isShellWorkspace) {
      return
    }
    void fetchTemplates()
  }, [
    fetchTemplates,
    isShellWorkspace,
    workspace?.config_default_source,
    workspace?.config_default_source_name,
    workspace?.run_root,
  ])

  useEffect(() => {
    if (isShellWorkspace) {
      return
    }

    if (templates.length === 0) {
      if (selectedTemplate) {
        clearTemplate()
      }
      return
    }

    const defaultTemplate = templates.find(template => pathLeaf(template.value) === 'config_default.yaml')
    const defaultTemplateValue = defaultTemplate?.value || templates[0].value
    const workspaceDefaultKey = [
      workspace?.run_root || '',
      workspace?.config_default_source || '',
      workspace?.config_default_source_name || '',
    ].join('|')
    const workspaceDefaultChanged = workspaceDefaultKey !== lastWorkspaceDefaultKeyRef.current
    if (workspaceDefaultChanged) {
      lastWorkspaceDefaultKeyRef.current = workspaceDefaultKey
    }

    if (workspaceDefaultChanged && defaultTemplateValue && selectedTemplate !== defaultTemplateValue) {
      void loadTemplate(defaultTemplateValue)
      return
    }

    const selectedStillExists = templates.some(template => template.value === selectedTemplate)
    if (!selectedStillExists) {
      void loadTemplate(defaultTemplateValue)
    }
  }, [
    clearTemplate,
    isShellWorkspace,
    loadTemplate,
    selectedTemplate,
    templates,
    workspace?.config_default_source,
    workspace?.config_default_source_name,
    workspace?.run_root,
  ])

  const parsedConfig = useMemo(() => {
    if (editorMode !== 'form') {
      return null
    }

    try {
      return (yamlParse(yamlText) as Record<string, any>) || {}
    } catch {
      return null
    }
  }, [editorMode, yamlText])

  const batchTriggerDetails = useMemo(() => {
    if (!parsedConfig) {
      return [] as BatchTriggerDetail[]
    }

    const result: BatchTriggerDetail[] = []
    const walk = (obj: Record<string, any>, prefix = '') => {
      for (const [key, value] of Object.entries(obj)) {
        const fullKey = prefix ? `${prefix}.${key}` : key
        if (
          typeof value === 'string'
          && (value.includes('|') || /^\s*-?\d+\s*:\s*-?\d+(?:\s*:\s*-?\d+)?\s*$/.test(value.trim()))
        ) {
          result.push({
            key: fullKey,
            value,
            kind: getBatchTriggerKind(value),
          })
          continue
        }
        if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
          walk(value, fullKey)
        }
      }
    }

    walk(parsedConfig)
    return result
  }, [parsedConfig])
  const batchParams = useMemo(() => batchTriggerDetails.map(item => item.key), [batchTriggerDetails])
  const declaredTypeMap = useMemo(
    () => buildTypeMap(templateContent?.parsed_config || parsedConfig),
    [parsedConfig, templateContent?.parsed_config]
  )

  const hasBatchSyntax = editorMode === 'form' && hasBatchExpression(yamlText)
  const yamlContainsBatchSyntax = editorMode === 'yaml' && hasBatchExpression(yamlText)
  const batchHintText = previewData?.count
    ? `Batch syntax detected. ${previewData.count} tasks will be created after confirmation.`
    : 'Batch syntax detected. A preview opens before creating multiple tasks.'
  const generationBusy = generating || generationStatus === 'previewing'
  const configDefaultSourceName = workspace?.config_default_source_name || ''
  const configDefaultSourcePath = workspace?.config_default_source || ''
  const showImportedConfigSource = Boolean(
    configDefaultSourceName
    && pathLeaf(selectedTemplate) === 'config_default.yaml'
  )
  const selectedTemplateListed = templates.some(template => template.value === selectedTemplate)
  const shellTemplateSelectValue = selectedTemplateListed ? selectedTemplate : ''
  const setAllTreeSections = useCallback((open: boolean) => {
    setTreeOpenValue(open)
    setTreeOpenSignal(signal => signal + 1)
  }, [])
  const handlePickShellFile = useCallback(async () => {
    setError('')
    try {
      const content = await api.pickGeneratorShellFile()
      await loadTemplate(content.value)
      await fetchTemplates()
    } catch (err: any) {
      setError(err?.message || 'Failed to load shell script')
    }
  }, [fetchTemplates, loadTemplate])

  const doCreate = useCallback(async () => {
    setGenerating(true)
    setGenerationStatus('creating')
    setCreatedSummary(null)
    setError('')
    try {
      const result = await api.createTasks({
        name_prefix: namePrefix || 'task',
        mode: editorMode,
        yaml_text: editorMode === 'shell' ? '' : yamlText,
        shell_text: editorMode === 'shell' ? shellText : '',
        template_value: isShellWorkspace ? undefined : selectedTemplate,
        append_timestamp: appendTimestamp,
      })
      setPreviewOpen(false)
      setPreviewData(null)

      try {
        await fetchTemplates()
      } catch {
        // Creation succeeded; template refresh can be retried on the next render.
      }

      setCreatedSummary({
        count: result.count,
        taskKind: result.task_kind === 'shell' ? 'shell task' : 'python task',
        firstTaskName: result.items[0]?.name || '',
      })
      setGenerationStatus('created')
    } catch (err: any) {
      setGenerationStatus('error')
      setError(err.message)
    } finally {
      setGenerating(false)
    }
  }, [appendTimestamp, editorMode, fetchTemplates, namePrefix, selectedTemplate, shellText, yamlText])

  const handleGenerate = useCallback(async () => {
    setError('')

    if (editorMode === 'form' && hasBatchSyntax && !previewOpen) {
      setGenerationStatus('previewing')
      setCreatedSummary(null)
      try {
        const preview = await api.previewTasks({
          mode: editorMode,
          yaml_text: yamlText,
          template_value: selectedTemplate,
        })
        setPreviewData(preview)
        setPreviewOpen(true)
        setGenerationStatus('idle')
      } catch (err: any) {
        setGenerationStatus('error')
        setError(err.message)
      }
      return
    }

    await doCreate()
  }, [doCreate, editorMode, hasBatchSyntax, previewOpen, selectedTemplate, yamlText])

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-border-subtle bg-surface-raised px-3 py-2">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          {!isShellWorkspace ? (
            <div className="relative min-w-[280px]">
              <select
                value={selectedTemplate}
                onChange={event => void loadTemplate(event.target.value)}
                title="Select template"
                className="w-full appearance-none rounded-md border border-border-subtle bg-surface-overlay px-3 py-1.5 pr-7 text-xs text-txt-primary outline-none transition-colors focus:border-border"
              >
                {templates.map(template => (
                  <option key={template.value} value={template.value}>{template.label}</option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
            </div>
          ) : (
            <>
              <div className="relative min-w-[280px]">
                <select
                  value={shellTemplateSelectValue}
                  onChange={event => {
                    const value = event.target.value
                    if (value) {
                      void loadTemplate(value)
                    } else {
                      clearTemplate()
                    }
                  }}
                  title="Load task or script"
                  className="w-full appearance-none rounded-md border border-border-subtle bg-surface-overlay px-3 py-1.5 pr-7 text-xs text-txt-primary outline-none transition-colors focus:border-border"
                >
                  <option value="">Load task or script</option>
                  {templates.map(template => (
                    <option key={template.value} value={template.value}>{template.label}</option>
                  ))}
                </select>
                <ChevronDown className="pointer-events-none absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
              </div>
              <button
                type="button"
                onClick={() => void handlePickShellFile()}
                className="inline-flex items-center gap-1.5 rounded-md border border-border-subtle bg-surface-overlay px-3 py-1.5 text-xs font-medium text-txt-secondary transition-colors hover:text-txt-primary"
              >
                <FileCode className="h-3 w-3" />
                <span>Browse Shell</span>
              </button>
            </>
          )}

          {!isShellWorkspace && templateContent?.read_only && (
            <span className="inline-flex items-center gap-1 rounded-md bg-amber-500/10 px-2 py-1 text-2xs text-amber-400">
              <AlertTriangle className="h-3 w-3" />
              <span>Read-only</span>
            </span>
          )}

          {showImportedConfigSource && (
            <span
              className="inline-flex min-w-0 max-w-[260px] items-center gap-1 rounded-md border border-border-subtle bg-surface-overlay px-2 py-1 text-2xs text-txt-secondary"
              title={configDefaultSourcePath || configDefaultSourceName}
            >
              <FileCode className="h-3 w-3 flex-none text-accent" />
              <span className="flex-none">Loaded from</span>
              <span className="truncate font-mono text-txt-primary">{configDefaultSourceName}</span>
            </span>
          )}

          {isShellWorkspace && templateContent?.mode_hint === 'shell' && (
            <span
              className="inline-flex min-w-0 max-w-[260px] items-center gap-1 rounded-md border border-border-subtle bg-surface-overlay px-2 py-1 text-2xs text-txt-secondary"
              title={templateContent.path}
            >
              <FileCode className="h-3 w-3 flex-none text-accent" />
              <span className="flex-none">Loaded shell</span>
              <span className="truncate font-mono text-txt-primary">{templateContent.label}</span>
            </span>
          )}
        </div>

        <div className="ml-auto flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1">
            {(isShellWorkspace ? ['shell'] : ['form', 'yaml']).map(mode => (
              <button
                key={mode}
                type="button"
                onClick={() => setViewMode(mode as 'form' | 'yaml' | 'shell')}
                className={clsx(
                  'inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-colors',
                  editorMode === mode
                    ? 'bg-surface-overlay text-txt-primary'
                    : 'text-txt-secondary hover:text-txt-primary'
                )}
              >
                {mode === 'form' && <LayoutGrid className="h-3 w-3" />}
                {mode === 'yaml' && <FileCode className="h-3 w-3" />}
                {mode === 'shell' && <Terminal className="h-3 w-3" />}
                <span>{mode.charAt(0).toUpperCase() + mode.slice(1)}</span>
              </button>
            ))}
          </div>

          {editorMode === 'form' && (
            <div className="inline-flex overflow-hidden rounded-md border border-border-subtle bg-surface-overlay">
              {(['grid', 'tree'] as FormLayoutMode[]).map(mode => (
                <button
                  key={mode}
                  type="button"
                  aria-pressed={formLayoutMode === mode}
                  onClick={() => setFormLayoutMode(mode)}
                  className={clsx(
                    'inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium transition-colors',
                    formLayoutMode === mode
                      ? 'bg-surface-raised text-txt-primary'
                      : 'text-txt-secondary hover:text-txt-primary',
                  )}
                >
                  {mode === 'grid' ? <LayoutGrid className="h-3 w-3" /> : <Workflow className="h-3 w-3" />}
                  <span>{mode === 'grid' ? 'Grid' : 'Tree'}</span>
                </button>
              ))}
            </div>
          )}

          {editorMode === 'form' && formLayoutMode === 'grid' && (
            <div className="relative">
              <select
                value={columns}
                onChange={event => setColumns(Number(event.target.value))}
                title="Columns per row"
                className="appearance-none rounded-md border border-border-subtle bg-surface-overlay px-2.5 py-1.5 pr-6 text-xs text-txt-primary outline-none transition-colors focus:border-border"
              >
                {[2, 3, 4, 5, 6, 7, 8].map(count => (
                  <option key={count} value={count}>{count} col{count > 1 ? 's' : ''}</option>
                ))}
              </select>
              <ChevronDown className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
            </div>
          )}

          {editorMode === 'form' && formLayoutMode === 'tree' && (
            <div className="inline-flex overflow-hidden rounded-md border border-border-subtle bg-surface-overlay">
              <button
                type="button"
                onClick={() => setAllTreeSections(true)}
                className="px-2.5 py-1.5 text-xs font-medium text-txt-secondary transition-colors hover:text-txt-primary"
              >
                Expand all
              </button>
              <button
                type="button"
                onClick={() => setAllTreeSections(false)}
                className="border-l border-border-subtle px-2.5 py-1.5 text-xs font-medium text-txt-secondary transition-colors hover:text-txt-primary"
              >
                Collapse all
              </button>
            </div>
          )}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <div className="min-w-0 flex flex-1 flex-col overflow-hidden" style={{ flexBasis: '78%' }}>
          {!isShellWorkspace && loading ? (
            <div className="flex h-full items-center justify-center">
              <div className="animate-pulse text-xs text-txt-tertiary">Loading template...</div>
            </div>
          ) : editorMode === 'form' ? (
            <FormEditor
              config={parsedConfig}
              columns={columns}
              layoutMode={formLayoutMode}
              openSignalValue={treeOpenValue}
              openSignalVersion={treeOpenSignal}
              declaredTypeMap={declaredTypeMap}
              pinnedParams={pinnedParams}
              batchParams={batchParams}
              onTogglePin={togglePin}
              onChange={data => setYamlText(yamlStringify(data))}
            />
          ) : editorMode === 'yaml' ? (
            <YamlEditor
              value={yamlText}
              onChange={setYamlText}
              theme={codeMirrorTheme}
              extensions={yamlEditorExtensions}
            />
          ) : (
            <ShellEditor
              value={shellText}
              onChange={setShellText}
              theme={codeMirrorTheme}
              extensions={shellEditorExtensions}
            />
          )}
        </div>

        <aside
          className="flex w-[286px] flex-col gap-2.5 overflow-y-auto border-l border-border-subtle bg-surface-raised p-2.5"
          style={{ minWidth: 268, maxWidth: 296 }}
        >
          <CompactSection title="Naming" bodyClassName="space-y-2.5 p-2">
            <div>
              <label className="block text-2xs uppercase tracking-[0.16em] text-txt-tertiary">Task Prefix</label>
              <input
                value={namePrefix}
                onChange={event => setNamePrefix(event.target.value)}
                placeholder="task"
                className="mt-1.5 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 py-1.5 text-sm font-medium text-txt-primary outline-none transition-colors focus:border-border"
              />
            </div>
            <label className="inline-flex items-center gap-2 text-xs text-txt-secondary">
              <input
                type="checkbox"
                checked={appendTimestamp}
                onChange={event => setAppendTimestamp(event.target.checked)}
                className="h-3.5 w-3.5 rounded border-border accent-accent"
              />
              <span>Append timestamp</span>
            </label>
          </CompactSection>

          {editorMode === 'form' && batchParams.length > 0 && (
            <CompactSection
              title="Pinned Batch Inputs"
              icon={<Pin className="h-3.5 w-3.5 text-accent" />}
              accent
              bodyClassName="space-y-1.5 p-2"
            >
              <div className="text-2xs text-accent">
                {batchParams.length} batch-sensitive field{batchParams.length > 1 ? 's' : ''}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {batchParams.map(param => (
                  <span
                    key={param}
                    className="rounded-md border border-accent/20 bg-accent/8 px-1.5 py-0.5 font-mono text-2xs text-accent"
                    title={param}
                  >
                    {param}
                  </span>
                ))}
              </div>
            </CompactSection>
          )}

          {editorMode === 'form' && (
            <CompactSection title="Batch Syntax" bodyClassName="space-y-1.5 p-2">
              <div className="text-2xs leading-relaxed text-txt-secondary">
                <span className="text-txt-primary">Product</span>: `lr: 0.001 | 0.01 | 0.1`
              </div>
              <div className="text-2xs leading-relaxed text-txt-secondary">
                <span className="text-txt-primary">Zip</span>: `seed: (1 | 2 | 3)`
              </div>
              <div className="text-2xs leading-relaxed text-txt-secondary">
                <span className="text-txt-primary">Range</span>: `epoch: 10:100:1`
              </div>
            </CompactSection>
          )}

          {editorMode === 'yaml' && (
            <CompactSection title="YAML Mode" bodyClassName="space-y-1.5 p-2">
              <div className="text-2xs leading-relaxed text-txt-secondary">
                YAML mode creates exactly one <code className="text-txt-primary">config.yaml</code> task.
              </div>
              <div className="text-2xs leading-relaxed text-txt-secondary">
                Batch syntax is disabled here. Switch back to <span className="text-txt-primary">Form</span> for batch expansion and parameter pinning.
              </div>
              {yamlContainsBatchSyntax && (
                <div className="rounded-md border border-amber-500/20 bg-amber-500/10 px-2.5 py-2 text-2xs text-amber-400">
                  Batch syntax was detected in YAML mode, but YAML mode only creates one task. Use Form mode if you want batch generation.
                </div>
              )}
            </CompactSection>
          )}

          {editorMode === 'shell' && (
            <ShellRuntimePanel runtime={shellRuntime} runRoot={workspace?.run_root} />
          )}

          {error && (
            <CompactSection title="Status" bodyClassName="space-y-1.5 p-2">
              <div className="rounded-md border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-xs text-rose-400" title={error}>
                {error}
              </div>
            </CompactSection>
          )}

          <div className="sticky bottom-0 mt-auto border-t border-border-subtle bg-surface-raised pt-3">
            <ActionButton
              icon={generationBusy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Sparkles className="h-4 w-4" />}
              variant="primary"
              size="md"
              className="w-full"
              onClick={handleGenerate}
              disabled={generationBusy}
            >
              {generationStatus === 'previewing'
                ? 'Previewing...'
                : generationStatus === 'creating'
                  ? 'Creating...'
                  : editorMode === 'shell' ? 'Create Shell Task' : hasBatchSyntax ? 'Preview Batch Tasks' : 'Generate Tasks'}
            </ActionButton>
            <GenerationFeedback
              status={generationStatus}
              createdSummary={createdSummary}
              defaultText={
                editorMode === 'form' && hasBatchSyntax
                  ? batchHintText
                  : editorMode === 'yaml' && yamlContainsBatchSyntax
                    ? 'YAML mode does not expand batch syntax. Switch back to Form mode.'
                  : editorMode === 'shell'
                    ? 'Creates one shell task immediately.'
                    : 'Creates one task immediately.'
              }
              onOpenManager={() => {
                const taskName = createdSummary?.firstTaskName
                navigate(taskName ? `/manager?task=${encodeURIComponent(taskName)}` : '/manager')
              }}
            />
          </div>
        </aside>
      </div>

      <ConfirmDialog
        open={previewOpen}
        title="Batch Preview"
        description="Review the expansion before writing task folders."
        confirmLabel="Generate All"
        size="lg"
        onConfirm={doCreate}
        onCancel={() => setPreviewOpen(false)}
      >
        <BatchPreviewContent preview={previewData} triggers={batchTriggerDetails} />
      </ConfirmDialog>
    </div>
  )
}

function GenerationFeedback({
  status,
  createdSummary,
  defaultText,
  onOpenManager,
}: {
  status: GenerationStatus
  createdSummary: CreatedTaskResult | null
  defaultText: string
  onOpenManager: () => void
}) {
  if (status === 'creating') {
    return (
      <div className="mt-2 flex items-center justify-center gap-1.5 rounded-md bg-accent/8 px-2.5 py-2 text-2xs font-medium text-accent">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Writing task folders...
      </div>
    )
  }

  if (status === 'previewing') {
    return (
      <div className="mt-2 flex items-center justify-center gap-1.5 rounded-md bg-accent/8 px-2.5 py-2 text-2xs font-medium text-accent">
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        Preparing batch preview...
      </div>
    )
  }

  if (status === 'created' && createdSummary) {
    return <CreatedTaskSummary summary={createdSummary} onOpenManager={onOpenManager} />
  }

  return <div className="mt-2 text-center text-2xs text-txt-tertiary">{defaultText}</div>
}

function CreatedTaskSummary({
  summary,
  onOpenManager,
}: {
  summary: CreatedTaskResult
  onOpenManager: () => void
}) {
  return (
    <div className="mt-2 space-y-2 rounded-md border border-emerald-500/25 bg-emerald-500/10 px-2.5 py-2 text-2xs text-emerald-700 dark:text-emerald-300">
      <div className="flex items-start gap-1.5">
        <CheckCircle2 className="mt-0.5 h-3.5 w-3.5 flex-none" />
        <div className="min-w-0">
          <div className="font-medium">
            Created {summary.count} {summary.taskKind}{summary.count > 1 ? 's' : ''}
          </div>
          {summary.firstTaskName && (
            <div className="mt-0.5 truncate font-mono text-[11px]" title={summary.firstTaskName}>
              {summary.firstTaskName}
            </div>
          )}
        </div>
      </div>
      <button
        type="button"
        onClick={onOpenManager}
        className="inline-flex w-full items-center justify-center rounded-md bg-emerald-600 px-2.5 py-1.5 text-xs font-medium text-white transition-colors hover:bg-emerald-500"
      >
        Open in Manager
      </button>
    </div>
  )
}

function BatchPreviewContent({
  preview,
  triggers,
}: {
  preview: GeneratorPreview | null
  triggers: BatchTriggerDetail[]
}) {
  const count = preview?.count || 0

  return (
    <div className="space-y-4">
      <div className="grid gap-2 sm:grid-cols-2">
        <BatchPreviewMetric
          icon={<Hash className="h-3.5 w-3.5" />}
          label="Tasks to create"
          value={count ? String(count) : '-'}
          accent
        />
        <BatchPreviewMetric
          icon={<Workflow className="h-3.5 w-3.5" />}
          label="Batch triggers"
          value={String(triggers.length)}
        />
      </div>

      {triggers.length > 0 && (
        <section className="border-t border-border-subtle pt-3">
          <div className="mb-2 flex items-center gap-1.5 text-xs font-semibold text-txt-primary">
            <Workflow className="h-3.5 w-3.5 text-accent" />
            <span>Batch triggers</span>
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            {triggers.map(item => (
              <div
                key={item.key}
                className="min-w-0 px-0.5 py-1.5"
                title={item.value}
              >
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate font-mono text-xs font-semibold text-txt-primary">
                    {item.key}
                  </span>
                  <span className="flex-none rounded-md bg-accent/8 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-[0.12em] text-accent">
                    {formatBatchKind(item.kind)}
                  </span>
                </div>
                <div className="mt-1 truncate font-mono text-2xs text-txt-tertiary">
                  {item.value}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}

      <BatchPreviewList items={preview?.items || []} count={count} />
    </div>
  )
}

function BatchPreviewMetric({
  icon,
  label,
  value,
  accent = false,
}: {
  icon: ReactNode
  label: string
  value: string
  accent?: boolean
}) {
  return (
    <div
      className={clsx(
        'rounded-md px-3 py-2.5',
        accent ? 'bg-accent/8' : 'bg-surface-overlay/60',
      )}
    >
      <div className={clsx('mb-1 flex items-center gap-1.5 text-2xs', accent ? 'text-accent' : 'text-txt-tertiary')}>
        {icon}
        <span>{label}</span>
      </div>
      <div className="text-xl font-semibold leading-none text-txt-primary">{value}</div>
    </div>
  )
}

function BatchPreviewList({
  items,
  count,
}: {
  items: PreviewItem[]
  count: number
}) {
  return (
    <section className="border-t border-border-subtle pt-3">
      <div className="flex items-center justify-between px-0.5 pb-2">
        <div className="flex items-center gap-1.5 text-xs font-semibold text-txt-primary">
          <ListChecks className="h-3.5 w-3.5 text-accent" />
          <span>Task samples</span>
        </div>
        <span className="text-2xs text-txt-tertiary">
          {items.length ? `First ${items.length}` : 'No preview'}
        </span>
      </div>
      <div className="max-h-[320px] space-y-1 overflow-y-auto">
        {items.map(item => (
          <div
            key={item.index}
            className="grid grid-cols-[56px_minmax(0,1fr)] gap-2 rounded-md px-2 py-2 odd:bg-surface-overlay/40"
            title={formatFullTaskTooltip(item)}
          >
            <span className="text-center font-mono text-2xs font-semibold text-txt-secondary">
              #{item.index}
            </span>
            <span className="min-w-0 break-words font-mono text-xs leading-relaxed text-txt-secondary">
              {compactPreviewText(item.preview)}
            </span>
          </div>
        ))}
        {count > items.length && (
          <div className="px-2 py-1.5 text-xs text-txt-tertiary">
            Plus {count - items.length} more task{count - items.length > 1 ? 's' : ''}
          </div>
        )}
      </div>
    </section>
  )
}

function ShellRuntimePanel({
  runtime,
  runRoot,
}: {
  runtime?: ShellRuntimeInfo
  runRoot?: string
}) {
  const currentShell = runtime?.mode === 'custom'
    ? 'Custom shell'
    : runtime?.display_name || 'Follow current terminal'
  const executable = runtime?.executable || 'Auto-detect'
  const mode = runtime?.mode === 'custom' ? 'Custom' : 'Follow'

  return (
    <CompactSection
      title="Shell Runtime"
      icon={<Terminal className="h-3.5 w-3.5 text-accent" />}
      accent
      bodyClassName="space-y-2 p-2.5"
    >
      <ShellRuntimeRow label="Current shell" value={currentShell} />
      <ShellRuntimeRow label="Resolved file" value={getShellConfigFilename(runtime)} mono />
      <ShellRuntimeRow label="Workspace folder" value={pathLeaf(runRoot) || '_shell_'} mono />
      <ShellRuntimeRow label="Mode" value={mode} />
      <div className="truncate rounded-md bg-surface-overlay px-2.5 py-2 font-mono text-2xs text-txt-tertiary" title={executable}>
        {executable}
      </div>
    </CompactSection>
  )
}

function ShellRuntimeRow({
  label,
  value,
  mono = false,
}: {
  label: string
  value: string
  mono?: boolean
}) {
  return (
    <div className="flex items-center justify-between gap-2 text-2xs">
      <span className="text-txt-tertiary">{label}</span>
      <span className={clsx('min-w-0 truncate text-right text-txt-primary', mono && 'font-mono')} title={value}>
        {value}
      </span>
    </div>
  )
}

function FormEditor({
  config,
  columns,
  layoutMode,
  openSignalValue,
  openSignalVersion,
  declaredTypeMap,
  pinnedParams,
  batchParams,
  onTogglePin,
  onChange,
}: {
  config: Record<string, any> | null
  columns: number
  layoutMode: FormLayoutMode
  openSignalValue: boolean
  openSignalVersion: number
  declaredTypeMap: Record<string, keyof typeof PARAM_TYPE_STYLES>
  pinnedParams: string[]
  batchParams: string[]
  onTogglePin: (key: string) => void
  onChange: (data: Record<string, any>) => void
}) {
  const [data, setData] = useState<Record<string, any>>(config || {})
  const pinnedRows = useMemo(
    () => collectPinnedRows(data, pinnedParams, declaredTypeMap, batchParams),
    [batchParams, data, declaredTypeMap, pinnedParams]
  )
  const pinnedRowKeys = useMemo(() => new Set(pinnedRows.map(row => row.fullKey)), [pinnedRows])

  useEffect(() => {
    if (config) {
      setData(config)
    }
  }, [config])

  if (!config || Object.keys(config).length === 0) {
    return <EmptyState title="No parameters" description="Load a template to edit parameters" />
  }

  const allKeys = Object.keys(data).filter(key => !key.startsWith('_meta'))

  const handleChange = (key: string, value: any) => {
    const next = { ...data, [key]: value }
    setData(next)
    onChange(next)
  }

  const handlePinnedChange = (fullKey: string, value: any) => {
    const next = updateValueAtPath(data, fullKey, value)
    setData(next)
    onChange(next)
  }
  const gridStyle = { gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }
  const contentStyle = layoutMode === 'tree' ? TREE_TOP_LEVEL_COLUMN_STYLE : gridStyle
  const contentClassName = layoutMode === 'tree' ? 'space-y-3' : 'grid gap-2'
  const childSectionClassName = layoutMode === 'tree' ? 'mb-3 inline-block w-full align-top [break-inside:avoid]' : 'col-span-full'

  return (
    <div className="h-full overflow-y-auto p-3">
      {pinnedRows.length > 0 && (
        <PinnedParameters
          rows={pinnedRows}
          columns={columns}
          layoutMode={layoutMode}
          onTogglePin={onTogglePin}
          onChange={handlePinnedChange}
        />
      )}
      {pinnedRows.length > 0 && (
        <div className="mb-2 mt-3 flex items-center gap-1.5 text-2xs font-bold uppercase tracking-[0.16em] text-txt-tertiary">
          <LayoutGrid className="h-3.5 w-3.5" />
          <span>All Parameters</span>
        </div>
      )}
      <div
        className={contentClassName}
        style={contentStyle}
      >
        {allKeys.filter(key => !key.startsWith('_meta') && !pinnedRowKeys.has(key)).map(key => {
          const value = data[key]
          if (isNestedGroup(value)) {
            return (
              <div key={key} className={childSectionClassName}>
                <NestedSection
                  name={key}
                  data={value}
                  depth={0}
                  columns={columns}
                  layoutMode={layoutMode}
                  openSignalValue={openSignalValue}
                  openSignalVersion={openSignalVersion}
                  declaredTypeMap={declaredTypeMap}
                  pinnedParams={pinnedParams}
                  pinnedRowKeys={pinnedRowKeys}
                  batchParams={batchParams}
                  prefix={key}
                  onTogglePin={onTogglePin}
                  onChange={next => handleChange(key, next)}
                />
              </div>
            )
          }
          return (
            <ParamRow
              key={key}
              name={key}
              value={value}
              declaredType={declaredTypeMap[key]}
              layoutMode={layoutMode}
              pinned={pinnedParams.includes(key)}
              batchActive={batchParams.includes(key)}
              onChange={next => handleChange(key, next)}
              onTogglePin={() => onTogglePin(key)}
            />
          )
        })}
      </div>
    </div>
  )
}

function PinnedParameters({
  rows,
  columns,
  layoutMode,
  onTogglePin,
  onChange,
}: {
  rows: PinnedParamRow[]
  columns: number
  layoutMode: FormLayoutMode
  onTogglePin: (key: string) => void
  onChange: (fullKey: string, value: any) => void
}) {
  const gridStyle = { gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }
  const contentStyle = layoutMode === 'tree' ? undefined : gridStyle
  const contentClassName = layoutMode === 'tree' ? 'space-y-1.5' : 'grid gap-2'

  return (
    <CompactSection
      title="Pinned Parameters"
      count={rows.length}
      icon={<Pin className="h-3.5 w-3.5 text-accent" />}
      accent
      className="mb-3 rounded-md border border-accent/20 bg-accent/5 p-2"
      bodyClassName="pt-0"
    >
      <div
        className={contentClassName}
        style={contentStyle}
      >
        {rows.map(row => (
          <ParamRow
            key={row.fullKey}
            name={row.fullKey}
            value={row.value}
            declaredType={row.declaredType}
            layoutMode={layoutMode}
            pinned
            batchActive={row.batchActive}
            onChange={next => onChange(row.fullKey, next)}
            onTogglePin={() => onTogglePin(row.fullKey)}
          />
        ))}
      </div>
    </CompactSection>
  )
}

function NestedSection({
  name,
  data,
  depth,
  columns,
  layoutMode,
  openSignalValue,
  openSignalVersion,
  declaredTypeMap,
  pinnedParams,
  pinnedRowKeys,
  batchParams,
  prefix,
  onTogglePin,
  onChange,
}: {
  name: string
  data: Record<string, any>
  depth: number
  columns: number
  layoutMode: FormLayoutMode
  openSignalValue: boolean
  openSignalVersion: number
  declaredTypeMap: Record<string, keyof typeof PARAM_TYPE_STYLES>
  pinnedParams: string[]
  pinnedRowKeys: Set<string>
  batchParams: string[]
  prefix: string
  onTogglePin: (key: string) => void
  onChange: (data: Record<string, any>) => void
}) {
  const [open, setOpen] = useState(true)
  const visibleEntries = Object.entries(data).filter(([key]) => !key.startsWith('_meta'))
  const treeSection = layoutMode === 'tree'
  const treeConnector = treeSection && depth > 0
  const gridStyle = { gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }
  const contentStyle = layoutMode === 'tree' ? undefined : gridStyle
  const contentClassName = layoutMode === 'tree' ? 'space-y-1.5' : 'grid gap-2'
  const childSectionClassName = layoutMode === 'tree' ? 'w-full' : 'col-span-full'

  useEffect(() => {
    setOpen(openSignalValue)
  }, [openSignalValue, openSignalVersion])

  const handleChange = (key: string, value: any) => {
    onChange({ ...data, [key]: value })
  }

  return (
    <div
      className={clsx(
        'relative box-border transition-colors',
        treeSection
          ? 'overflow-visible rounded-md border border-transparent bg-transparent'
          : 'overflow-hidden rounded-md border bg-surface-raised',
        treeSection && depth === 0 && 'border-border-subtle/80 bg-surface-raised/55 px-2 py-1.5',
        !treeSection && (depth === 0 ? 'border-border-subtle' : 'border-border-subtle/80 border-l-2 border-border-subtle/70'),
      )}
      style={treeSection ? undefined : { paddingLeft: `${Math.min(depth, 5) * 10}px` }}
    >
      {treeConnector && (
        <div className="pointer-events-none absolute bottom-0 left-0 top-4 border-l border-border-subtle/60" />
      )}
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className={clsx(
          'relative flex min-h-8 w-full items-center gap-1.5 text-left transition-colors',
          treeSection
            ? 'rounded-md px-2 py-1.5 hover:bg-surface-overlay/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/25'
            : 'border-b border-border-subtle px-2.5 py-1.5 hover:bg-surface-overlay',
          treeSection && depth === 0 && 'bg-surface-overlay/35',
          !treeSection && (depth === 0 ? 'bg-surface-raised' : 'bg-surface-overlay/45'),
        )}
        title={`${prefix} (${Object.keys(data).length} fields)`}
      >
        {treeConnector && (
          <span className="absolute left-0 top-1/2 w-2 border-t border-border-subtle/60" />
        )}
        {open ? <ChevronDown className="h-3.5 w-3.5 text-txt-tertiary" /> : <ChevronRight className="h-3.5 w-3.5 text-txt-tertiary" />}
        <span className="truncate text-sm font-semibold text-txt-primary" title={name}>{name}</span>
        {depth > 0 && (
          <span className="min-w-0 truncate font-mono text-2xs text-txt-tertiary">
            {prefix}
          </span>
        )}
        <span className="rounded-md bg-surface-overlay px-1.5 py-0.5 text-2xs font-medium text-txt-secondary">
          {visibleEntries.length}
        </span>
      </button>
      {open && (
        <div className={treeSection ? 'ml-4 border-l border-border-subtle/60 pb-1 pl-4 pt-1' : 'p-2'}>
          <div
            className={contentClassName}
            style={contentStyle}
          >
            {visibleEntries.map(([key, value]) => {
              const fullKey = `${prefix}.${key}`
              if (isNestedGroup(value)) {
                return (
                  <div key={key} className={childSectionClassName}>
                    <NestedSection
                      name={key}
                      data={value}
                      depth={depth + 1}
                      columns={columns}
                      layoutMode={layoutMode}
                      openSignalValue={openSignalValue}
                      openSignalVersion={openSignalVersion}
                      declaredTypeMap={declaredTypeMap}
                      pinnedParams={pinnedParams}
                      pinnedRowKeys={pinnedRowKeys}
                      batchParams={batchParams}
                      prefix={fullKey}
                      onTogglePin={onTogglePin}
                      onChange={next => handleChange(key, next)}
                    />
                  </div>
                )
              }
              if (pinnedRowKeys.has(fullKey)) {
                return null
              }
              return (
                <ParamRow
                  key={key}
                  name={key}
                  value={value}
                  declaredType={declaredTypeMap[fullKey]}
                  layoutMode={layoutMode}
                  pinned={pinnedParams.includes(fullKey)}
                  batchActive={batchParams.includes(fullKey)}
                  onChange={next => handleChange(key, next)}
                  onTogglePin={() => onTogglePin(fullKey)}
                />
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

function ParamRow({
  name,
  value,
  declaredType,
  layoutMode,
  pinned,
  batchActive,
  onChange,
  onTogglePin,
}: {
  name: string
  value: any
  declaredType?: keyof typeof PARAM_TYPE_STYLES
  layoutMode?: FormLayoutMode
  pinned?: boolean
  batchActive?: boolean
  onChange: (value: any) => void
  onTogglePin: () => void
}) {
  const originalType = declaredType || inferParamType(value)
  const treeParamRow = layoutMode === 'tree'

  const [localValue, setLocalValue] = useState(stringifyEditable(value))

  useEffect(() => {
    setLocalValue(stringifyEditable(value))
  }, [value])

  const hasBatch = localValue.includes('|') || /^\s*-?\d+\s*:\s*-?\d+(?:\s*:\s*-?\d+)?\s*$/.test(localValue.trim())

  const commitValue = () => {
    const next = localValue.trim()
    if (originalType === 'bool' && !hasBatch) {
      if (next === 'true' || next === 'True' || next === '1') {
        onChange(true)
        return
      }
      if (next === 'false' || next === 'False' || next === '0') {
        onChange(false)
        return
      }
    }
    if ((originalType === 'int' || originalType === 'float') && !hasBatch && next !== '' && !Number.isNaN(Number(next))) {
      onChange(originalType === 'int' ? Math.round(Number(next)) : Number(next))
      return
    }
    if (originalType === 'list' && !hasBatch) {
      try {
        const parsed = JSON.parse(localValue)
        if (Array.isArray(parsed)) {
          onChange(parsed)
          return
        }
      } catch {
        // Fall back to string if the edited value is not valid JSON.
      }
    }
    if (originalType === 'null' && !hasBatch && next === '') {
      onChange(null)
      return
    }
    onChange(localValue)
  }

  return (
    <div
      className={clsx(
        treeParamRow
          ? 'flex min-h-8 border-transparent bg-transparent px-2 py-1 hover:bg-surface-overlay/70 min-w-0 items-center gap-2 rounded-md border transition-colors focus-within:border-border-subtle focus-within:bg-surface-raised/80'
          : 'flex items-center gap-1.5 rounded-md border px-1.5 py-1 transition-colors',
        treeParamRow
          ? pinned && 'border-accent/20 bg-accent/5'
          : (pinned ? 'border-accent/20 bg-accent/5' : 'border-border-subtle bg-surface-raised hover:border-border'),
        (hasBatch || batchActive) && (treeParamRow ? 'border-amber-500/20 bg-amber-500/5' : 'border-amber-500/20 bg-amber-500/5'),
      )}
    >
      <button
        type="button"
        onClick={event => {
          event.stopPropagation()
          onTogglePin()
        }}
        title={pinned ? 'Unpin' : 'Pin'}
        aria-label={pinned ? `Unpin ${name}` : `Pin ${name}`}
        className={clsx(
          'flex h-6 w-6 flex-none items-center justify-center rounded-md transition-colors',
          pinned ? 'text-accent' : 'text-txt-tertiary hover:text-accent'
        )}
      >
        <Pin className="h-3 w-3" />
      </button>

      <div className={clsx('flex min-w-0 items-center gap-1.5', treeParamRow ? 'flex-[0.7]' : 'flex-1')}>
        <span className="min-w-0 truncate text-xs font-semibold text-txt-primary" title={name}>
          {name}
        </span>

        <span className={clsx(
          'flex-none rounded-md px-1.5 py-0.5 text-[10px] font-mono',
          PARAM_TYPE_STYLES[originalType]
        )}>
          {originalType}
        </span>
      </div>

      {originalType === 'bool' && !hasBatch ? (
        <div
          className={clsx(
            'ml-auto flex min-w-0 items-center gap-1.5',
            treeParamRow ? 'flex-[1.3] justify-start' : 'flex-none justify-end',
          )}
        >
          <button
            type="button"
            onClick={() => onChange(!value)}
            title={`Toggle ${name}`}
            className={clsx(
              'relative h-4.5 w-9 rounded-full transition-colors',
              value ? 'bg-accent' : 'bg-zinc-600'
            )}
          >
            <span
              className={clsx(
                'absolute top-0.5 h-3.5 w-3.5 rounded-full bg-white transition-transform',
                value ? 'left-[18px]' : 'left-0.5'
              )}
            />
          </button>
          <span className="min-w-0 truncate text-xs font-mono text-txt-secondary" title={String(value)}>{String(value)}</span>
        </div>
      ) : (
        <input
          type="text"
          value={localValue}
          onChange={event => setLocalValue(event.target.value)}
          onBlur={commitValue}
          onKeyDown={event => {
            if (event.key === 'Enter') {
              event.preventDefault()
              commitValue()
            }
          }}
          spellCheck={false}
          title={localValue}
          className={clsx(
            'rounded-md border bg-transparent px-1.5 py-1 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-border',
            treeParamRow ? 'ml-auto min-w-0 flex-[1.3]' : 'ml-auto min-w-0 flex-1',
            hasBatch || batchActive ? 'border-amber-500/20' : 'border-border-subtle',
          )}
        />
      )}
    </div>
  )
}

function YamlEditor({
  value,
  onChange,
  theme,
  extensions,
}: {
  value: string
  onChange: (value: string) => void
  theme: 'light' | 'dark'
  extensions: any[]
}) {
  return (
    <CodeEditorFrame value={value} onChange={onChange} theme={theme} extensions={extensions} />
  )
}

function ShellEditor({
  value,
  onChange,
  theme,
  extensions,
}: {
  value: string
  onChange: (value: string) => void
  theme: 'light' | 'dark'
  extensions: any[]
}) {
  return (
    <CodeEditorFrame value={value} onChange={onChange} theme={theme} extensions={extensions} />
  )
}

function CodeEditorFrame({
  value,
  onChange,
  theme,
  extensions,
}: {
  value: string
  onChange: (value: string) => void
  theme: 'light' | 'dark'
  extensions: any[]
}) {
  const editorViewRef = useRef<EditorView | null>(null)

  const focusEditorFromBlankArea = useCallback((event: ReactMouseEvent<HTMLDivElement>) => {
    const target = event.target as HTMLElement | null
    if (!target || target.closest('.cm-content') || target.closest('.cm-gutters')) {
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
    <div className="h-full p-3">
      <div className="generator-code-editor cursor-text" onMouseDown={focusEditorFromBlankArea}>
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

function stringifyEditable(value: any) {
  if (Array.isArray(value)) {
    try {
      return JSON.stringify(value)
    } catch {
      return String(value)
    }
  }
  return String(value ?? '')
}
