import type { Task } from '@/types'

/** Pick the most useful task to show when Monitor has no explicit selection. */
export function pickInitialMonitorTask(tasks: Task[]): Task | undefined {
  return tasks.find(task => task.status === 'running')
    ?? tasks.find(task => task.status === 'queued')
    ?? tasks[0]
}
