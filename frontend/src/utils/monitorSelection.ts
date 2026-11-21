import type { Task } from '@/types'
import { taskStatusLabel } from '../theme/tokens'

/** Pick the most useful task to show when Monitor has no explicit selection. */
export function pickInitialMonitorTask(tasks: Task[]): Task | undefined {
  return tasks.find(task => taskStatusLabel(task) === 'Running')
    ?? tasks.find(task => taskStatusLabel(task) === 'Queued')
    ?? tasks[0]
}
