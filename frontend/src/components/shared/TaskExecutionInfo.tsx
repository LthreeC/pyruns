import { AlertTriangle, Folder, Server } from 'lucide-react'
import clsx from 'clsx'
import type { Task } from '@/types'
import { useTranslation } from '@/i18n'
import { isActiveTask, stopOnExecutionDevice } from '@/utils/taskStop'

export default function TaskExecutionInfo({ task, className, showRecordedHost = true, singleLine = false }: {
  task: Task; className?: string; showRecordedHost?: boolean; singleLine?: boolean
}) {
  const { t } = useTranslation()
  const active = isActiveTask(task)
  const host = active ? task.runner_host : showRecordedHost ? task.run_environments?.at(-1)?.host : null
  const sourceHost = task.task_source_host
  const stopOnOtherDevice = stopOnExecutionDevice(task)
  const showHost = !stopOnOtherDevice && host && host.toLowerCase() !== sourceHost?.toLowerCase()
  const expired = active && !task.local_process_verified && task.runner_lease_expired === true
  const deviceFolder = task.task_storage === 'local' || task.task_storage === 'remote'
  const stopHost = host || sourceHost
  const stopLabel = stopHost ? t('Stop on {{host}}', { host: stopHost }) : t('Stop on execution device')
  const deviceLabel = sourceHost ? t('Other device · {{host}}', { host: sourceHost })
    : t(task.task_storage === 'remote' ? 'Other device' : 'Device folder')
  if (!stopOnOtherDevice && !host && !expired && !deviceFolder) return null

  return <div role="group" className={clsx(
    'flex min-w-0 items-center text-2xs text-txt-secondary',
    singleLine ? 'gap-x-2 whitespace-nowrap' : 'flex-wrap gap-x-3 gap-y-1',
    className,
  )} aria-label={t('Execution source')}>
    {stopOnOtherDevice && <span className="inline-flex min-w-0 items-center gap-1" title={stopLabel}>
      <Server aria-hidden="true" className="h-3 w-3 shrink-0" /><span className="truncate">{stopLabel}</span>
    </span>}
    {deviceFolder && !stopOnOtherDevice && <span className="inline-flex min-w-0 items-center gap-1" title={task.task_source}>
      <Folder aria-hidden="true" className="h-3 w-3 shrink-0" />
      <span className="truncate">{deviceLabel}</span>
    </span>}
    {showHost && <span className="inline-flex min-w-0 items-center gap-1" title={t('Execution host: {{host}}', { host })}>
      <Server aria-hidden="true" className="h-3 w-3 shrink-0" /><span className="truncate font-mono">{host}</span>
    </span>}
    {expired && <span className={clsx('inline-flex items-center gap-1 text-amber-800 dark:text-amber-300', singleLine && 'min-w-0 shrink-[4]')}
      data-runner-warning="expired"
      title={t('The recorded runner lease has expired. This does not confirm that the process stopped; waiting for a verified result.')}>
      <AlertTriangle aria-hidden="true" className="h-3 w-3 shrink-0" />
      <span className={singleLine ? 'monitor-execution-warning-text truncate' : undefined}>
        {t('Runner status awaiting confirmation')}
      </span>
    </span>}
  </div>
}
