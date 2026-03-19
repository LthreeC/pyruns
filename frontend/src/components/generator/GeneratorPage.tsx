import { useCallback, useEffect, useMemo, useState } from 'react'
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
  FileCode,
  LayoutGrid,
  Pin,
  Sparkles,
  Terminal,
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

const DEFAULT_SHELL_TEMPLATE = ''
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
  return [...baseTheme, language, highlighting]
}

function hasBatchExpression(text: string) {
  return text.includes('|') || /-?\d+\s*:\s*-?\d+(?:\s*:\s*-?\d+)?/.test(text)
}

function buildGeneratedTemplateValue(task: { name?: string; config_file?: string; task_kind?: string }) {
  if ((task.task_kind || 'config') !== 'config' || !task.name) {
    return ''
  }
  return `tasks/${task.name}/${task.config_file || 'config.yaml'}`
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

function inferParamType(value: any): keyof typeof PARAM_TYPE_STYLES {
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

function buildTypeMap(config: Record<string, any> | null, prefix = ''): Record<string, keyof typeof PARAM_TYPE_STYLES> {
  if (!config) {
    return {}
  }

  const result: Record<string, keyof typeof PARAM_TYPE_STYLES> = {}
  for (const [key, value] of Object.entries(config)) {
    if (key.startsWith('_meta')) {
      continue
    }
    const fullKey = prefix ? `${prefix}.${key}` : key
    if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
      Object.assign(result, buildTypeMap(value, fullKey))
      continue
    }
    result[fullKey] = inferParamType(value)
  }
  return result
}

export default function GeneratorPage() {
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
    togglePin,
  } = useGeneratorStore()

  const [columns, setColumns] = useState(5)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewData, setPreviewData] = useState<any>(null)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  const isShellWorkspace = workspace?.workspace_kind === 'shell'
  const shellRuntime = workspace?.shell_runtime
  const editorMode = isShellWorkspace ? 'shell' : viewMode === 'shell' ? 'form' : viewMode
  const codeMirrorTheme = theme === 'dark' ? 'dark' : 'light'
  const yamlEditorExtensions = useMemo(() => getEditorExtensions('yaml', codeMirrorTheme), [codeMirrorTheme])
  const shellEditorExtensions = useMemo(() => getEditorExtensions('shell', codeMirrorTheme), [codeMirrorTheme])

  useEffect(() => {
    if (isShellWorkspace) {
      clearTemplate()
      if (viewMode !== 'shell') {
        setViewMode('shell')
      }
      if (DEFAULT_SHELL_TEMPLATE && !shellText.trim()) {
        setShellText(DEFAULT_SHELL_TEMPLATE)
      }
      return
    }

    if (viewMode === 'shell') {
      setViewMode('form')
    }
  }, [clearTemplate, isShellWorkspace, setShellText, setViewMode, shellText, viewMode])

  useEffect(() => {
    if (!workspace?.run_root || isShellWorkspace) {
      return
    }
    void fetchTemplates()
  }, [fetchTemplates, isShellWorkspace, workspace?.run_root])

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

    const selectedStillExists = templates.some(template => template.value === selectedTemplate)
    if (!selectedStillExists) {
      void loadTemplate(templates[0].value)
    }
  }, [clearTemplate, isShellWorkspace, loadTemplate, selectedTemplate, templates])

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
      return [] as { key: string; value: string; kind: string }[]
    }

    const result: { key: string; value: string; kind: string }[] = []
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
  const batchHintText = previewData?.count
    ? `Batch syntax detected. ${previewData.count} tasks will be created after confirmation.`
    : 'Batch syntax detected. A preview opens before creating multiple tasks.'

  const doCreate = useCallback(async () => {
    setGenerating(true)
    setError('')
    setSuccess('')
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

      let selectedGeneratedTask = ''
      const generatedTemplateValue = !isShellWorkspace && result.items.length > 0
        ? buildGeneratedTemplateValue(result.items[0])
        : ''

      if (generatedTemplateValue) {
        try {
          await fetchTemplates()
          await loadTemplate(generatedTemplateValue)
          selectedGeneratedTask = result.items[0].name
        } catch {
          selectedGeneratedTask = ''
        }
      }

      setSuccess(
        selectedGeneratedTask
          ? `Created ${result.count} ${result.task_kind === 'shell' ? 'shell task' : 'task'}${result.count > 1 ? 's' : ''}. Selected ${selectedGeneratedTask}.`
          : `Created ${result.count} ${result.task_kind === 'shell' ? 'shell task' : 'task'}${result.count > 1 ? 's' : ''}`
      )
      window.setTimeout(() => setSuccess(''), 4000)
    } catch (err: any) {
      setError(err.message)
    } finally {
      setGenerating(false)
    }
  }, [appendTimestamp, editorMode, fetchTemplates, isShellWorkspace, loadTemplate, namePrefix, selectedTemplate, shellText, yamlText])

  const handleGenerate = useCallback(async () => {
    setError('')
    setSuccess('')

    if (editorMode === 'form' && hasBatchSyntax && !previewOpen) {
      try {
        const preview = await api.previewTasks({
          mode: editorMode,
          yaml_text: yamlText,
          template_value: selectedTemplate,
        })
        setPreviewData(preview)
        setPreviewOpen(true)
      } catch (err: any) {
        setError(err.message)
      }
      return
    }

    await doCreate()
  }, [doCreate, editorMode, hasBatchSyntax, previewOpen, selectedTemplate, yamlText])

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-border-subtle bg-surface-raised px-3 py-2">
        {!isShellWorkspace ? (
          <div className="relative">
            <select
              value={selectedTemplate}
              onChange={event => void loadTemplate(event.target.value)}
              title="Select template"
              className="max-w-[320px] appearance-none rounded-md border border-border-subtle bg-surface-overlay px-3 py-1.5 pr-7 text-xs text-txt-primary outline-none transition-colors focus:border-border"
            >
              {templates.map(template => (
                <option key={template.value} value={template.value}>{template.label}</option>
              ))}
            </select>
            <ChevronDown className="pointer-events-none absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
          </div>
        ) : (
          <span className="inline-flex items-center gap-1.5 rounded-md border border-accent/20 bg-accent/8 px-3 py-1.5 text-xs font-medium text-accent">
            <Terminal className="h-3.5 w-3.5" />
            <span>Shell Workspace</span>
          </span>
        )}

        {editorMode === 'form' && (
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

        {!isShellWorkspace && templateContent?.read_only && (
          <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-1 text-2xs text-amber-400">
            <AlertTriangle className="h-3 w-3" />
            <span>Read-only</span>
          </span>
        )}

        <div className="flex-1" />

        <div className="flex items-center rounded-lg border border-border-subtle bg-surface-overlay p-0.5">
          {(isShellWorkspace ? ['shell'] : ['form', 'yaml']).map(mode => (
            <button
              key={mode}
              type="button"
              onClick={() => setViewMode(mode as 'form' | 'yaml' | 'shell')}
              className={clsx(
                'inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-colors',
                editorMode === mode
                  ? 'bg-surface-raised text-txt-primary'
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
                Switch back to <span className="text-txt-primary">Form</span> for batch expansion and parameter pinning.
              </div>
            </CompactSection>
          )}

          {editorMode === 'shell' && (
            <CompactSection
              title="Shell Mode"
              icon={<Terminal className="h-3.5 w-3.5 text-accent" />}
              accent
              bodyClassName="space-y-1.5 p-2"
            >
              <div className="text-2xs leading-relaxed text-txt-secondary">
                Shell mode creates one <code className="text-txt-primary">config.sh</code> task at a time.
              </div>
              <div className="text-2xs leading-relaxed text-txt-secondary">
                The script runs inside <code className="text-txt-primary">_shell_</code> by following the terminal that launched <code className="text-txt-primary">pyr</code>.
              </div>
              <div className="text-2xs leading-relaxed text-txt-secondary">
                {shellRuntime?.mode === 'custom'
                  ? <>Custom shell active: <code className="text-txt-primary">{shellRuntime.executable || 'unset'}</code></>
                  : <>Current shell: <code className="text-txt-primary">{shellRuntime?.display_name || 'Follow current terminal'}</code></>}
              </div>
              <div className="text-2xs leading-relaxed text-txt-secondary">
                To override the default, set <code className="text-txt-primary">shell_mode: custom</code> and <code className="text-txt-primary">shell_executable</code> in <code className="text-txt-primary">_pyruns_settings.yaml</code>.
              </div>
            </CompactSection>
          )}

          {(error || success) && (
            <CompactSection title="Status" bodyClassName="space-y-1.5 p-2">
              {error && (
                <div className="rounded-md border border-rose-500/20 bg-rose-500/10 px-3 py-2 text-xs text-rose-400" title={error}>
                  {error}
                </div>
              )}
              {success && (
                <div className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-xs text-emerald-400">
                  {success}
                </div>
              )}
            </CompactSection>
          )}

          <div className="sticky bottom-0 mt-auto border-t border-border-subtle bg-surface-raised pt-3">
            <ActionButton
              icon={<Sparkles className="h-4 w-4" />}
              variant="primary"
              size="md"
              className="w-full"
              onClick={handleGenerate}
              disabled={generating}
            >
              {generating ? 'Creating...' : editorMode === 'shell' ? 'Create Shell Task' : hasBatchSyntax ? 'Preview Batch Tasks' : 'Generate Tasks'}
            </ActionButton>
            <div className="mt-2 text-center text-2xs text-txt-tertiary">
              {editorMode === 'form' && hasBatchSyntax
                ? batchHintText
                : editorMode === 'shell'
                  ? 'Creates one config.sh task immediately.'
                  : 'Creates one task immediately.'}
            </div>
          </div>
        </aside>
      </div>

      <ConfirmDialog
        open={previewOpen}
        title="Batch Preview"
        description={previewData ? `${previewData.count} task(s) will be created` : ''}
        confirmLabel="Generate All"
        onConfirm={doCreate}
        onCancel={() => setPreviewOpen(false)}
      >
        {batchTriggerDetails.length > 0 && (
          <div className="space-y-2 rounded-lg border border-border-subtle bg-surface-overlay/70 p-3">
            <div className="text-xs font-medium text-txt-primary">Batch triggers</div>
            <div className="flex flex-wrap gap-1.5">
              {batchTriggerDetails.map(item => (
                <div
                  key={item.key}
                  className="inline-flex items-center gap-1.5 rounded-md border border-border-subtle bg-surface-raised px-2 py-1 text-2xs"
                  title={item.value}
                >
                  <span className="font-mono text-txt-primary">{item.key}</span>
                  <span className="rounded-full border border-border-subtle bg-surface-overlay px-1.5 py-0.5 uppercase tracking-[0.12em] text-[10px] text-txt-secondary">
                    {item.kind}
                  </span>
                </div>
              ))}
            </div>
          </div>
        )}
        {previewData?.items && (
          <div className="mt-2 max-h-60 space-y-1 overflow-y-auto">
            {previewData.items.map((item: any) => (
              <div
                key={item.index}
                className="rounded-md border border-border-subtle bg-surface-overlay px-2 py-1 text-2xs font-mono text-txt-secondary"
              >
                #{item.index}: {item.preview}
              </div>
            ))}
            {previewData.count > previewData.items.length && (
              <div className="px-2 text-2xs text-txt-tertiary">
                ...and {previewData.count - previewData.items.length} more
              </div>
            )}
          </div>
        )}
      </ConfirmDialog>
    </div>
  )
}

function FormEditor({
  config,
  columns,
  declaredTypeMap,
  pinnedParams,
  batchParams,
  onTogglePin,
  onChange,
}: {
  config: Record<string, any> | null
  columns: number
  declaredTypeMap: Record<string, keyof typeof PARAM_TYPE_STYLES>
  pinnedParams: string[]
  batchParams: string[]
  onTogglePin: (key: string) => void
  onChange: (data: Record<string, any>) => void
}) {
  const [data, setData] = useState<Record<string, any>>(config || {})

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

  return (
    <div className="h-full overflow-y-auto p-3">
      <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
        {allKeys.map(key => {
          const value = data[key]
          if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
            return (
              <div key={key} className="col-span-full">
                <NestedSection
                  name={key}
                  data={value}
                  columns={columns}
                  declaredTypeMap={declaredTypeMap}
                  pinnedParams={pinnedParams}
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

function NestedSection({
  name,
  data,
  columns,
  declaredTypeMap,
  pinnedParams,
  batchParams,
  prefix,
  onTogglePin,
  onChange,
}: {
  name: string
  data: Record<string, any>
  columns: number
  declaredTypeMap: Record<string, keyof typeof PARAM_TYPE_STYLES>
  pinnedParams: string[]
  batchParams: string[]
  prefix: string
  onTogglePin: (key: string) => void
  onChange: (data: Record<string, any>) => void
}) {
  const [open, setOpen] = useState(true)

  const handleChange = (key: string, value: any) => {
    onChange({ ...data, [key]: value })
  }

  return (
    <div className="overflow-hidden rounded-md border border-border-subtle bg-surface-raised">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-1.5 border-b border-border-subtle px-2.5 py-1.5 text-left transition-colors hover:bg-surface-overlay"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5 text-txt-tertiary" /> : <ChevronRight className="h-3.5 w-3.5 text-txt-tertiary" />}
        <span className="truncate text-sm font-semibold text-txt-primary" title={name}>{name}</span>
        <span className="rounded-full border border-border-subtle px-1.5 py-0.5 text-2xs font-medium text-txt-secondary">
          {Object.keys(data).length}
        </span>
      </button>
      {open && (
        <div className="p-2">
          <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
            {Object.entries(data).filter(([key]) => !key.startsWith('_meta')).map(([key, value]) => {
              const fullKey = `${prefix}.${key}`
              if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
                return (
                  <div key={key} className="col-span-full">
                    <NestedSection
                      name={key}
                      data={value}
                      columns={columns}
                      declaredTypeMap={declaredTypeMap}
                      pinnedParams={pinnedParams}
                      batchParams={batchParams}
                      prefix={fullKey}
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
                  declaredType={declaredTypeMap[fullKey]}
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
  pinned,
  batchActive,
  onChange,
  onTogglePin,
}: {
  name: string
  value: any
  declaredType?: keyof typeof PARAM_TYPE_STYLES
  pinned?: boolean
  batchActive?: boolean
  onChange: (value: any) => void
  onTogglePin: () => void
}) {
  const originalType = declaredType || inferParamType(value)

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
        'flex items-center gap-1.5 rounded-md border px-1.5 py-1 transition-colors',
        pinned ? 'border-accent/20 bg-accent/5' : 'border-border-subtle bg-surface-raised hover:border-border',
        (hasBatch || batchActive) && 'border-amber-500/20 bg-amber-500/5',
      )}
    >
      <button
        type="button"
        onClick={event => {
          event.stopPropagation()
          onTogglePin()
        }}
        title={pinned ? 'Unpin' : 'Pin'}
        className={clsx(
          'flex-none rounded-md p-0.5 transition-colors',
          pinned ? 'text-accent' : 'text-txt-tertiary hover:text-accent'
        )}
      >
        <Pin className="h-3 w-3" />
      </button>

      <span className="max-w-[34%] flex-none truncate text-xs font-semibold text-txt-primary" title={name}>
        {name}
      </span>

      <span className={clsx(
        'flex-none rounded-full border px-1.5 py-0.5 text-[10px] font-mono',
        PARAM_TYPE_STYLES[originalType]
      )}>
        {originalType}
      </span>

      {originalType === 'bool' && !hasBatch ? (
        <div className="ml-auto flex items-center gap-1.5">
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
          <span className="text-xs font-mono text-txt-secondary" title={String(value)}>{String(value)}</span>
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
            'ml-auto min-w-0 flex-1 rounded-md border bg-transparent px-1.5 py-1 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-border',
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
    <div className="h-full p-3">
      <div className="generator-code-editor">
        <CodeMirror
          value={value}
          height="100%"
          theme={theme}
          extensions={extensions}
          onChange={nextValue => onChange(nextValue)}
        />
      </div>
    </div>
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
    <div className="h-full p-3">
      <div className="generator-code-editor">
        <CodeMirror
          value={value}
          height="100%"
          theme={theme}
          extensions={extensions}
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
