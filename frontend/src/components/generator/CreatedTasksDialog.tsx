import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Loader2, X } from 'lucide-react'
import type { Task } from '@/types'
import { batchRunTasks } from '@/api'
import { useTranslation } from '@/i18n'
import { errorMessage } from '@/utils/errors'
import ActionButton from '@/components/shared/ActionButton'
import StatusBadge from '@/components/shared/StatusBadge'
import { useWorkspaceStore } from '@/store'

const PAGE_SIZE = 50

export default function CreatedTasksDialog({ tasks: createdTasks, submission, onSubmitted, onClose }: {
  tasks: Task[]; submission?: string; onSubmitted: (tasks: Task[], message: string) => void; onClose: () => void
}) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const dialog = useRef<HTMLDialogElement>(null)
  const mounted = useRef(true)
  const running = useRef(false)
  const [tasks, setTasks] = useState(createdTasks)
  const [offset, setOffset] = useState(0)
  const [busy, setBusy] = useState(false)
  const [submitted, setSubmitted] = useState(Boolean(submission))
  const [message, setMessage] = useState(submission || '')
  const [error, setError] = useState('')
  const pending = tasks.filter(task => task.status === 'pending')

  useEffect(() => {
    mounted.current = true
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null
    dialog.current?.showModal()
    return () => {
      mounted.current = false
      trigger?.isConnected && trigger.focus()
    }
  }, [])

  const run = async () => {
    if (running.current || submitted || !pending.length) return
    const identities = Object.fromEntries(pending.map(task => [task.name, task.task_identity || '']))
    if (Object.values(identities).some(identity => !identity)) {
      setError(t('Task identity is unavailable. Open the tasks in Manager before running.'))
      return
    }
    running.current = true
    setBusy(true)
    setError('')
    const workspaceEpoch = useWorkspaceStore.getState().workspaceEpoch
    const current = () => mounted.current && workspaceEpoch === useWorkspaceStore.getState().workspaceEpoch
    try {
      const result = await batchRunTasks(pending.map(task => task.name), undefined, identities)
      if (!current()) return
      const updated = new Map(result.items.map(task => [task.name, task]))
      const nextTasks = tasks.map(task => updated.get(task.name) || task)
      const resultMessage = t('Submitted {{count}} tasks; {{skipped}} were not started. Open Manager for live status.', { count: result.count, skipped: result.skipped.length })
      setTasks(nextTasks)
      setSubmitted(true)
      setMessage(resultMessage)
      onSubmitted(nextTasks, resultMessage)
    } catch (caught) {
      if (current()) setError(errorMessage(caught))
    } finally {
      running.current = false
      if (current()) setBusy(false)
    }
  }

  return <dialog ref={dialog} aria-labelledby="created-tasks-title" onCancel={event => { event.preventDefault(); if (!busy) onClose() }}
    className="fixed inset-0 m-auto max-h-[calc(100dvh-1.5rem)] w-[min(680px,calc(100vw-1.5rem))] overflow-y-auto rounded-md border border-border-subtle bg-surface-raised p-4 text-txt-primary shadow-xl backdrop:bg-black/50 sm:p-5">
    <div className="flex items-center justify-between gap-3">
      <h2 id="created-tasks-title" className="text-base font-semibold">{t('Created batch · {{count}} tasks', { count: tasks.length })}</h2>
      <ActionButton disabled={busy} onClick={onClose} aria-label={t('Close created batch')}><X aria-hidden="true" className="h-4 w-4" /></ActionButton>
    </div>
    <p className="my-3 text-xs leading-5 text-txt-secondary">{t('Only tasks created by this operation are listed. Run starts their first attempt only; active or previously run tasks are skipped.')}</p>
    <p className="mb-2 text-2xs text-txt-tertiary">{t('Status reflects the last creation or submission response. Open a task for live details.')}</p>
    <ul className="max-h-[45vh] divide-y divide-border-subtle overflow-y-auto">
      {tasks.slice(offset, offset + PAGE_SIZE).map(task => <li key={task.name}>
        <button type="button" disabled={busy} onClick={() => { onClose(); navigate(`/manager?task=${encodeURIComponent(task.name)}`) }}
          className="flex min-h-11 w-full items-center gap-3 rounded px-2 py-2 text-left text-sm hover:bg-surface-overlay disabled:opacity-50">
          <StatusBadge status={task.status} /><span className="min-w-0 break-all">{task.name}</span>
        </button>
      </li>)}
    </ul>
    {tasks.length > PAGE_SIZE && <div className="mt-2 flex items-center justify-between text-xs">
      <ActionButton disabled={offset === 0} onClick={() => setOffset(offset - PAGE_SIZE)}>{t('Previous page')}</ActionButton>
      <span>{offset + 1}–{Math.min(offset + PAGE_SIZE, tasks.length)} / {tasks.length}</span>
      <ActionButton disabled={offset + PAGE_SIZE >= tasks.length} onClick={() => setOffset(offset + PAGE_SIZE)}>{t('Next page')}</ActionButton>
    </div>}
    {error && <p role="alert" className="mt-3 whitespace-pre-wrap break-words text-xs text-rose-700 dark:text-rose-300">{error}</p>}
    {message && <p role="status" className="mt-3 text-xs text-txt-secondary">{message}</p>}
    <div className="mt-4 flex flex-wrap justify-end gap-2 border-t border-border-subtle pt-3">
      <ActionButton disabled={busy} variant="secondary" onClick={onClose}>{t('Close')}</ActionButton>
      <ActionButton disabled={busy || submitted || !pending.length} variant="primary" onClick={() => void run()}
        icon={busy ? <Loader2 aria-hidden="true" className="h-4 w-4 motion-safe:animate-spin" /> : undefined}>
        {busy ? t('Submitting…') : submitted ? t('Batch submitted') : t('Run {{count}} pending tasks', { count: pending.length })}
      </ActionButton>
    </div>
  </dialog>
}
