import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { useNavigate } from 'react-router-dom'
import {
  AlertTriangle,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CheckCircle2,
  FileCode,
  Hash,
  LayoutGrid,
  ListChecks,
  Loader2,
  Pin,
  Search,
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
import CodeTextEditor from '@/components/shared/CodeTextEditor'
import { PARAM_TYPE_STYLES } from '@/theme/tokens'
import * as api from '@/api'
import type { GeneratorPreview, PreviewItem, ShellRuntimeInfo } from '@/types'

const DEFAULT_SHELL_TEMPLATE = ''
type GenerationStatus = 'idle' | 'previewing' | 'creating' | 'created' | 'error'
type FormLayoutMode = 'grid' | 'tree'

interface CreatedTaskResult {
  count: number
  taskKind: string
  firstTaskName: string
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

function buildColumnGridStyle(columns: number) {
  return { gridTemplateColumns: `repeat(${columns}, minmax(20rem, 1fr))` }
}

function readCompactGeneratorLayout() {
  if (typeof window === 'undefined') {
    return false
  }
  return window.matchMedia('(max-width: 700px)').matches
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

interface TemplateOption {
  value: string
  label: string
}

interface TemplatePickerProps {
  value: string
  options: TemplateOption[]
  placeholder: string
  searchPlaceholder: string
  allowEmpty?: boolean
  onChange: (value: string) => void
}

function TemplatePicker({
  value,
  options,
  placeholder,
  searchPlaceholder,
  allowEmpty = false,
  onChange,
}: TemplatePickerProps) {
  const [open, setOpen] = useState(false)
  const [templateFilter, setTemplateFilter] = useState('')
  const pickerRef = useRef<HTMLDivElement | null>(null)
  const inputRef = useRef<HTMLInputElement | null>(null)
  const selectedOption = options.find(option => option.value === value)
  const buttonLabel = selectedOption?.label || placeholder
  const normalizedFilter = templateFilter.trim().toLowerCase()
  const filteredOptions = useMemo(() => {
    if (!normalizedFilter) return options
    return options.filter(option => `${option.label} ${option.value}`.toLowerCase().includes(normalizedFilter))
  }, [normalizedFilter, options])
  const emptyOptionVisible = allowEmpty && (!normalizedFilter || placeholder.toLowerCase().includes(normalizedFilter))
  const disabled = options.length === 0 && !allowEmpty

  useEffect(() => {
    if (!open) return

    setTemplateFilter('')
    const focusTimer = window.setTimeout(() => inputRef.current?.focus(), 0)
    const handlePointerDown = (event: MouseEvent) => {
      const target = event.target as Node | null
      if (target && !pickerRef.current?.contains(target)) {
        setOpen(false)
      }
    }

    document.addEventListener('mousedown', handlePointerDown)
    return () => {
      window.clearTimeout(focusTimer)
      document.removeEventListener('mousedown', handlePointerDown)
    }
  }, [open])

  const selectValue = useCallback((nextValue: string) => {
    onChange(nextValue)
    setOpen(false)
    setTemplateFilter('')
  }, [onChange])

  return (
    <div
      ref={pickerRef}
      className="relative min-w-[280px]"
      onKeyDown={event => {
        if (event.key === 'Escape') {
          setOpen(false)
        }
      }}
    >
      <button
        type="button"
        disabled={disabled}
        aria-haspopup="listbox"
        aria-expanded={open}
        title={buttonLabel}
        onClick={() => setOpen(current => !current)}
        className="flex h-8 w-full items-center gap-2 rounded-md border border-border-subtle bg-surface-overlay px-3 text-left text-xs text-txt-primary outline-none transition-colors hover:border-border focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/20 disabled:cursor-not-allowed disabled:opacity-50"
      >
        <span className="min-w-0 flex-1 truncate">{buttonLabel}</span>
        <ChevronDown className={clsx('h-3 w-3 flex-none text-txt-tertiary transition-transform', open && 'rotate-180')} />
      </button>

      {open && (
        <div className="absolute left-0 right-0 top-[calc(100%+4px)] z-50 overflow-hidden rounded-md border border-border bg-surface-raised shadow-md">
          <div className="relative border-b border-border-subtle p-2">
            <Search className="pointer-events-none absolute left-4 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
            <input
              ref={inputRef}
              value={templateFilter}
              onChange={event => setTemplateFilter(event.target.value)}
              placeholder={searchPlaceholder}
              aria-label={searchPlaceholder}
              className="h-7 w-full rounded-md border border-border-subtle bg-surface-overlay px-2 pl-7 text-xs text-txt-primary outline-none transition-colors placeholder:text-txt-tertiary focus:border-accent focus:ring-2 focus:ring-accent/15"
            />
          </div>
          <div role="listbox" className="max-h-[min(18rem,50vh)] overflow-y-auto py-1">
            {emptyOptionVisible && (
              <button
                type="button"
                role="option"
                aria-selected={!value}
                onClick={() => selectValue('')}
                className={clsx(
                  'block w-full px-3 py-1.5 text-left text-xs transition-colors hover:bg-surface-overlay',
                  !value ? 'bg-accent/10 text-accent' : 'text-txt-secondary',
                )}
              >
                {placeholder}
              </button>
            )}
            {filteredOptions.length > 0 ? (
              filteredOptions.map(option => (
                <button
                  key={option.value}
                  type="button"
                  role="option"
                  aria-selected={option.value === value}
                  title={option.value}
                  onClick={() => selectValue(option.value)}
                  className={clsx(
                    'block w-full px-3 py-1.5 text-left text-xs transition-colors hover:bg-surface-overlay',
                    option.value === value ? 'bg-accent/10 text-accent' : 'text-txt-primary',
                  )}
                >
                  <span className="block truncate">{option.label}</span>
                </button>
              ))
            ) : (
              <div className="px-3 py-2 text-xs text-txt-tertiary">No matching templates</div>
            )}
          </div>
        </div>
      )}
    </div>
  )
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

interface TreeSectionNode {
  name: string
  path: string
  depth: number
  childCount: number
  leafCount: number
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

function countLeafParams(data: Record<string, any>): number {
  let count = 0
  for (const [key, value] of Object.entries(data)) {
    if (key.startsWith('_meta')) {
      continue
    }
    if (isNestedGroup(value)) {
      count += countLeafParams(value)
    } else {
      count += 1
    }
  }
  return count
}

function collectTreeSections(data: Record<string, any>, prefix = '', depth = 0): TreeSectionNode[] {
  const sections: TreeSectionNode[] = []
  for (const [key, value] of Object.entries(data)) {
    if (key.startsWith('_meta') || !isNestedGroup(value)) {
      continue
    }
    const path = prefix ? `${prefix}.${key}` : key
    const childEntries = Object.entries(value).filter(([childKey]) => !childKey.startsWith('_meta'))
    sections.push({
      name: key,
      path,
      depth,
      childCount: childEntries.length,
      leafCount: countLeafParams(value),
    })
    sections.push(...collectTreeSections(value, path, depth + 1))
  }
  return sections
}

function collectParamRows(
  data: Record<string, any>,
  declaredTypeMap: Record<string, ParamType>,
  batchParams: string[],
  prefix = '',
) {
  const rows: PinnedParamRow[] = []
  const batchSet = new Set(batchParams)

  for (const [key, value] of Object.entries(data)) {
    if (key.startsWith('_meta')) {
      continue
    }
    const fullKey = prefix ? `${prefix}.${key}` : key
    if (isNestedGroup(value)) {
      rows.push(...collectParamRows(value, declaredTypeMap, batchParams, fullKey))
      continue
    }
    rows.push({
      name: key,
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
  const [compactGeneratorLayout, setCompactGeneratorLayout] = useState(readCompactGeneratorLayout)
  const lastWorkspaceDefaultKeyRef = useRef('')
  const lastShellRootRef = useRef('')

  const isShellWorkspace = workspace?.workspace_kind === 'shell'
  const shellRuntime = workspace?.shell_runtime
  const editorMode = isShellWorkspace ? 'shell' : viewMode === 'shell' ? 'form' : viewMode
  const codeMirrorTheme = theme === 'dark' ? 'dark' : 'light'
  const generatorBodyClassName = clsx(
    'flex min-h-0 flex-1',
    compactGeneratorLayout ? 'flex-col overflow-y-auto' : 'overflow-hidden',
  )
  const generatorEditorClassName = clsx(
    'min-w-0 flex flex-col overflow-hidden',
    compactGeneratorLayout ? 'min-h-[20rem] flex-none' : 'flex-1',
  )
  const generatorSettingsClassName = clsx(
    'flex flex-col gap-2.5 overflow-y-auto bg-surface-raised p-2.5',
    compactGeneratorLayout ? 'w-full flex-none border-t border-border-subtle' : 'w-[286px] border-l border-border-subtle',
  )

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
    if (typeof window === 'undefined') {
      return
    }

    const query = window.matchMedia('(max-width: 700px)')
    const handleChange = () => setCompactGeneratorLayout(query.matches)
    handleChange()
    query.addEventListener('change', handleChange)
    return () => query.removeEventListener('change', handleChange)
  }, [])

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
            <TemplatePicker
              value={selectedTemplate}
              options={templates}
              placeholder="Select template"
              searchPlaceholder="Search templates"
              onChange={value => void loadTemplate(value)}
            />
          ) : (
            <>
              <TemplatePicker
                value={shellTemplateSelectValue}
                options={templates}
                placeholder="Load task or script"
                searchPlaceholder="Search tasks or scripts"
                allowEmpty
                onChange={value => {
                  if (value) {
                    void loadTemplate(value)
                  } else {
                    clearTemplate()
                  }
                }}
              />
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
              className="inline-flex min-w-0 max-w-full select-text items-start gap-1 rounded-md border border-border-subtle bg-surface-overlay px-2 py-1 text-2xs text-txt-secondary"
              title={configDefaultSourcePath || configDefaultSourceName}
            >
              <FileCode className="mt-0.5 h-3 w-3 flex-none text-accent" />
              <span className="flex-none">Loaded from</span>
              <span className="min-w-0 whitespace-normal break-all font-mono text-txt-primary">{configDefaultSourceName}</span>
            </span>
          )}

          {isShellWorkspace && templateContent?.mode_hint === 'shell' && (
            <span
              className="inline-flex min-w-0 max-w-full select-text items-start gap-1 rounded-md border border-border-subtle bg-surface-overlay px-2 py-1 text-2xs text-txt-secondary"
              title={templateContent.path}
            >
              <FileCode className="mt-0.5 h-3 w-3 flex-none text-accent" />
              <span className="flex-none">Loaded shell</span>
              <span className="min-w-0 whitespace-normal break-all font-mono text-txt-primary">{templateContent.label}</span>
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

          {editorMode === 'form' && (formLayoutMode === 'tree' || formLayoutMode === 'grid') && (
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

      <div className={generatorBodyClassName}>
        <div className={generatorEditorClassName} style={compactGeneratorLayout ? undefined : { flexBasis: '78%' }}>
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
            <div className="h-full p-3">
              <CodeTextEditor
                language="yaml"
                value={yamlText}
                onChange={setYamlText}
                theme={codeMirrorTheme}
                className="generator-code-editor"
                wrapStorageKey="pyruns.generator.yaml.wrap"
              />
            </div>
          ) : (
            <div className="h-full p-3">
              <CodeTextEditor
                language="shell"
                value={shellText}
                onChange={setShellText}
                theme={codeMirrorTheme}
                className="generator-code-editor"
                wrapStorageKey="pyruns.generator.shell.wrap"
              />
            </div>
          )}
        </div>

        <aside
          className={generatorSettingsClassName}
          style={compactGeneratorLayout ? undefined : { minWidth: 268, maxWidth: 296 }}
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

  if (layoutMode === 'tree') {
    return (
      <TreeParameterExplorer
        data={data}
        openSignalValue={openSignalValue}
        openSignalVersion={openSignalVersion}
        declaredTypeMap={declaredTypeMap}
        pinnedRows={pinnedRows}
        pinnedParams={pinnedParams}
        pinnedRowKeys={pinnedRowKeys}
        batchParams={batchParams}
        onTogglePin={onTogglePin}
        onChangeRoot={handleChange}
        onChangePath={handlePinnedChange}
      />
    )
  }

  const visibleKeys = allKeys.filter(key => !key.startsWith('_meta') && !pinnedRowKeys.has(key))
  const gridStyle = buildColumnGridStyle(columns)
  const contentStyle = gridStyle
  const contentClassName = 'grid gap-x-3 gap-y-2.5 overflow-x-auto pb-1'
  const childSectionClassName = 'col-span-full'

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
        {visibleKeys.map(key => {
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

function TreeParameterExplorer({
  data,
  openSignalValue,
  openSignalVersion,
  declaredTypeMap,
  pinnedRows,
  pinnedParams,
  pinnedRowKeys,
  batchParams,
  onTogglePin,
  onChangeRoot,
  onChangePath,
}: {
  data: Record<string, any>
  openSignalValue: boolean
  openSignalVersion: number
  declaredTypeMap: Record<string, keyof typeof PARAM_TYPE_STYLES>
  pinnedRows: PinnedParamRow[]
  pinnedParams: string[]
  pinnedRowKeys: Set<string>
  batchParams: string[]
  onTogglePin: (key: string) => void
  onChangeRoot: (key: string, value: any) => void
  onChangePath: (fullKey: string, value: any) => void
}) {
  const [selectedPath, setSelectedPath] = useState('')
  const [filterText, setFilterText] = useState('')
  const [outlineCollapsed, setOutlineCollapsed] = useState(false)
  const outlineSections = useMemo(() => {
    const rootEntries = Object.keys(data).filter(key => !key.startsWith('_meta'))
    return [
      {
        name: 'All parameters',
        path: '',
        depth: -1,
        childCount: rootEntries.length,
        leafCount: countLeafParams(data),
      },
      ...collectTreeSections(data),
    ]
  }, [data])
  const outlinePathSet = useMemo(() => new Set(outlineSections.map(section => section.path)), [outlineSections])
  const query = filterText.trim().toLowerCase()
  const searchRows = useMemo(() => {
    if (!query) {
      return [] as PinnedParamRow[]
    }
    return collectParamRows(data, declaredTypeMap, batchParams).filter(row => {
      const pathText = row.fullKey.toLowerCase()
      const valueText = stringifyEditable(row.value).toLowerCase()
      return pathText.includes(query) || valueText.includes(query)
    })
  }, [batchParams, data, declaredTypeMap, query])
  const selectedSection = outlineSections.find(section => section.path === selectedPath) || outlineSections[0]
  const selectedValue = selectedPath ? getValueAtPath(data, selectedPath) : data
  const selectedData = isNestedGroup(selectedValue) ? selectedValue : data
  const selectedCrumbs = selectedPath ? selectedPath.split('.') : ['config']

  useEffect(() => {
    if (!outlinePathSet.has(selectedPath)) {
      setSelectedPath('')
    }
  }, [outlinePathSet, selectedPath])

  return (
    <div className={clsx(
      'grid h-full min-h-0 p-3',
      outlineCollapsed ? 'grid-cols-[minmax(0,1fr)]' : 'grid-cols-[minmax(220px,260px)_minmax(0,1fr)] gap-3',
    )}>
      {!outlineCollapsed && (
      <aside className="flex min-h-0 flex-col overflow-hidden rounded-md border border-border-subtle bg-surface-raised/85">
        <div className="border-b border-border-subtle p-2">
          <div className="mb-2 flex items-center justify-between gap-2">
            <span className="text-2xs font-bold uppercase tracking-[0.16em] text-txt-tertiary">Outline</span>
            <button
              type="button"
              onClick={() => setOutlineCollapsed(true)}
              className="inline-flex h-6 items-center gap-1 rounded-md px-2 text-2xs font-medium text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary"
            >
              <ChevronLeft className="h-3 w-3" />
              Hide
            </button>
          </div>
          <div className="relative">
            <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-txt-tertiary" />
            <input
              value={filterText}
              onChange={event => setFilterText(event.target.value)}
              placeholder="Search path or value"
              className="w-full rounded-md border border-border-subtle bg-surface-overlay py-1.5 pl-7 pr-2 text-xs text-txt-primary outline-none transition-colors placeholder:text-txt-tertiary focus:border-border"
            />
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
          <div className="space-y-0.5">
            {outlineSections.map(section => {
              const active = !query && selectedPath === section.path
              const indent = section.path ? 10 + section.depth * 14 : 8
              return (
                <button
                  key={section.path || '__root__'}
                  type="button"
                  onClick={() => {
                    setFilterText('')
                    setSelectedPath(section.path)
                  }}
                  title={section.path || 'All parameters'}
                  className={clsx(
                    'flex w-full min-w-0 items-center gap-1.5 rounded-md py-1.5 pr-2 text-left text-xs transition-colors',
                    active
                      ? 'bg-accent/10 text-accent'
                      : 'text-txt-secondary hover:bg-surface-overlay hover:text-txt-primary',
                  )}
                  style={{ paddingLeft: `${indent}px` }}
                >
                  {section.path ? (
                    <ChevronRight className="h-3 w-3 flex-none text-txt-tertiary" />
                  ) : (
                    <Workflow className="h-3 w-3 flex-none" />
                  )}
                  <span className="min-w-0 flex-1 truncate font-semibold">{section.name}</span>
                  <span className="rounded-md bg-surface-overlay px-1.5 py-0.5 text-2xs text-txt-tertiary">
                    {section.leafCount}
                  </span>
                </button>
              )
            })}
          </div>
        </div>
      </aside>
      )}

      <section className="flex min-h-0 min-w-0 flex-col overflow-hidden rounded-md border border-border-subtle bg-surface-raised/50">
        <div className="border-b border-border-subtle bg-surface-raised/80 px-3 py-2">
          <div className="flex min-w-0 items-center gap-2">
            {outlineCollapsed && (
              <button
                type="button"
                onClick={() => setOutlineCollapsed(false)}
                className="inline-flex h-7 items-center gap-1 rounded-md border border-border-subtle bg-surface-overlay px-2 text-xs font-medium text-txt-secondary transition-colors hover:text-txt-primary"
              >
                <Workflow className="h-3.5 w-3.5" />
                Outline
              </button>
            )}
            <h3 className="min-w-0 truncate text-sm font-semibold text-txt-primary">
              {query ? 'Search results' : selectedSection.name}
            </h3>
            <span className="rounded-md bg-surface-overlay px-1.5 py-0.5 text-2xs text-txt-secondary">
              {query ? searchRows.length : selectedSection.leafCount}
            </span>
          </div>
          <div className="mt-1 flex min-w-0 flex-wrap items-center gap-1 text-2xs text-txt-tertiary">
            {(query ? ['filter', filterText.trim()] : selectedCrumbs).filter(Boolean).map((crumb, index) => (
              <span key={`${crumb}-${index}`} className="font-mono">
                {index > 0 ? `/ ${crumb}` : crumb}
              </span>
            ))}
          </div>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-2.5">
          {pinnedRows.length > 0 && !query && (
            <PinnedParameters
              rows={pinnedRows}
              columns={1}
              layoutMode="tree"
              onTogglePin={onTogglePin}
              onChange={onChangePath}
            />
          )}

          {query ? (
            <SearchResultRows
              rows={searchRows}
              pinnedParams={pinnedParams}
              onTogglePin={onTogglePin}
              onChange={onChangePath}
            />
          ) : selectedPath ? (
            <NestedSection
              name={selectedSection.name}
              data={selectedData}
              depth={0}
              columns={1}
              layoutMode="tree"
              openSignalValue={openSignalValue}
              openSignalVersion={openSignalVersion}
              declaredTypeMap={declaredTypeMap}
              pinnedParams={pinnedParams}
              pinnedRowKeys={pinnedRowKeys}
              batchParams={batchParams}
              prefix={selectedPath}
              onTogglePin={onTogglePin}
              onChange={next => onChangePath(selectedPath, next)}
            />
          ) : (
            <div className="space-y-4">
              {Object.entries(data).filter(([key, value]) => (
                !key.startsWith('_meta') && !isNestedGroup(value) && !pinnedRowKeys.has(key)
              )).length > 0 && (
                <div className="space-y-2.5 rounded-md border border-border-subtle bg-surface-raised/40 p-3 shadow-sm">
                  <div className="flex items-center gap-1.5 text-2xs font-bold uppercase tracking-[0.16em] text-txt-tertiary">
                    <Workflow className="h-3.5 w-3.5" />
                    <span>Global Parameters</span>
                  </div>
                  <div className="space-y-1.5">
                    {Object.entries(data).filter(([key, value]) => (
                      !key.startsWith('_meta') && !isNestedGroup(value) && !pinnedRowKeys.has(key)
                    )).map(([key, value]) => (
                      <ParamRow
                        key={key}
                        name={key}
                        value={value}
                        declaredType={declaredTypeMap[key]}
                        layoutMode="tree"
                        pinned={pinnedParams.includes(key)}
                        batchActive={batchParams.includes(key)}
                        onChange={next => onChangeRoot(key, next)}
                        onTogglePin={() => onTogglePin(key)}
                      />
                    ))}
                  </div>
                </div>
              )}

              {outlineSections.filter(section => section.path && section.depth === 0).map(section => {
                const sectionData = getValueAtPath(data, section.path)
                return (
                  <NestedSection
                    key={section.path}
                    name={section.name}
                    data={sectionData}
                    depth={0}
                    columns={1}
                    layoutMode="tree"
                    openSignalValue={openSignalValue}
                    openSignalVersion={openSignalVersion}
                    declaredTypeMap={declaredTypeMap}
                    pinnedParams={pinnedParams}
                    pinnedRowKeys={pinnedRowKeys}
                    batchParams={batchParams}
                    prefix={section.path}
                    onTogglePin={onTogglePin}
                    onChange={next => onChangePath(section.path, next)}
                  />
                )
              })}
            </div>
          )}
        </div>
      </section>
    </div>
  )
}

function RootSectionOverview({
  data,
  sections,
  declaredTypeMap,
  pinnedParams,
  pinnedRowKeys,
  batchParams,
  onTogglePin,
  onChangeRoot,
  onSelectPath,
}: {
  data: Record<string, any>
  sections: TreeSectionNode[]
  declaredTypeMap: Record<string, keyof typeof PARAM_TYPE_STYLES>
  pinnedParams: string[]
  pinnedRowKeys: Set<string>
  batchParams: string[]
  onTogglePin: (key: string) => void
  onChangeRoot: (key: string, value: any) => void
  onSelectPath: (path: string) => void
}) {
  const scalarEntries = Object.entries(data).filter(([key, value]) => (
    !key.startsWith('_meta') && !isNestedGroup(value) && !pinnedRowKeys.has(key)
  ))

  return (
    <div className="space-y-3">
      {scalarEntries.length > 0 && (
        <div className="grid gap-2" style={buildColumnGridStyle(1)}>
          {scalarEntries.map(([key, value]) => (
            <ParamRow
              key={key}
              name={key}
              value={value}
              declaredType={declaredTypeMap[key]}
              layoutMode="tree"
              pinned={pinnedParams.includes(key)}
              batchActive={batchParams.includes(key)}
              onChange={next => onChangeRoot(key, next)}
              onTogglePin={() => onTogglePin(key)}
            />
          ))}
        </div>
      )}

      {sections.length > 0 && (
        <div>
          <div className="mb-2 flex items-center gap-1.5 text-2xs font-bold uppercase tracking-[0.16em] text-txt-tertiary">
            <Workflow className="h-3.5 w-3.5" />
            <span>Sections</span>
          </div>
          <div className="space-y-2">
            {sections.map(section => (
              <button
                key={section.path}
                type="button"
                onClick={() => onSelectPath(section.path)}
                className="group block w-full min-w-0 rounded-md border border-border bg-surface-raised px-3 py-2 text-left shadow-sm transition-colors hover:border-accent/40 hover:bg-surface-overlay"
              >
                <div className="flex min-w-0 items-center gap-2">
                  <ChevronRight className="h-3.5 w-3.5 flex-none text-txt-tertiary transition-transform group-hover:translate-x-0.5" />
                  <span className="min-w-0 flex-1 truncate text-sm font-semibold text-txt-primary">
                    {section.name}
                  </span>
                  <span className="rounded-md bg-surface-overlay px-1.5 py-0.5 text-2xs text-txt-secondary">
                    {section.leafCount}
                  </span>
                </div>
                <div className="mt-1 truncate font-mono text-2xs text-txt-tertiary" title={section.path}>
                  {section.path}
                </div>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

function SearchResultRows({
  rows,
  pinnedParams,
  onTogglePin,
  onChange,
}: {
  rows: PinnedParamRow[]
  pinnedParams: string[]
  onTogglePin: (key: string) => void
  onChange: (fullKey: string, value: any) => void
}) {
  if (rows.length === 0) {
    return (
      <div className="rounded-md border border-dashed border-border-subtle bg-surface-overlay/40 px-3 py-8 text-center text-sm text-txt-tertiary">
        No matching parameters.
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {rows.map(row => (
        <div key={row.fullKey} className="rounded-md border border-border-subtle bg-surface-raised p-1.5">
          <div className="mb-1 truncate px-1 font-mono text-2xs text-txt-tertiary" title={row.fullKey}>
            {row.fullKey}
          </div>
          <ParamRow
            name={row.name}
            value={row.value}
            declaredType={row.declaredType}
            layoutMode="tree"
            pinned={pinnedParams.includes(row.fullKey)}
            batchActive={row.batchActive}
            onChange={next => onChange(row.fullKey, next)}
            onTogglePin={() => onTogglePin(row.fullKey)}
          />
        </div>
      ))}
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
  const gridStyle = buildColumnGridStyle(columns)
  const contentStyle = gridStyle
  const contentClassName = 'grid gap-2 overflow-x-auto pb-1'

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
  const effectiveColumns = Math.max(1, columns)
  const gridStyle = buildColumnGridStyle(effectiveColumns)
  const contentStyle = layoutMode === 'tree' ? undefined : gridStyle
  const contentClassName = layoutMode === 'tree' ? 'space-y-1.5' : 'grid gap-x-3 gap-y-2.5 overflow-x-auto pb-1'
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
          ? 'overflow-hidden rounded-md border border-transparent bg-transparent'
          : 'relative',
        treeSection && depth === 0 && 'border-border bg-surface-raised shadow-sm',
        treeSection && depth > 0 && 'rounded-none',
        !treeSection && depth === 0 && 'overflow-hidden rounded-md border border-border-subtle bg-surface-raised shadow-sm',
        !treeSection && depth > 0 && 'border-l-2 border-border-subtle pl-3',
      )}
    >
      {treeConnector && (
        <div className="pointer-events-none absolute bottom-0 left-0 top-4 border-l border-dashed border-border-strong/60" />
      )}
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className={clsx(
          'relative flex min-h-8 w-full items-center gap-1.5 text-left transition-colors group',
          treeSection
            ? 'px-2.5 py-1.5 hover:bg-surface-overlay/70 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/25'
            : 'px-2.5 py-1.5 hover:bg-surface-overlay',
          treeSection && depth === 0 && open && 'border-b border-border bg-surface-overlay/50',
          treeSection && depth > 0 && 'rounded-md',
          !treeSection && depth === 0 && open && 'border-b border-border-subtle bg-surface-overlay/55',
          !treeSection && depth > 0 && 'rounded-md bg-surface-overlay/25',
        )}
        title={`${prefix} (${Object.keys(data).length} fields)`}
      >
        {treeConnector && (
          <span className="absolute left-0 top-1/2 w-2 border-t border-dashed border-border-strong/60" />
        )}
        {open ? <ChevronDown className="h-3.5 w-3.5 text-txt-tertiary group-hover:text-accent transition-colors" /> : <ChevronRight className="h-3.5 w-3.5 text-txt-tertiary group-hover:text-accent transition-colors" />}
        <span className="truncate text-sm font-semibold text-txt-primary group-hover:text-accent transition-colors" title={name}>{name}</span>
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
        <div className={treeSection ? 'ml-4 border-l border-dashed border-border-strong/60 pb-1 pl-4 pt-1' : (depth === 0 ? 'p-2.5' : 'pb-2 pl-3 pt-1.5')}>
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

  if (!treeParamRow) {
    return (
      <div
        className={clsx(
          'group grid min-h-10 grid-cols-[minmax(9rem,0.75fr)_auto_minmax(12rem,1.25fr)] items-center gap-2 rounded-md border border-border-subtle bg-surface-raised/40 px-2.5 py-1.5 shadow-sm transition-all hover:border-border hover:bg-surface-overlay/30 focus-within:border-accent/60 focus-within:bg-surface-raised focus-within:ring-2 focus-within:ring-accent/15',
          pinned ? 'border-l-2 border-l-accent border-y-accent/20 border-r-accent/20 bg-accent/[0.03] ring-1 ring-accent/20' : '',
          (hasBatch || batchActive) && 'border-l-2 border-l-amber-500 border-y-amber-500/20 border-r-amber-500/20 bg-amber-500/[0.03] ring-1 ring-amber-500/20',
        )}
      >
        <div className="flex min-w-0 items-center gap-1.5">
          <button
            type="button"
            onClick={event => {
              event.stopPropagation()
              onTogglePin()
            }}
            title={pinned ? 'Unpin' : 'Pin'}
            aria-label={pinned ? `Unpin ${name}` : `Pin ${name}`}
            className={clsx(
              'flex h-5 w-5 flex-none items-center justify-center rounded transition-colors',
              pinned ? 'text-accent' : 'text-txt-tertiary hover:text-accent'
            )}
          >
            <Pin className="h-3 w-3" />
          </button>

          <span className="truncate text-xs font-semibold text-txt-primary" title={name}>
            {name}
          </span>
        </div>

        <div className="flex flex-none items-center justify-end gap-1">
          {pinned && (
            <span className="rounded-md bg-accent/10 px-1 py-0.2 text-[8px] font-bold uppercase tracking-wider text-accent">
              Pin
            </span>
          )}

          {(hasBatch || batchActive) && (
            <span className="rounded-md bg-amber-500/10 px-1 py-0.2 text-[8px] font-bold uppercase tracking-wider text-amber-500">
              Batch
            </span>
          )}

          <span className={clsx(
            'rounded-md px-1.5 py-0.5 text-[10px] font-mono',
            PARAM_TYPE_STYLES[originalType]
          )}>
            {originalType}
          </span>
        </div>

        <div className="min-w-0 w-full">
          {originalType === 'bool' && !hasBatch ? (
            <div
              className={clsx(
                'flex h-7 w-full items-center justify-between gap-2 rounded-md border bg-surface-overlay/45 px-2 transition-colors focus-within:border-accent focus-within:bg-surface-raised focus-within:ring-2 focus-within:ring-accent/15',
                batchActive ? 'border-amber-500/20' : 'border-border-subtle',
              )}
            >
              <span className="min-w-0 truncate text-xs font-mono text-txt-secondary" title={String(value)}>{String(value)}</span>
              <button
                type="button"
                onClick={() => onChange(!value)}
                title={`Toggle ${name}`}
                className={clsx(
                  'relative h-4.5 w-9 flex-none rounded-full outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent/30',
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
                'h-7 w-full rounded-md border bg-surface-overlay/45 px-2 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-accent focus:bg-surface-raised focus:ring-2 focus:ring-accent/15',
                hasBatch || batchActive ? 'border-amber-500/20' : 'border-border-subtle',
              )}
            />
          )}
        </div>
      </div>
    )
  }

  return (
    <div
      className={clsx(
        'grid min-h-10 grid-cols-[24px_minmax(150px,0.95fr)_minmax(150px,1.05fr)] min-w-0 items-center gap-2 rounded-md border border-border-subtle bg-surface-raised px-2.5 py-1.5 shadow-sm transition-colors hover:border-border hover:bg-surface-overlay focus-within:border-accent/60 focus-within:bg-surface-raised focus-within:ring-2 focus-within:ring-accent/20',
        pinned && 'border-l-2 border-l-accent border-y-accent/20 border-r-accent/20 bg-accent/[0.04]',
        (hasBatch || batchActive) && 'border-l-2 border-l-amber-500 border-y-amber-500/20 border-r-amber-500/20 bg-amber-500/[0.04]',
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

      <div className={clsx('flex min-w-0 items-center gap-1.5', treeParamRow ? 'min-w-0' : 'flex-1')}>
        <span className="min-w-0 truncate text-xs font-semibold text-txt-primary" title={name}>
          {name}
        </span>

        {pinned && (
          <span className="flex-none rounded-md bg-accent/10 px-1 py-0.2 text-[8px] font-bold uppercase tracking-wider text-accent">
            Pinned
          </span>
        )}

        {(hasBatch || batchActive) && (
          <span className="flex-none rounded-md bg-amber-500/10 px-1 py-0.2 text-[8px] font-bold uppercase tracking-wider text-amber-500">
            Batch
          </span>
        )}

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
            'flex min-w-0 items-center gap-1.5',
            treeParamRow ? 'min-w-0 justify-start' : 'flex-none justify-end',
          )}
        >
          <button
            type="button"
            onClick={() => onChange(!value)}
            title={`Toggle ${name}`}
            className={clsx(
              'relative h-4.5 w-9 rounded-full outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent/30',
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
            'rounded-md border bg-transparent px-1.5 py-1 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-accent focus:bg-surface-overlay/45 focus:ring-2 focus:ring-accent/15',
            treeParamRow ? 'min-w-0 w-full' : 'ml-auto min-w-0 flex-1',
            hasBatch || batchActive ? 'border-amber-500/20' : 'border-border-subtle',
          )}
        />
      )}
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
