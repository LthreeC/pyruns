import { useEffect, useRef, useState } from 'react'
import { ChevronRight, FileText, LoaderCircle, X } from 'lucide-react'
import type { Task, TaskSearchMatch } from '@/types'
import { splitTaskSearchSnippet } from '@/utils/monitorSearch'
import { errorMessage } from '@/utils/errors'
import { getTaskLogs } from '@/api'
import CopyButton from './CopyButton'

const LABELS = { name: 'Name', notes: 'Notes', config: 'Config', script: 'Script', log: 'Log' }

export function SearchMatchContext({ match }: { match: TaskSearchMatch }) {
  const [before, highlighted, after] = splitTaskSearchSnippet(match.snippet, match.match_start, match.match_end)
  return <span className="flex min-w-0 items-start gap-2 font-mono text-2xs leading-5" title={match.snippet}>
    {match.line && <span className="flex-none select-none tabular-nums text-txt-tertiary">{match.line}</span>}
    <span className="min-w-0 break-all text-txt-secondary">
      {before}<mark className="rounded-sm bg-amber-200/80 text-slate-950 dark:bg-amber-400/75">{highlighted}</mark>{after}
    </span>
  </span>
}

export default function TaskSearchMatches({ task, onSelect, action = 'View', exportMode = false }: {
  task: Task; onSelect: () => void; action?: string; exportMode?: boolean
}) {
  const [preview, setPreview] = useState<TaskSearchMatch | null>(null)
  const matches = task.search_matches ?? []
  const groups = new Map<string, TaskSearchMatch[]>()
  for (const match of matches) {
    const source = match.log_file || `${LABELS[match.field]}${match.location ? `: ${match.location}` : ''}`
    groups.set(source, [...(groups.get(source) ?? []), match])
  }
  const count = Math.max(matches.length, task.search_match_count ?? 0)
  return <div aria-label={`Matches in ${task.name}`} className="min-w-0">
    {Array.from(groups, ([source, rows]) => <details key={source} open className="group/search border-l border-border-subtle">
      <summary className="touch-target flex min-h-8 cursor-pointer list-none items-center gap-1 px-2 text-2xs text-txt-tertiary hover:bg-surface-overlay">
        <ChevronRight className="h-3 w-3 flex-none group-open/search:rotate-90" aria-hidden="true" />
        <FileText className="h-3 w-3 flex-none" aria-hidden="true" />
        <span className="min-w-0 flex-1 truncate" title={source}>{source}</span>
        <span className="tabular-nums" title={`${rows.length} previewed matches`}>{rows.length}</span>
      </summary>
      {rows.map((match, index) => <button key={index} type="button"
        aria-label={`${action} ${LABELS[match.field]} match in ${task.name}${match.location ? ` at ${match.location}` : ''}: ${match.snippet}`}
        className="touch-target block w-full min-w-0 border-l-2 border-transparent py-1 pl-5 pr-2 text-left hover:border-accent/50 hover:bg-accent/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/35"
        onClick={() => match.field === 'log' && !exportMode ? setPreview(match) : onSelect()}>
        <SearchMatchContext match={match} />
      </button>)}
    </details>)}
    {count > matches.length && <p className="px-2 py-1 text-2xs text-txt-tertiary">Showing {matches.length} of {count.toLocaleString()} matches</p>}
    {preview && <LogMatchDialog key={`${task.name}:${preview.location}:${preview.offset}`} task={task} match={preview} onClose={() => setPreview(null)} />}
  </div>
}

function LogMatchDialog({ task, match, onClose }: { task: Task; match: TaskSearchMatch; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const [content, setContent] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [retry, setRetry] = useState(0)
  useEffect(() => {
    const previousFocus = document.activeElement
    const modal = dialog.current
    modal?.showModal()
    return () => {
      modal?.close()
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus({ preventScroll: true })
    }
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    const timeout = window.setTimeout(() => controller.abort(new Error('Log preview timed out. Please retry.')), 10_000)
    setLoading(true)
    setError('')
    void getTaskLogs(task.name, { logFileName: match.log_file, logIdentity: match.log_identity, offset: match.offset ?? 0, chunkSize: 32 * 1024 }, controller.signal)
      .then(log => {
        if (controller.signal.aborted) return
        if (!log.available_logs.includes(match.log_file ?? '') || log.selected_log !== match.log_file || log.reset) throw new Error('This log changed. Refresh the search to locate the match again.')
        setContent(log.content.replace(/\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))/g, ''))
      })
      .catch(err => { if (controller.signal.reason?.name !== 'AbortError') setError(errorMessage(err)) })
      .finally(() => { window.clearTimeout(timeout); if (controller.signal.reason?.name !== 'AbortError') setLoading(false) })
    return () => { window.clearTimeout(timeout); controller.abort() }
  }, [task.name, match.log_file, match.log_identity, match.offset, retry])
  return <dialog ref={dialog} aria-label={`Log match in ${task.name}`} onCancel={event => { event.preventDefault(); onClose() }}
    className="fixed inset-0 z-50 m-auto max-h-[calc(100dvh-1.5rem)] w-[calc(100vw-1.5rem)] max-w-3xl overflow-hidden rounded-md border border-border-subtle bg-surface-raised p-0 text-txt-primary shadow-md backdrop:bg-black/50">
    <div className="flex max-h-[calc(100dvh-1.5rem)] flex-col">
      <div className="flex flex-none items-center gap-2 border-b border-border-subtle px-3 py-2">
        <div className="min-w-0 flex-1"><h3 className="truncate text-sm font-medium">{match.log_file}:{match.line}</h3><p className="truncate text-2xs text-txt-tertiary">{task.name} · Log context at match</p></div>
        <CopyButton value={content} disabled={loading || Boolean(error)} label="Copy log context" />
        <button type="button" className="touch-target rounded-md p-2 hover:bg-surface-overlay" aria-label="Close log preview" onClick={onClose}><X className="h-4 w-4" /></button>
      </div>
      <div className="flex-none border-b border-border-subtle bg-accent/5 px-3 py-2"><SearchMatchContext match={match} /></div>
      {loading ? <p role="status" className="flex items-center gap-2 p-4 text-xs"><LoaderCircle className="h-4 w-4 motion-safe:animate-spin" />Loading log context…</p>
        : error ? <div role="alert" className="p-4 text-xs">{error}<button type="button" onClick={() => setRetry(value => value + 1)} className="ml-2 text-accent underline">Retry</button></div>
          : <pre tabIndex={0} aria-label="Log context" className="min-h-0 flex-1 overflow-auto p-3 font-mono text-xs leading-5">{content}</pre>}
      <p className="flex-none border-t border-border-subtle px-3 py-2 text-2xs text-txt-tertiary">Up to 32 KB around this match. Search covers the complete log files.</p>
    </div>
  </dialog>
}
