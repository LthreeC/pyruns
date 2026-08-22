import { describe, expect, it } from 'vitest'

import { pickInitialMonitorTask } from './monitorSelection'

function task(name: string, status: 'pending' | 'queued' | 'running' | 'completed' | 'failed' | 'cancelled') {
  return { name, status } as any
}

describe('pickInitialMonitorTask', () => {
  it('prefers a running task', () => {
    expect(pickInitialMonitorTask([
      task('queued', 'queued'),
      task('running', 'running'),
      task('completed', 'completed'),
    ])?.name).toBe('running')
  })

  it('falls back to queued, then the first recent task', () => {
    expect(pickInitialMonitorTask([
      task('queued', 'queued'),
      task('completed', 'completed'),
    ])?.name).toBe('queued')
    expect(pickInitialMonitorTask([
      task('completed', 'completed'),
      task('failed', 'failed'),
    ])?.name).toBe('completed')
  })

  it('returns no selection when there are no tasks', () => {
    expect(pickInitialMonitorTask([])).toBeUndefined()
  })
})
