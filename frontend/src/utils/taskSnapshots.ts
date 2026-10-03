import type { Task } from '../types'

// Task responses contain JSON values. Compare all fields so reused rows also
// retain correct click actions, search locations, and task detail data.
function equalJson(left: unknown, right: unknown, depth = 0): boolean {
  if (left === right) return true
  if (depth > 100 || !left || !right || typeof left !== 'object' || typeof right !== 'object'
    || Array.isArray(left) !== Array.isArray(right)) return false

  const previous = left as Record<string, unknown>
  const incoming = right as Record<string, unknown>
  const keys = Object.keys(previous)
  return keys.length === Object.keys(incoming).length && keys.every(key => (
    Object.prototype.hasOwnProperty.call(incoming, key)
    && equalJson(previous[key], incoming[key], depth + 1)
  ))
}

export function retainTaskSnapshots(previous: Task[], incoming: Task[]): Task[] {
  if (previous === incoming) return previous
  const byName = new Map(previous.map(task => [task.name, task]))
  const tasks = incoming.map(task => {
    const existing = byName.get(task.name)
    return existing && equalJson(existing, task) ? existing : task
  })
  return tasks.length === previous.length && tasks.every((task, index) => task === previous[index])
    ? previous
    : tasks
}
