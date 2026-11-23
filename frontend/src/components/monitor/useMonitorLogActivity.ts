import { useCallback, useEffect, useRef, useState } from 'react'
import type { Task } from '@/types'

const ACTIVITY_IDLE_MS = 2600
const NO_ACTIVITY: ReadonlySet<string> = new Set()

function activityKey(workspace: string, task: Task) {
  return JSON.stringify([workspace, task.name, task.task_identity || task.dir, task.run_index])
}

export function useMonitorLogActivity(tasks: Task[], workspace: string, visible: boolean, followedTask: string | null) {
  const samples = useRef(new Map<string, NonNullable<Task['log_position']>>())
  const expires = useRef(new Map<string, number>())
  const timer = useRef<number | undefined>(undefined)
  const [active, setActive] = useState(NO_ACTIVITY)

  const publish = useCallback(function expireLogActivity(added = false) {
    window.clearTimeout(timer.current)
    const now = performance.now()
    let changed = added
    let next = Infinity
    for (const [key, deadline] of expires.current) {
      if (deadline <= now) {
        expires.current.delete(key)
        changed = true
      } else next = Math.min(next, deadline)
    }
    if (changed) setActive(expires.current.size ? new Set(expires.current.keys()) : NO_ACTIVITY)
    timer.current = Number.isFinite(next) ? window.setTimeout(expireLogActivity, Math.ceil(next - now)) : undefined
  }, [])

  const record = useCallback((keys: string[]) => {
    if (document.visibilityState === 'hidden') return
    const now = performance.now()
    let added = false
    for (const key of keys) {
      added ||= !expires.current.has(key)
      expires.current.set(key, now + ACTIVITY_IDLE_MS)
    }
    // Keep the same animation mounted while output is arriving.
    if (added) publish(true)
  }, [publish])

  useEffect(() => {
    setActive(NO_ACTIVITY)
    return () => {
      window.clearTimeout(timer.current)
      expires.current.clear()
      samples.current.clear()
    }
  }, [workspace, visible])

  useEffect(() => {
    if (!visible) return
    const next = new Map<string, NonNullable<Task['log_position']>>()
    const appended: string[] = []
    for (const task of tasks) {
      const position = task.log_position
      if (task.status !== 'running' || !position?.identity) continue
      const key = activityKey(workspace, task)
      const previous = samples.current.get(key)
      if (task.name !== followedTask && previous?.identity === position.identity && position.size > previous.size) {
        appended.push(key)
      }
      next.set(key, position)
    }
    samples.current = next
    if (appended.length) record(appended)
  }, [tasks, workspace, visible, followedTask, record])

  const markLogActivity = useCallback((task: Task | undefined) => {
    if (task?.status === 'running') record([activityKey(workspace, task)])
  }, [record, workspace])

  const hasLogActivity = useCallback((task: Task) => visible && task.status === 'running' && active.size > 0
    && active.has(activityKey(workspace, task)), [active, visible, workspace])

  return { markLogActivity, hasLogActivity }
}
