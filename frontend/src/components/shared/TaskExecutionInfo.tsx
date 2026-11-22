import { AlertTriangle, Folder, Server } from 'lucide-react'
import clsx from 'clsx'
import type { Task } from '@/types'
import { useTranslation } from '@/i18n'

export default function TaskExecutionInfo({ task, className, showRecordedHost = true, singleLine = false }: {
  task: Task; className?: string; showRecordedHost?: boolean; singleLine?: boolean
}) {
  const { t } = useTranslation()
  const active = task.status === 'running' || task.status === 'queued'
  const host = active ? task.runner_host : showRecordedHost ? task.run_environments?.at(-1)?.host : null
  const sourceHost = task.task_source_host
  const showHost = host && host.toLowerCase() !== sourceHost?.toLowerCase()
  const cancelling = active && Boolean(task.cancel_requested_at)
  const expired = active && task.runner_lease_expired === true
  const deviceFolder = task.task_storage === 'local' || task.task_storage === 'remote'
  if (!host && !cancelling && !expired && !deviceFolder) return null

  return <div role="group" className={clsx(
    'flex min-w-0 items-center text-2xs text-txt-secondary',
    singleLine ? 'gap-x-2 whitespace-nowrap' : 'flex-wrap gap-x-3 gap-y-1',
    className,
  )} aria-label={t('Execution source')}>
    {deviceFolder && <span className="inline-flex min-w-0 items-center gap-1" title={task.task_source}>
      <Folder aria-hidden="true" className="h-3 w-3 shrink-0" />
      <span className="truncate">{sourceHost ? t('{{host}} · read only', { host: sourceHost })
        : t(task.task_storage === 'remote' ? 'Other device · read only' : 'Device folder')}</span>
    </span>}
    {showHost && <span className="inline-flex min-w-0 items-center gap-1" title={t('Execution host: {{host}}', { host })}>
      <Server aria-hidden="true" className="h-3 w-3 shrink-0" /><span className="truncate font-mono">{host}</span>
    </span>}
    {(cancelling || expired) && <span className={clsx('inline-flex items-center gap-1 text-amber-800 dark:text-amber-300', singleLine && 'min-w-0 shrink-[4]')}
      title={t(cancelling ? 'Cancellation was requested. Waiting for the runner to confirm the result.' : 'The recorded runner lease has expired. This does not confirm that the process stopped; waiting for a verified result.')}>
      <AlertTriangle aria-hidden="true" className="h-3 w-3 shrink-0" />
      <span className={singleLine ? 'monitor-execution-warning-text truncate' : undefined}>
        {t(cancelling ? 'Cancellation awaiting confirmation' : 'Runner status awaiting confirmation')}
      </span>
    </span>}
  </div>
}
