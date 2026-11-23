import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { getMonitorLogPositions } from '@/api'
import { usePolling } from '@/hooks/usePolling'
import type { MonitorLogPosition, Task } from '@/types'

const ACTIVITY_POLL_MS = 2000
// Training can run for several dozen seconds between progress messages.
const ACTIVITY_IDLE_MS = 90_000
const MAX_WATCHED_TASKS = 200
const NO_ACTIVITY: ReadonlySet<string> = new Set()
let retainedActivity: { workspace: string; deadlines: Map<string, number> } | undefined

function activityKey(workspace: string, task: Task) {
  return JSON.stringify([workspace, task.name, task.task_identity || task.dir, task.run_index])
}

export function useMonitorLogActivity(tasks: Task[], workspace: string, visible: boolean, followedTask: string | null) {
  const samples = useRef(new Map<string, Pick<MonitorLogPosition, 'identity' | 'size'>>())
  const expires = useMemo(() => {
    return retainedActivity?.workspace === workspace ? retainedActivity.deadlines : new Map<string, number>()
  }, [workspace])
  const timer = useRef<number | undefined>(undefined)
  const request = useRef<AbortController | null>(null)
  const [active, setActive] = useState(NO_ACTIVITY)
  const watched = useMemo(() => {
    const rows = new Map<string, { key: string; runIndex: number }>()
    for (const task of tasks) {
      if (task.name === followedTask || task.status !== 'running' || !task.run_index
        || !(task.runner_lease_expired === false || task.local_process_verified)) continue
      rows.set(task.name, { key: activityKey(workspace, task), runIndex: task.run_index })
      if (rows.size === MAX_WATCHED_TASKS) break
    }
    return rows
  }, [tasks, workspace, followedTask])
  const watchedRef = useRef(watched)
  watchedRef.current = watched
  const polling = visible && Boolean(workspace) && watched.size > 0

  const publish = useCallback(function expireLogActivity(added = false) {
    window.clearTimeout(timer.current)
    const now = performance.now()
    let changed = added
    let next = Infinity
    for (const [key, deadline] of expires) {
      if (deadline <= now) {
        expires.delete(key)
        changed = true
      } else next = Math.min(next, deadline)
    }
    if (changed) setActive(expires.size ? new Set(expires.keys()) : NO_ACTIVITY)
    timer.current = Number.isFinite(next) ? window.setTimeout(expireLogActivity, Math.ceil(next - now)) : undefined
  }, [expires])

  const record = useCallback((keys: string[]) => {
    if (document.visibilityState === 'hidden') return
    const now = performance.now()
    let added = false
    for (const key of keys) {
      added ||= !expires.has(key)
      expires.set(key, now + ACTIVITY_IDLE_MS)
    }
    // Keep the same animation mounted while output is arriving.
    if (added) publish(true)
  }, [expires, publish])

  useLayoutEffect(() => {
    if (workspace) retainedActivity = { workspace, deadlines: expires }
    // Restore confirmed activity before the first paint on return, with its original expiry.
    if (visible) publish(true)
    return () => {
      window.clearTimeout(timer.current)
      // Re-establish baselines on return so missed history does not look like new output.
      samples.current.clear()
    }
  }, [expires, publish, workspace, visible])

  useEffect(() => () => request.current?.abort(), [workspace, polling])

  useEffect(() => {
    const current = new Set(tasks.filter(task => task.status === 'running').map(task => activityKey(workspace, task)))
    for (const key of samples.current.keys()) {
      if (!current.has(key)) samples.current.delete(key)
    }
  }, [tasks, workspace])

  usePolling(async () => {
    const controller = new AbortController()
    request.current = controller
    try {
      const { items } = await getMonitorLogPositions([...watched.keys()], controller.signal)
      if (controller.signal.aborted || document.visibilityState === 'hidden') return
      const appended: string[] = []
      for (const { name, run_index, identity, size } of items) {
        const row = watched.get(name)
        if (!row || row.runIndex !== run_index || row.key !== watchedRef.current.get(name)?.key) continue
        const previous = samples.current.get(row.key)
        if (previous?.identity === identity && size > previous.size) appended.push(row.key)
        samples.current.set(row.key, { identity, size })
      }
      if (appended.length) record(appended)
    } finally {
      if (request.current === controller) request.current = null
    }
  }, ACTIVITY_POLL_MS, polling)

  const markLogActivity = useCallback((task: Task | undefined, identity: string, size: number) => {
    if (task?.status !== 'running' || document.visibilityState === 'hidden') return
    const key = activityKey(workspace, task)
    // Reuse the live cursor as the baseline if this row stops being followed.
    if (identity) samples.current.set(key, { identity, size })
    record([key])
  }, [record, workspace])

  const hasLogActivity = useCallback((task: Task) => visible && task.status === 'running' && active.size > 0
    && active.has(activityKey(workspace, task)), [active, visible, workspace])

  return { markLogActivity, hasLogActivity }
}
