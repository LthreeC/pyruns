import { useState } from 'react'
import { useTranslation } from '@/i18n'
import type { TaskExportField } from '@/api'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import { errorMessage } from '@/utils/errors'

const OPTIONS: { field: TaskExportField; label: string }[] = [
  { field: 'environment', label: 'Recorded run environment' },
  { field: 'config', label: 'Current configuration' },
  { field: 'notes', label: 'Task notes' },
  { field: 'logs', label: 'Latest log tail (64 KiB per task)' },
]

type ExportScope = 'current' | 'page' | 'selected'
type ExportFormat = 'csv' | 'json'

interface Props {
  current: string
  page: string[]
  selected: string[]
  onChoose: () => void
  onClose: () => void
  onDownload: (names: string[], format: ExportFormat, fields: TaskExportField[]) => Promise<void>
}

export default function TaskExportDialog({ current, page, selected, onChoose, onClose, onDownload }: Props) {
  const { t } = useTranslation()
  const [scope, setScope] = useState<ExportScope>(selected.length ? 'selected' : current ? 'current' : 'page')
  const [format, setFormat] = useState<ExportFormat>('json')
  const [fields, setFields] = useState<TaskExportField[]>(['environment'])
  const [error, setError] = useState('')
  const names = scope === 'selected' ? selected : scope === 'current' && current ? [current] : page
  const selectClass = 'touch-input min-h-11 min-w-0 rounded-md border border-border-subtle bg-surface-base px-2 text-xs sm:min-h-9'

  return (
    <ConfirmDialog
      open title={t('Export tasks')}
      confirmLabel={t(format === 'json' ? 'Download JSON' : 'Download CSV')}
      onCancel={onClose}
      onConfirm={async () => {
        if (!names.length) { setError(t('Select at least one task to export.')); return }
        setError('')
        try {
          await onDownload(names, format, fields)
        } catch (err) {
          setError(errorMessage(err, t('Could not export tasks')))
        }
      }}
    >
      <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-x-4 gap-y-3 text-xs">
        <label htmlFor="export-task-scope" className="font-medium text-txt-primary">{t('Tasks')}</label>
        <select id="export-task-scope" value={scope} className={selectClass} onChange={event => setScope(event.target.value as ExportScope)}>
          {current && <option value="current">{t('Current task')}</option>}
          {page.length > 0 && <option value="page">{t('Current results page')}</option>}
          {selected.length > 0 && <option value="selected">{t('Selected tasks')}</option>}
        </select>
        <label htmlFor="export-task-format" className="font-medium text-txt-primary">{t('File format')}</label>
        <select id="export-task-format" value={format} className={selectClass} onChange={event => setFormat(event.target.value as ExportFormat)}>
          <option value="json">JSON</option>
          <option value="csv">CSV</option>
        </select>
        <span className="text-txt-secondary">{t('{{count}} selected', { count: names.length })}</span>
        <button type="button" onClick={onChoose} className="touch-target min-h-11 justify-self-end rounded px-2 font-medium text-accent-ink hover:bg-surface-overlay sm:min-h-8">
          {t('Choose tasks')}
        </button>
      </div>
      {format === 'json' ? (
        <fieldset className="mt-3 rounded-md border border-border-subtle bg-surface-base px-3 py-2">
          <legend className="px-1 text-xs font-medium text-txt-primary">{t('Include with run results')}</legend>
          {OPTIONS.map(({ field, label }) => (
            <label key={field} className="flex min-h-11 cursor-pointer items-center gap-2.5 text-xs text-txt-primary sm:min-h-8">
              <input type="checkbox" className="size-4 shrink-0 accent-accent" checked={fields.includes(field)}
                onChange={event => setFields(previous => event.target.checked ? [...previous, field] : previous.filter(item => item !== field))} />
              {t(label)}
            </label>
          ))}
        </fieldset>
      ) : <p className="mt-3 border-t border-border-subtle pt-3 text-xs text-txt-secondary">{t('One row per run with status, timing, and recorded metrics.')}</p>}
      {error && <p role="alert" className="mt-3 text-xs text-rose-700 dark:text-rose-300">{error}</p>}
    </ConfirmDialog>
  )
}
