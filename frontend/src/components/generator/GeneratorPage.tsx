import { useEffect, useState, useCallback, useMemo } from 'react'
import {
  ChevronDown, ChevronRight, Pin, Sparkles, FileCode,
  Terminal as TermIcon, LayoutGrid, AlertTriangle, Info,
} from 'lucide-react'
import clsx from 'clsx'
import { useGeneratorStore, useWorkspaceStore } from '@/store'
import EmptyState from '@/components/shared/EmptyState'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import * as api from '@/api'
import { parse as yamlParse, stringify as yamlStringify } from 'yaml'

export default function GeneratorPage() {
  const workspace = useWorkspaceStore(s => s.workspace)
  const {
    templates, selectedTemplate, templateContent, viewMode, yamlText, argsText, runScript,
    namePrefix, appendTimestamp, pinnedParams, loading,
    fetchTemplates, loadTemplate, setViewMode, setYamlText, setArgsText, setRunScript,
    setNamePrefix, setAppendTimestamp, togglePin,
  } = useGeneratorStore()

  const [columns, setColumns] = useState(3)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewData, setPreviewData] = useState<any>(null)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  useEffect(() => { fetchTemplates() }, [workspace?.run_root, fetchTemplates])

  useEffect(() => {
    if (templates.length > 0 && !selectedTemplate) {
      void loadTemplate(templates[0].value)
    }
  }, [templates, selectedTemplate, loadTemplate])

  const parsedConfig = useMemo(() => {
    if (viewMode === 'args') return null
    try { return yamlParse(yamlText) as Record<string, any> || {} }
    catch { return null }
  }, [yamlText, viewMode])

  const batchParams = useMemo(() => {
    if (!parsedConfig) return []
    const result: string[] = []
    const walk = (obj: Record<string, any>, prefix = '') => {
      for (const [key, value] of Object.entries(obj)) {
        const fullKey = prefix ? `${prefix}.${key}` : key
        if (typeof value === 'string' && (value.includes('|') || /^\s*-?\d+\s*:\s*-?\d+(?:\s*:\s*-?\d+)?\s*$/.test(value.trim()))) {
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
      const res = await api.createTasks({
        name_prefix: namePrefix || 'task',
        run_mode: viewMode === 'args' ? 'args' : 'yaml',
        yaml_text: viewMode !== 'args' ? yamlText : '',
        args_text: viewMode === 'args' ? argsText : '',
        run_script: viewMode === 'args' ? runScript : '',
        template_value: selectedTemplate,
        append_timestamp: appendTimestamp,
      })
      setPreviewOpen(false)
      setSuccess(`Created ${res.count} task(s)`)
      void fetchTemplates()
      setTimeout(() => setSuccess(''), 4000)
    } catch (e: any) {
      setError(e.message)
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
      } catch (e: any) {
        setError(e.message)
      }
      return
    }

    await doCreate()
  }, [viewMode, yamlText, argsText, runScript, selectedTemplate, hasBatchSyntax, previewOpen, doCreate])

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="flex flex-wrap items-center gap-2.5 border-b border-border-subtle bg-surface-raised px-4 py-2.5">
        <div className="relative">
          <select
            value={selectedTemplate}
            onChange={e => void loadTemplate(e.target.value)}
            title="Select template"
            className="max-w-[320px] appearance-none rounded-md border border-border-subtle bg-surface-overlay pl-3 pr-7 py-1.5 text-xs text-txt-primary outline-none transition-colors focus:border-border"
          >
            {templates.map(t => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
          <ChevronDown className="pointer-events-none absolute right-2 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
        </div>

        {viewMode === 'form' && (
          <div className="relative">
            <select
              value={columns}
              onChange={e => setColumns(Number(e.target.value))}
              title="Columns per row"
              className="appearance-none rounded-md border border-border-subtle bg-surface-overlay pl-2.5 pr-6 py-1.5 text-xs text-txt-primary outline-none transition-colors focus:border-border"
            >
              {[1, 2, 3, 4, 5, 6].map(n => (
                <option key={n} value={n}>{n} col{n > 1 ? 's' : ''}</option>
              ))}
            </select>
            <ChevronDown className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
          </div>
        )}

        {templateContent?.read_only && (
          <span className="flex items-center gap-1 rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-1 text-2xs text-amber-400">
            <AlertTriangle className="h-3 w-3" /> Read-only
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
                'flex items-center gap-1.5 rounded-md px-3 py-1.5 text-xs transition-colors',
                viewMode === mode
                  ? 'bg-surface-raised text-txt-primary'
                  : 'text-txt-secondary hover:text-txt-primary'
              )}
            >
              {mode === 'form' && <LayoutGrid className="h-3 w-3" />}
              {mode === 'yaml' && <FileCode className="h-3 w-3" />}
              {mode === 'args' && <TermIcon className="h-3 w-3" />}
              {mode.charAt(0).toUpperCase() + mode.slice(1)}
            </button>
          ))}
        </div>
      </div>

      <div className="flex flex-1 overflow-hidden">
        <div className="flex-1 overflow-y-auto" style={{ flexBasis: '68%' }}>
          {loading ? (
            <div className="flex h-full items-center justify-center">
              <div className="text-xs text-txt-tertiary animate-pulse">Loading template...</div>
            </div>
          ) : (
            <div className="mx-auto max-w-[1440px] p-5">
              <div className="mb-4 flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-border-subtle pb-3 text-2xs text-txt-secondary">
                <MetaInline label="Template" value={templateContent?.label || 'No template'} />
                <MetaInline label="Mode" value={viewMode.toUpperCase()} />
                <MetaInline label="Batch" value={hasBatchSyntax ? `${Math.max(batchParams.length, 1)} hot parameter(s)` : 'Single run'} />
              </div>

              <div className="overflow-hidden rounded-lg border border-border-subtle bg-surface-raised">
                {viewMode === 'form' ? (
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
            </div>
          )}
        </div>

        <div className="flex flex-col gap-4 overflow-y-auto border-l border-border-subtle bg-surface-raised p-4" style={{ flexBasis: '32%', minWidth: 280, maxWidth: 380 }}>
          <section className="border-b border-border-subtle pb-4">
            <div className="mb-2 text-2xs uppercase tracking-[0.18em] text-txt-tertiary">Naming</div>
            <label className="mb-2 block text-sm font-medium text-txt-primary">Task Name Prefix</label>
            <input
              value={namePrefix}
              onChange={e => setNamePrefix(e.target.value)}
              placeholder="task"
              className="w-full rounded-lg border border-border-subtle bg-surface-base px-3 py-2.5 text-sm font-medium text-txt-primary outline-none transition-colors focus:border-border"
            />
            <label className="mt-3 flex items-center gap-2 text-xs text-txt-secondary">
              <input
                type="checkbox"
                checked={appendTimestamp}
                onChange={e => setAppendTimestamp(e.target.checked)}
                className="h-3.5 w-3.5 rounded border-border accent-accent"
              />
              Append timestamp
            </label>
          </section>

          {batchParams.length > 0 && (
            <section className="border-b border-border-subtle pb-4">
              <div className="mb-2 flex items-center gap-1.5">
                <Info className="h-3.5 w-3.5 text-amber-400" />
                <span className="text-xs font-medium text-txt-primary">Batch expansion sources</span>
              </div>
              <div className="flex flex-wrap gap-1.5 text-2xs text-txt-secondary">
                {batchParams.map(param => (
                  <div key={param} className="rounded-md border border-border-subtle bg-surface-overlay px-2 py-1 font-mono">
                    {param}
                  </div>
                ))}
              </div>
            </section>
          )}

          <section className="border-b border-border-subtle pb-4 text-2xs text-txt-secondary">
            <div className="mb-2 text-xs font-medium text-txt-primary">Batch syntax</div>
            <div className="space-y-2 leading-relaxed">
              <div><span className="text-txt-primary">Product</span>: `lr: 0.001 | 0.01 | 0.1`</div>
              <div><span className="text-txt-primary">Zip</span>: `seed: (1 | 2 | 3)`</div>
              <div><span className="text-txt-primary">Range</span>: `epoch: 10:100:1`</div>
            </div>
          </section>

          {error && (
            <div className="rounded-lg border border-rose-500/20 bg-rose-500/10 p-3 text-xs text-rose-400">
              {error}
            </div>
          )}

          {success && (
            <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 p-3 text-xs text-emerald-400">
              {success}
            </div>
          )}

          <div className="flex-1" />

          <button
            type="button"
            onClick={handleGenerate}
            disabled={generating}
            className="flex w-full items-center justify-center gap-2 rounded-md bg-accent px-4 py-2.5 text-sm font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-50"
          >
            <Sparkles className="h-4 w-4" />
            {generating ? 'Generating...' : 'Generate Tasks'}
          </button>
        </div>
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
              <div key={item.index} className="rounded-lg border border-border-subtle bg-surface-overlay px-2 py-1 text-2xs font-mono text-txt-secondary">
                #{item.index}: {item.preview}
              </div>
            ))}
            {previewData.count > previewData.items.length && (
              <div className="px-2 text-2xs text-txt-tertiary">...and {previewData.count - previewData.items.length} more</div>
            )}
          </div>
        )}
      </ConfirmDialog>
    </div>
  )
}

function MetaInline({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center gap-2">
      <span className="uppercase tracking-[0.16em] text-txt-tertiary">{label}</span>
      <span className="truncate text-xs font-medium text-txt-primary" title={value}>{value}</span>
    </div>
  )
}

function FormEditor({
  config, columns, pinnedParams, batchParams, onTogglePin, onChange,
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

  const pinned = pinnedParams.filter(k => k in data)
  const allKeys = Object.keys(data).filter(k => !k.startsWith('_meta'))

  const handleChange = (key: string, value: any) => {
    const next = { ...data, [key]: value }
    setData(next)
    onChange(next)
  }

  return (
    <div className="p-5">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <span className="rounded-full border border-border-subtle bg-surface-overlay px-2.5 py-1 text-2xs text-txt-secondary">
          {allKeys.length} top-level fields
        </span>
        <span className="rounded-full border border-border-subtle bg-surface-overlay px-2.5 py-1 text-2xs text-txt-secondary">
          {pinned.length} pinned
        </span>
        <span className="rounded-full border border-border-subtle bg-surface-overlay px-2.5 py-1 text-2xs text-txt-secondary">
          {batchParams.length} batch-sensitive
        </span>
      </div>

      <div className="space-y-5">
        {pinned.length > 0 && (
          <div className="border-b border-border-subtle pb-4">
            <div className="mb-3 flex items-center gap-1.5">
              <Pin className="h-3.5 w-3.5 text-accent" />
              <span className="text-xs font-medium text-txt-primary">Pinned Parameters</span>
            </div>
            <div className="grid gap-2.5" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
              {pinned.map(key => (
                <ParamCell
                  key={key}
                  name={key}
                  value={data[key]}
                  pinned
                  batchActive={batchParams.includes(key)}
                  onChange={v => handleChange(key, v)}
                  onTogglePin={() => onTogglePin(key)}
                />
              ))}
            </div>
          </div>
        )}

        <div className="grid gap-2.5" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
          {allKeys.map(key => {
            const val = data[key]
            if (typeof val === 'object' && val !== null && !Array.isArray(val)) {
              return (
                <div key={key} className="col-span-full">
                  <NestedSection
                    name={key}
                    data={val}
                    columns={columns}
                    pinnedParams={pinnedParams}
                    batchParams={batchParams}
                    prefix={key}
                    onTogglePin={onTogglePin}
                    onChange={v => handleChange(key, v)}
                  />
                </div>
              )
            }
            return (
              <ParamCell
                key={key}
                name={key}
                value={val}
                pinned={pinnedParams.includes(key)}
                batchActive={batchParams.includes(key)}
                onChange={v => handleChange(key, v)}
                onTogglePin={() => onTogglePin(key)}
              />
            )
          })}
        </div>
      </div>
    </div>
  )
}

function NestedSection({
  name, data, columns, pinnedParams, batchParams, prefix, onTogglePin, onChange,
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
    <div className="overflow-hidden rounded-lg border border-border-subtle bg-surface-overlay/20">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex w-full items-center gap-2 border-b border-border-subtle px-4 py-3 text-left transition-colors hover:bg-surface-overlay"
      >
        {open ? <ChevronDown className="h-3.5 w-3.5 text-txt-tertiary" /> : <ChevronRight className="h-3.5 w-3.5 text-txt-tertiary" />}
        <span className="text-sm font-medium text-txt-primary">{name}</span>
        <span className="rounded-full border border-border-subtle px-2 py-1 text-2xs text-txt-tertiary">
          {Object.keys(data).length} fields
        </span>
      </button>
      {open && (
        <div className="p-4">
          <div className="grid gap-2.5" style={{ gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` }}>
            {Object.entries(data).filter(([k]) => !k.startsWith('_meta')).map(([key, val]) => {
              const fullKey = `${prefix}.${key}`
              if (typeof val === 'object' && val !== null && !Array.isArray(val)) {
                return (
                  <div key={key} className="col-span-full">
                    <NestedSection
                      name={key}
                      data={val}
                      columns={columns}
                      pinnedParams={pinnedParams}
                      batchParams={batchParams}
                      prefix={fullKey}
                      onTogglePin={onTogglePin}
                      onChange={v => handleChange(key, v)}
                    />
                  </div>
                )
              }
              return (
                <ParamCell
                  key={key}
                  name={key}
                  value={val}
                  pinned={pinnedParams.includes(fullKey)}
                  batchActive={batchParams.includes(fullKey)}
                  onChange={v => handleChange(key, v)}
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

function ParamCell({
  name, value, pinned, batchActive, onChange, onTogglePin,
}: {
  name: string
  value: any
  pinned?: boolean
  batchActive?: boolean
  onChange: (v: any) => void
  onTogglePin: () => void
}) {
  const origType = value === null || value === undefined ? 'null'
    : typeof value === 'boolean' ? 'bool'
    : typeof value === 'number' ? (Number.isInteger(value) ? 'int' : 'float')
    : Array.isArray(value) ? 'list'
    : 'str'

  const typeColor: Record<string, string> = {
    bool: 'text-txt-secondary bg-surface-overlay border-border-subtle',
    int: 'text-txt-secondary bg-surface-overlay border-border-subtle',
    float: 'text-txt-secondary bg-surface-overlay border-border-subtle',
    str: 'text-txt-secondary bg-surface-overlay border-border-subtle',
    list: 'text-txt-secondary bg-surface-overlay border-border-subtle',
    null: 'text-txt-secondary bg-surface-overlay border-border-subtle',
  }

  const [localValue, setLocalValue] = useState(stringifyEditableValue(value))

  useEffect(() => {
    setLocalValue(stringifyEditableValue(value))
  }, [value])

  const hasBatch = localValue.includes('|') || /^\s*-?\d+\s*:\s*-?\d+(?:\s*:\s*-?\d+)?\s*$/.test(localValue.trim())

  const commitValue = () => {
    const next = localValue.trim()
    if (origType === 'bool') {
      if (!hasBatch && (next === 'true' || next === 'True' || next === '1')) onChange(true)
      else if (!hasBatch && (next === 'false' || next === 'False' || next === '0')) onChange(false)
      else onChange(localValue)
      return
    }

    if (origType === 'int' || origType === 'float') {
      const num = Number(next)
      if (!hasBatch && next !== '' && !Number.isNaN(num)) {
        onChange(origType === 'int' ? Math.round(num) : num)
      } else {
        onChange(localValue)
      }
      return
    }

    if (origType === 'list' && !hasBatch) {
      try {
        const parsed = JSON.parse(localValue)
        onChange(Array.isArray(parsed) ? parsed : localValue)
      } catch {
        onChange(localValue)
      }
      return
    }

    if (origType === 'null' && !hasBatch && next === '') {
      onChange(null)
      return
    }

    onChange(localValue)
  }

  return (
    <div className={clsx(
      'flex flex-col gap-1.5 rounded-lg border p-2.5 transition-colors',
      pinned ? 'border-accent/20 bg-surface-overlay/70' : 'border-border-subtle bg-surface-raised hover:border-border'
    )}>
      <div className="flex items-center gap-2">
        <span className="flex-1 truncate text-xs font-medium text-txt-primary" title={name}>{name}</span>
        <span className={clsx('rounded-full border px-2 py-1 text-2xs font-mono', typeColor[origType] || typeColor.str)}>
          {origType}
        </span>
        <button
          type="button"
          onClick={e => { e.stopPropagation(); onTogglePin() }}
          title={pinned ? 'Unpin' : 'Pin'}
          className={clsx('rounded-lg p-1 transition-colors', pinned ? 'text-accent' : 'text-txt-tertiary hover:bg-surface-hover hover:text-txt-secondary')}
        >
          <Pin className="h-3.5 w-3.5" />
        </button>
      </div>

      {origType === 'bool' && !hasBatch ? (
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => onChange(!value)}
            title={`Toggle ${name}`}
            className={clsx(
              'relative h-5 w-10 rounded-full transition-colors',
              value ? 'bg-accent' : 'bg-zinc-600'
            )}
          >
            <span className={clsx(
              'absolute top-0.5 h-4 w-4 rounded-full bg-white transition-transform',
              value ? 'left-[22px]' : 'left-0.5'
            )} />
          </button>
          <span className="text-xs font-mono text-txt-secondary" title={String(value)}>{String(value)}</span>
        </div>
      ) : (
        <textarea
          value={localValue}
          onChange={e => setLocalValue(e.target.value)}
          onBlur={commitValue}
          onKeyDown={e => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              commitValue()
            }
          }}
          rows={Array.isArray(value) ? 3 : 1}
          spellCheck={false}
          className={clsx(
            'min-h-[36px] w-full resize-y rounded-md border px-2.5 py-2 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-border',
            hasBatch || batchActive ? 'border-amber-500/20 bg-amber-500/5' : 'border-border-subtle bg-surface-overlay'
          )}
        />
      )}

      {(hasBatch || batchActive) && (
        <div className="text-2xs text-amber-400">
          Batch sensitive
        </div>
      )}
    </div>
  )
}

function YamlEditor({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="h-full min-h-[640px]">
      <textarea
        value={value}
        onChange={e => onChange(e.target.value)}
        spellCheck={false}
        className="h-full w-full resize-none bg-surface-base p-5 font-mono text-xs leading-relaxed text-txt-primary outline-none"
        placeholder="# Enter YAML configuration..."
      />
    </div>
  )
}

function ArgsEditor({
  argsText, runScript, onArgsChange, onRunScriptChange, scriptPath,
}: {
  argsText: string
  runScript: string
  onArgsChange: (v: string) => void
  onRunScriptChange: (v: string) => void
  scriptPath: string
}) {
  return (
    <div className="flex min-h-[640px] flex-col gap-4 p-5">
      <div className="rounded-lg border border-border-subtle bg-surface-overlay/40 p-4">
        <label className="mb-2 block text-xs font-semibold text-txt-primary">Run Script</label>
        <input
          value={runScript || `python ${scriptPath}`}
          onChange={e => onRunScriptChange(e.target.value)}
          className="w-full rounded-lg border border-border-subtle bg-surface-base px-3 py-2 text-xs font-mono text-txt-primary outline-none transition-colors focus:border-border"
          placeholder="python script.py"
        />
      </div>

      <div className="flex flex-1 flex-col rounded-lg border border-border-subtle bg-surface-overlay/20 p-4">
        <label className="mb-2 block text-xs font-semibold text-txt-primary">Arguments</label>
        <textarea
          value={argsText}
          onChange={e => onArgsChange(e.target.value)}
          spellCheck={false}
          className="min-h-[220px] flex-1 resize-y rounded-lg border border-border-subtle bg-surface-base px-3 py-3 font-mono text-xs leading-relaxed text-txt-primary outline-none transition-colors focus:border-border"
          placeholder="model=vit dataset=imagenet train.epochs=300"
        />
        <div className="mt-3 text-2xs text-txt-tertiary">
          Arguments are appended to the run script command. Use <code className="text-txt-secondary">|</code> for batch syntax.
        </div>
      </div>
    </div>
  )
}

function stringifyEditableValue(value: any) {
  if (Array.isArray(value)) {
    try {
      return JSON.stringify(value, null, 2)
    } catch {
      return String(value)
    }
  }
  return String(value ?? '')
}
