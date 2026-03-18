import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle, ChevronDown, ChevronRight, FileCode, LayoutGrid, Pin, Sparkles,
  Terminal as TermIcon,
} from 'lucide-react'
import clsx from 'clsx'
import { parse as yamlParse, stringify as yamlStringify } from 'yaml'
import { useGeneratorStore, useWorkspaceStore } from '@/store'
import EmptyState from '@/components/shared/EmptyState'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import ActionButton from '@/components/shared/ActionButton'
import CompactSection from '@/components/shared/CompactSection'
import * as api from '@/api'

export default function GeneratorPage() {
  const workspace = useWorkspaceStore(state => state.workspace)
  const {
    templates, selectedTemplate, templateContent, viewMode, yamlText, argsText, runScript,
    namePrefix, appendTimestamp, pinnedParams, loading,
    fetchTemplates, loadTemplate, setViewMode, setYamlText, setArgsText, setRunScript,
    setNamePrefix, setAppendTimestamp, togglePin,
  } = useGeneratorStore()

  const [columns, setColumns] = useState(5)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewData, setPreviewData] = useState<any>(null)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  useEffect(() => {
    void fetchTemplates()
  }, [workspace?.run_root, fetchTemplates])

  useEffect(() => {
    if (templates.length > 0 && !selectedTemplate) {
      void loadTemplate(templates[0].value)
    }
  }, [templates, selectedTemplate, loadTemplate])

  const parsedConfig = useMemo(() => {
    if (viewMode === 'args') return null
    try {
      return (yamlParse(yamlText) as Record<string, any>) || {}
    } catch {
      return null
    }
  }, [yamlText, viewMode])

  const batchParams = useMemo(() => {
    if (!parsedConfig) return [] as string[]
    const result: string[] = []

    const walk = (obj: Record<string, any>, prefix = '') => {
      for (const [key, value] of Object.entries(obj)) {
        const fullKey = prefix ? `${prefix}.${key}` : key
        if (
          typeof value === 'string'
          && (value.includes('|') || /^\s*-?\d+\s*:\s*-?\d+(?:\s*:\s*-?\d+)?\s*$/.test(value.trim()))
        ) {
          result.push(fullKey)
        } else if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
          walk(value, fullKey)
        }
      }
    }

    walk(parsedConfig)
    return result
  }, [parsedConfig])

  const hasBatchSyntax = viewMode === 'args'
    ? argsText.includes('|') || /-?\d+\s*:\s*-?\d+(?:\s*:\s*-?\d+)?/.test(argsText)
    : yamlText.includes('|') || /-?\d+\s*:\s*-?\d+(?:\s*:\s*-?\d+)?/.test(yamlText)

  const doCreate = useCallback(async () => {
    setGenerating(true)
    setError('')
    setSuccess('')
    try {
      const result = await api.createTasks({
        name_prefix: namePrefix || 'task',
        run_mode: viewMode === 'args' ? 'args' : 'yaml',
        yaml_text: viewMode !== 'args' ? yamlText : '',
        args_text: viewMode === 'args' ? argsText : '',
        run_script: viewMode === 'args' ? runScript : '',
        template_value: selectedTemplate,
        append_timestamp: appendTimestamp,
      })
      setPreviewOpen(false)
      setSuccess(`Created ${result.count} task(s)`)
      void fetchTemplates()
      setTimeout(() => setSuccess(''), 4000)
    } catch (err: any) {
      setError(err.message)
    } finally {
      setGenerating(false)
    }
  }, [
    viewMode, yamlText, argsText, runScript, namePrefix,
    appendTimestamp, selectedTemplate, fetchTemplates,
  ])

  const handleGenerate = useCallback(async () => {
    setError('')
    setSuccess('')

    if (hasBatchSyntax && !previewOpen) {
      try {
        const preview = await api.previewTasks({
          run_mode: viewMode === 'args' ? 'args' : 'yaml',
          yaml_text: viewMode !== 'args' ? yamlText : '',
          args_text: viewMode === 'args' ? argsText : '',
          run_script: viewMode === 'args' ? runScript : '',
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
  }, [viewMode, yamlText, argsText, runScript, selectedTemplate, hasBatchSyntax, previewOpen, doCreate])

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-border-subtle bg-surface-raised px-3 py-2">
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

        {viewMode === 'form' && (
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

        {templateContent?.read_only && (
          <span className="inline-flex items-center gap-1 rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-1 text-2xs text-amber-400">
            <AlertTriangle className="h-3 w-3" />
            <span>Read-only</span>
          </span>
        )}

        <div className="flex-1" />

        <div className="flex items-center rounded-lg border border-border-subtle bg-surface-overlay p-0.5">
          {(['form', 'yaml', 'args'] as const).map(mode => (
            <button
              key={mode}
              type="button"
              onClick={() => setViewMode(mode)}
              className={clsx(
                'inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-colors',
                viewMode === mode
                  ? 'bg-surface-raised text-txt-primary'
                  : 'text-txt-secondary hover:text-txt-primary'
              )}
            >
              {mode === 'form' && <LayoutGrid className="h-3 w-3" />}
              {mode === 'yaml' && <FileCode className="h-3 w-3" />}
              {mode === 'args' && <TermIcon className="h-3 w-3" />}
              <span>{mode.charAt(0).toUpperCase() + mode.slice(1)}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="flex min-h-0 flex-1 overflow-hidden">
        <div className="min-w-0 flex-1 overflow-y-auto" style={{ flexBasis: '78%' }}>
          {loading ? (
            <div className="flex h-full items-center justify-center">
              <div className="animate-pulse text-xs text-txt-tertiary">Loading template...</div>
            </div>
          ) : viewMode === 'form' ? (
            <FormEditor
              config={parsedConfig}
              columns={columns}
              pinnedParams={pinnedParams}
              batchParams={batchParams}
              onTogglePin={togglePin}
              onChange={data => setYamlText(yamlStringify(data))}
            />
          ) : viewMode === 'yaml' ? (
            <YamlEditor value={yamlText} onChange={setYamlText} />
          ) : (
            <ArgsEditor
              argsText={argsText}
              runScript={runScript}
              onArgsChange={setArgsText}
              onRunScriptChange={setRunScript}
              scriptPath={workspace?.script_path || ''}
            />
          )}
        </div>

        <aside
          className="flex w-[296px] flex-col gap-3 overflow-y-auto border-l border-border-subtle bg-surface-raised p-3"
          style={{ minWidth: 280, maxWidth: 304 }}
        >
          <CompactSection title="Naming" bodyClassName="space-y-3 p-2.5">
            <div>
              <label className="block text-2xs uppercase tracking-[0.16em] text-txt-tertiary">Task Prefix</label>
              <input
                value={namePrefix}
                onChange={event => setNamePrefix(event.target.value)}
                placeholder="task"
                className="mt-2 w-full rounded-md border border-border-subtle bg-surface-overlay px-3 py-2 text-sm font-medium text-txt-primary outline-none transition-colors focus:border-border"
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

          {batchParams.length > 0 && (
            <CompactSection
              title="Pinned Batch Inputs"
              icon={<Pin className="h-3.5 w-3.5 text-accent" />}
              accent
              bodyClassName="space-y-2 p-2.5"
            >
              <div className="text-2xs text-accent">
                {batchParams.length} batch-sensitive field{batchParams.length > 1 ? 's' : ''}
              </div>
              <div className="flex flex-wrap gap-1.5">
                {batchParams.map(param => (
                  <span
                    key={param}
                    className="rounded-md border border-accent/20 bg-accent/8 px-2 py-1 font-mono text-2xs text-accent"
                    title={param}
                  >
                    {param}
                  </span>
                ))}
              </div>
            </CompactSection>
          )}

          <CompactSection title="Batch Syntax" bodyClassName="space-y-2 p-2.5">
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

          {(error || success) && (
            <CompactSection title="Status" bodyClassName="space-y-2 p-2.5">
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
              {generating ? 'Generating...' : 'Generate Tasks'}
            </ActionButton>
            <div className="mt-2 text-center text-2xs text-txt-tertiary">
              {hasBatchSyntax ? 'Preview opens automatically for batch generation.' : 'Creates tasks immediately.'}
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
  pinnedParams,
  batchParams,
  onTogglePin,
  onChange,
}: {
  config: Record<string, any> | null
  columns: number
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
  const pinned = pinnedParams.filter(key => key in data)
  const unpinned = allKeys.filter(key => !pinnedParams.includes(key))

  const handleChange = (key: string, value: any) => {
    const next = { ...data, [key]: value }
    setData(next)
    onChange(next)
  }

  return (
    <div className="p-3">
      {pinned.length > 0 && (
        <CompactSection
          title="Pinned"
          subtitle={`${pinned.length} parameter${pinned.length > 1 ? 's' : ''}`}
          icon={<Pin className="h-3.5 w-3.5 text-accent" />}
          accent
          className="mb-3"
          bodyClassName="p-2"
        >
          <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
            {pinned.map(key => (
              <ParamRow
                key={key}
                name={key}
                value={data[key]}
                pinned
                batchActive={batchParams.includes(key)}
                onChange={value => handleChange(key, value)}
                onTogglePin={() => onTogglePin(key)}
              />
            ))}
          </div>
        </CompactSection>
      )}

      <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
        {unpinned.map(key => {
          const value = data[key]
          if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
            return (
              <div key={key} className="col-span-full">
                <NestedSection
                  name={key}
                  data={value}
                  columns={columns}
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
              pinned={false}
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
  pinnedParams,
  batchParams,
  prefix,
  onTogglePin,
  onChange,
}: {
  name: string
  data: Record<string, any>
  columns: number
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
        className="flex w-full items-center gap-2 border-b border-border-subtle px-3 py-2 text-left transition-colors hover:bg-surface-overlay"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5 text-txt-tertiary" /> : <ChevronRight className="h-3.5 w-3.5 text-txt-tertiary" />}
        <span className="truncate text-sm font-medium text-txt-primary" title={name}>{name}</span>
        <span className="rounded-full border border-border-subtle px-2 py-0.5 text-2xs text-txt-tertiary">
          {Object.keys(data).length}
        </span>
      </button>
      {open && (
        <div className="p-2.5">
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
  pinned,
  batchActive,
  onChange,
  onTogglePin,
}: {
  name: string
  value: any
  pinned?: boolean
  batchActive?: boolean
  onChange: (value: any) => void
  onTogglePin: () => void
}) {
  const originalType = value === null || value === undefined ? 'null'
    : typeof value === 'boolean' ? 'bool'
    : typeof value === 'number' ? (Number.isInteger(value) ? 'int' : 'float')
    : Array.isArray(value) ? 'list'
    : 'str'

  const [localValue, setLocalValue] = useState(stringifyEditable(value))

  useEffect(() => {
    setLocalValue(stringifyEditable(value))
  }, [value])

  const hasBatch = localValue.includes('|') || /^\s*-?\d+\s*:\s*-?\d+(?:\s*:\s*-?\d+)?\s*$/.test(localValue.trim())

  const commitValue = () => {
    const next = localValue.trim()
    if (originalType === 'bool' && !hasBatch) {
      if (next === 'true' || next === 'True' || next === '1') { onChange(true); return }
      if (next === 'false' || next === 'False' || next === '0') { onChange(false); return }
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
    <div className={clsx(
      'flex items-center gap-1.5 rounded-md border px-2 py-1.5 transition-colors',
      pinned ? 'border-accent/20 bg-accent/5' : 'border-border-subtle bg-surface-raised hover:border-border',
      (hasBatch || batchActive) && 'border-amber-500/20 bg-amber-500/5',
    )}>
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

      <span className="max-w-[34%] flex-none truncate text-xs font-medium text-txt-primary" title={name}>
        {name}
      </span>

      <span className="flex-none rounded-full border border-border-subtle bg-surface-overlay px-1.5 py-0.5 text-[10px] font-mono text-txt-secondary">
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

function YamlEditor({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <div className="h-full p-3">
      <textarea
        value={value}
        onChange={event => onChange(event.target.value)}
        spellCheck={false}
        className="h-full min-h-[640px] w-full resize-none rounded-md border border-border-subtle bg-surface-raised p-4 font-mono text-xs leading-relaxed text-txt-primary outline-none transition-colors focus:border-border"
        placeholder="# Enter YAML configuration..."
      />
    </div>
  )
}

function ArgsEditor({
  argsText,
  runScript,
  onArgsChange,
  onRunScriptChange,
  scriptPath,
}: {
  argsText: string
  runScript: string
  onArgsChange: (value: string) => void
  onRunScriptChange: (value: string) => void
  scriptPath: string
}) {
  return (
    <div className="flex h-full flex-col gap-3 p-3">
      <section className="rounded-md border border-border-subtle bg-surface-raised p-3">
        <label className="mb-2 block text-xs font-medium text-txt-primary">Run Script</label>
        <input
          value={runScript || `python ${scriptPath}`}
          onChange={event => onRunScriptChange(event.target.value)}
          className="w-full rounded-md border border-border-subtle bg-surface-overlay px-3 py-2 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-border"
          placeholder="python script.py"
        />
      </section>

      <section className="flex min-h-0 flex-1 flex-col rounded-md border border-border-subtle bg-surface-raised p-3">
        <label className="mb-2 block text-xs font-medium text-txt-primary">Arguments</label>
        <textarea
          value={argsText}
          onChange={event => onArgsChange(event.target.value)}
          spellCheck={false}
          className="min-h-[220px] flex-1 resize-y rounded-md border border-border-subtle bg-surface-overlay px-3 py-3 font-mono text-xs leading-relaxed text-txt-primary outline-none transition-colors focus:border-border"
          placeholder="model=vit dataset=imagenet train.epochs=300"
        />
        <div className="mt-2 text-2xs text-txt-tertiary">
          Use <code className="text-txt-secondary">|</code> or `10:100:1` style ranges for batch generation.
        </div>
      </section>
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
