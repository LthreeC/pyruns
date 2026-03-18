import { useEffect, useState, useCallback, useMemo } from 'react'
import { ChevronDown, ChevronRight, Pin, Sparkles, FileCode, Terminal as TermIcon, LayoutGrid, AlertTriangle } from 'lucide-react'
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

  const [columns, setColumns] = useState(2)
  const [previewOpen, setPreviewOpen] = useState(false)
  const [previewData, setPreviewData] = useState<any>(null)
  const [generating, setGenerating] = useState(false)
  const [error, setError] = useState('')
  const [success, setSuccess] = useState('')

  useEffect(() => { fetchTemplates() }, [workspace?.run_root])

  useEffect(() => {
    if (templates.length > 0 && !selectedTemplate) {
      loadTemplate(templates[0].value)
    }
  }, [templates])

  const parsedConfig = useMemo(() => {
    if (viewMode === 'args') return null
    try { return yamlParse(yamlText) as Record<string, any> || {} }
    catch { return null }
  }, [yamlText, viewMode])

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
      fetchTemplates()
      setTimeout(() => setSuccess(''), 4000)
    } catch (e: any) { setError(e.message) }
    finally { setGenerating(false) }
  }, [viewMode, yamlText, argsText, runScript, namePrefix, appendTimestamp, selectedTemplate])

  const handleGenerate = useCallback(async () => {
    setError('')
    setSuccess('')
    const hasBatch = viewMode === 'args' ? argsText.includes('|') : yamlText.includes('|')

    if (hasBatch && !previewOpen) {
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
      } catch (e: any) { setError(e.message) }
      return
    }

    await doCreate()
  }, [viewMode, yamlText, argsText, runScript, namePrefix, appendTimestamp, selectedTemplate, previewOpen, doCreate])

  return (
    <div className="h-full flex flex-col overflow-hidden">
      {/* Header bar */}
      <div className="flex items-center gap-3 px-4 py-2.5 border-b border-border-subtle flex-none bg-surface-raised flex-wrap">
        {/* Template selector */}
        <div className="relative">
          <select
            value={selectedTemplate}
            onChange={e => loadTemplate(e.target.value)}
            title="Select template"
            className="appearance-none bg-surface-overlay border border-border rounded-md pl-3 pr-7 py-1.5 text-xs text-txt-primary outline-none focus:border-accent/50 cursor-pointer max-w-[280px]"
          >
            {templates.map(t => (
              <option key={t.value} value={t.value}>{t.label}</option>
            ))}
          </select>
          <ChevronDown className="absolute right-2 top-1/2 -translate-y-1/2 w-3 h-3 text-txt-tertiary pointer-events-none" />
        </div>

        {templateContent?.read_only && (
          <span className="text-2xs text-amber-500/80 flex items-center gap-1">
            <AlertTriangle className="w-3 h-3" /> Read-only
          </span>
        )}

        <div className="flex-1" />

        {/* View mode toggle */}
        <div className="flex items-center bg-surface-overlay rounded-md border border-border p-0.5">
          {(['form', 'yaml', 'args'] as const).map(mode => (
            <button
              key={mode}
              type="button"
              onClick={() => setViewMode(mode)}
              className={clsx(
                'flex items-center gap-1.5 px-3 py-1 rounded text-xs transition-colors',
                viewMode === mode
                  ? 'bg-accent/15 text-accent font-medium'
                  : 'text-txt-secondary hover:text-txt-primary'
              )}
            >
              {mode === 'form' && <LayoutGrid className="w-3 h-3" />}
              {mode === 'yaml' && <FileCode className="w-3 h-3" />}
              {mode === 'args' && <TermIcon className="w-3 h-3" />}
              {mode.charAt(0).toUpperCase() + mode.slice(1)}
            </button>
          ))}
        </div>

        {/* Column selector dropdown — only in form mode */}
        {viewMode === 'form' && (
          <div className="relative">
            <select
              value={columns}
              onChange={e => setColumns(Number(e.target.value))}
              title="Columns per row"
              className="appearance-none bg-surface-overlay border border-border rounded-md pl-2.5 pr-6 py-1.5 text-xs text-txt-primary outline-none focus:border-accent/50 cursor-pointer"
            >
              {[1,2,3,4,5,6,7,8,9].map(n => (
                <option key={n} value={n}>{n} col{n > 1 ? 's' : ''}</option>
              ))}
            </select>
            <ChevronDown className="absolute right-1.5 top-1/2 -translate-y-1/2 w-3 h-3 text-txt-tertiary pointer-events-none" />
          </div>
        )}
      </div>

      {/* Main content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Editor area */}
        <div className="flex-1 overflow-y-auto">
          {loading ? (
            <div className="flex items-center justify-center h-full">
              <div className="text-xs text-txt-tertiary animate-pulse">Loading template...</div>
            </div>
          ) : viewMode === 'form' ? (
            <FormEditor
              config={parsedConfig}
              columns={columns}
              pinnedParams={pinnedParams}
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

        {/* Settings panel */}
        <div className="w-72 flex-none border-l border-border-subtle bg-surface-raised p-4 flex flex-col gap-4 overflow-y-auto">
          {/* Task name — prominent */}
          <div className="bg-surface-overlay border border-border rounded-lg p-3">
            <label className="text-xs font-medium text-txt-primary block mb-2">Task Name</label>
            <input
              value={namePrefix}
              onChange={e => setNamePrefix(e.target.value)}
              placeholder="task"
              className="w-full bg-surface-base border border-border rounded-md px-3 py-2 text-sm text-txt-primary outline-none focus:border-accent/50 font-medium"
            />
            <label className="flex items-center gap-2 cursor-pointer mt-2.5">
              <input
                type="checkbox"
                checked={appendTimestamp}
                onChange={e => setAppendTimestamp(e.target.checked)}
                className="w-3.5 h-3.5 rounded border-border accent-accent"
              />
              <span className="text-xs text-txt-secondary">Append timestamp</span>
            </label>
          </div>

          {/* Batch syntax hint */}
          <div className="bg-surface-overlay border border-border-subtle rounded-lg p-3 text-2xs text-txt-secondary space-y-1.5">
            <div className="text-xs font-medium text-txt-primary mb-1.5">Batch Syntax</div>
            <div><span className="text-accent font-medium">Product</span>: lr: 0.001 | 0.01 | 0.1</div>
            <div><span className="text-purple-400 font-medium">Zip</span>: seed: (1 | 2 | 3)</div>
            <div><span className="text-pink-400 font-medium">Range</span>: epoch: 1:30:1</div>
          </div>

          {/* Status messages */}
          {error && (
            <div className="bg-rose-500/10 border border-rose-500/20 rounded-lg p-3 text-xs text-rose-400">
              {error}
            </div>
          )}
          {success && (
            <div className="bg-emerald-500/10 border border-emerald-500/20 rounded-lg p-3 text-xs text-emerald-400">
              {success}
            </div>
          )}

          <div className="flex-1" />

          {/* Generate button — large and prominent */}
          <button
            type="button"
            onClick={handleGenerate}
            disabled={generating}
            className="w-full flex items-center justify-center gap-2 px-4 py-3 rounded-lg bg-accent text-white text-sm font-semibold hover:bg-accent-hover transition-colors disabled:opacity-50 shadow-lg shadow-accent/20"
          >
            <Sparkles className="w-4.5 h-4.5" />
            {generating ? 'Generating...' : 'Generate Tasks'}
          </button>
        </div>
      </div>

      {/* Batch Preview Dialog */}
      <ConfirmDialog
        open={previewOpen}
        title="Batch Preview"
        description={previewData ? `${previewData.count} task(s) will be created` : ''}
        confirmLabel="Generate All"
        onConfirm={doCreate}
        onCancel={() => setPreviewOpen(false)}
      >
        {previewData?.items && (
          <div className="max-h-60 overflow-y-auto space-y-1 mt-2">
            {previewData.items.map((item: any) => (
              <div key={item.index} className="text-2xs text-txt-secondary font-mono bg-surface-overlay rounded px-2 py-1">
                #{item.index}: {item.preview}
              </div>
            ))}
            {previewData.count > previewData.items.length && (
              <div className="text-2xs text-txt-tertiary px-2">...and {previewData.count - previewData.items.length} more</div>
            )}
          </div>
        )}
      </ConfirmDialog>
    </div>
  )
}

/* ── Form Editor ── */
function FormEditor({
  config, columns, pinnedParams, onTogglePin, onChange,
}: {
  config: Record<string, any> | null
  columns: number
  pinnedParams: string[]
  onTogglePin: (key: string) => void
  onChange: (data: Record<string, any>) => void
}) {
  const [data, setData] = useState<Record<string, any>>(config || {})

  useEffect(() => { if (config) setData(config) }, [config])

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
    <div className="p-4 space-y-4">
      {pinned.length > 0 && (
        <div className="bg-accent/5 border border-accent/10 rounded-lg p-3">
          <div className="flex items-center gap-1.5 mb-2">
            <Pin className="w-3 h-3 text-accent" />
            <span className="text-2xs font-medium text-accent">Pinned Parameters</span>
          </div>
          <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${columns}, 1fr)` }}>
            {pinned.map(key => (
              <ParamCell key={key} name={key} value={data[key]} pinned onChange={v => handleChange(key, v)} onTogglePin={() => onTogglePin(key)} />
            ))}
          </div>
        </div>
      )}

      <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${columns}, 1fr)` }}>
        {allKeys.map(key => {
          const val = data[key]
          if (typeof val === 'object' && val !== null && !Array.isArray(val)) {
            return (
              <div key={key} className="col-span-full">
                <NestedSection
                  name={key} data={val} columns={columns}
                  pinnedParams={pinnedParams} prefix={key}
                  onTogglePin={onTogglePin}
                  onChange={v => handleChange(key, v)}
                />
              </div>
            )
          }
          return (
            <ParamCell
              key={key} name={key} value={val}
              pinned={pinnedParams.includes(key)}
              onChange={v => handleChange(key, v)}
              onTogglePin={() => onTogglePin(key)}
            />
          )
        })}
      </div>
    </div>
  )
}

/* ── Nested Section ── */
function NestedSection({
  name, data, columns, pinnedParams, prefix, onTogglePin, onChange,
}: {
  name: string; data: Record<string, any>; columns: number
  pinnedParams: string[]; prefix: string
  onTogglePin: (key: string) => void
  onChange: (data: Record<string, any>) => void
}) {
  const [open, setOpen] = useState(true)

  const handleChange = (key: string, value: any) => {
    onChange({ ...data, [key]: value })
  }

  return (
    <div className="border border-border-subtle rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full flex items-center gap-2 px-3 py-2 bg-surface-overlay hover:bg-surface-hover transition-colors text-left"
      >
        {open ? <ChevronDown className="w-3 h-3 text-txt-tertiary" /> : <ChevronRight className="w-3 h-3 text-txt-tertiary" />}
        <span className="text-xs font-medium text-txt-primary">{name}</span>
        <span className="text-2xs text-txt-tertiary">{Object.keys(data).length} params</span>
      </button>
      {open && (
        <div className="p-3">
          <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${columns}, 1fr)` }}>
            {Object.entries(data).filter(([k]) => !k.startsWith('_meta')).map(([key, val]) => {
              const fullKey = `${prefix}.${key}`
              if (typeof val === 'object' && val !== null && !Array.isArray(val)) {
                return (
                  <div key={key} className="col-span-full">
                    <NestedSection
                      name={key} data={val} columns={columns}
                      pinnedParams={pinnedParams} prefix={fullKey}
                      onTogglePin={onTogglePin}
                      onChange={v => handleChange(key, v)}
                    />
                  </div>
                )
              }
              return (
                <ParamCell
                  key={key} name={key} value={val}
                  pinned={pinnedParams.includes(fullKey)}
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

/* ── Param Cell ── */
function ParamCell({
  name, value, pinned, onChange, onTogglePin,
}: {
  name: string; value: any; pinned?: boolean
  onChange: (v: any) => void; onTogglePin: () => void
}) {
  const type = typeof value === 'boolean' ? 'bool' : typeof value === 'number' ? 'number' : 'string'

  return (
    <div className={clsx(
      'flex flex-col gap-1 p-2.5 rounded-md border transition-colors',
      pinned ? 'border-accent/20 bg-accent/5' : 'border-border-subtle hover:border-border'
    )}>
      <div className="flex items-center justify-between">
        <span className="text-2xs font-medium text-txt-secondary truncate">{name}</span>
        <button
          type="button"
          onClick={e => { e.stopPropagation(); onTogglePin() }}
          title={pinned ? 'Unpin parameter' : 'Pin parameter'}
          className={clsx('p-0.5 rounded transition-colors', pinned ? 'text-accent' : 'text-txt-tertiary hover:text-txt-secondary')}
        >
          <Pin className="w-2.5 h-2.5" />
        </button>
      </div>
      {type === 'bool' ? (
        <button
          type="button"
          onClick={() => onChange(!value)}
          title={`Toggle ${name}`}
          className={clsx(
            'w-8 h-4 rounded-full transition-colors relative',
            value ? 'bg-accent' : 'bg-zinc-600'
          )}
        >
          <span className={clsx(
            'absolute top-0.5 w-3 h-3 rounded-full bg-white transition-transform',
            value ? 'left-4' : 'left-0.5'
          )} />
        </button>
      ) : (
        <input
          type={type === 'number' ? 'number' : 'text'}
          value={String(value ?? '')}
          title={name}
          onChange={e => {
            const v = e.target.value
            onChange(type === 'number' ? (v === '' ? 0 : Number(v)) : v)
          }}
          className="w-full bg-surface-overlay border border-border rounded px-2 py-1 text-xs text-txt-primary font-mono outline-none focus:border-accent/50"
        />
      )}
    </div>
  )
}

/* ── YAML Editor ── */
function YamlEditor({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  return (
    <div className="h-full">
      <textarea
        value={value}
        onChange={e => onChange(e.target.value)}
        spellCheck={false}
        className="w-full h-full bg-surface-base text-txt-primary font-mono text-xs p-4 outline-none resize-none leading-relaxed"
        placeholder="# Enter YAML configuration..."
      />
    </div>
  )
}

/* ── Args Editor — resizable textarea ── */
function ArgsEditor({
  argsText, runScript, onArgsChange, onRunScriptChange, scriptPath,
}: {
  argsText: string; runScript: string
  onArgsChange: (v: string) => void; onRunScriptChange: (v: string) => void
  scriptPath: string
}) {
  return (
    <div className="h-full flex flex-col p-4 gap-4">
      <div className="flex-none">
        <label className="text-xs font-medium text-txt-primary block mb-1.5">Run Script</label>
        <input
          value={runScript || `python ${scriptPath}`}
          onChange={e => onRunScriptChange(e.target.value)}
          className="w-full bg-surface-overlay border border-border rounded-md px-3 py-2 text-xs text-txt-primary font-mono outline-none focus:border-accent/50"
          placeholder="python script.py"
        />
      </div>
      <div className="flex-1 flex flex-col min-h-0">
        <label className="text-xs font-medium text-txt-primary block mb-1.5">Arguments</label>
        <textarea
          value={argsText}
          onChange={e => onArgsChange(e.target.value)}
          spellCheck={false}
          className="flex-1 w-full bg-surface-overlay border border-border rounded-md px-3 py-2 text-xs text-txt-primary font-mono outline-none focus:border-accent/50 resize-y leading-relaxed min-h-[120px]"
          placeholder="model=vit dataset=imagenet train.epochs=300"
        />
      </div>
      <div className="text-2xs text-txt-tertiary flex-none">
        Arguments are appended to the run script command. Use <code className="text-txt-secondary">|</code> for batch syntax.
      </div>
    </div>
  )
}
