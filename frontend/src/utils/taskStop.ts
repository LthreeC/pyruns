import { t } from '@/i18n'
import { taskStatusLabel } from '@/theme/tokens'
import type { Task } from '@/types'

export function isActiveTask(task: Task) {
  return task.status === 'queued' || task.status === 'running'
}

export function stopOnExecutionDevice(task: Task) {
  return isActiveTask(task) && (task.task_storage === 'remote' || task.runner_location === 'remote')
}

export function canStopTask(task: Task) {
  return isActiveTask(task) && !stopOnExecutionDevice(task)
}

export function describeTaskStop(task: Task) {
  return t("Stop '{{name}}'?", { name: task.display_name ?? task.name })
}

export function taskStopFeedback(task: Task | null) {
  if (task?.status === 'cancelled') return 'Task cancelled'
  if (task?.status === 'completed' || task?.status === 'failed') return taskStatusLabel(task)
  return 'Cancel requested'
}
