import { describe, expect, it } from 'vitest'

import { recordGpuWaitReceipt, summarizeGpuWait } from './gpuWait'

describe('summarizeGpuWait', () => {
  it('normalizes scheduler diagnostics for a bounded wait', () => {
    expect(summarizeGpuWait({
      state: 'waiting',
      requested_gpu_count: 2,
      eligible_gpu_count: 1,
      waited_seconds: 12.4,
      max_wait_seconds: 60,
    })).toEqual({
      requestedGpuCount: 2,
      eligibleGpuCount: 1,
      waitedSeconds: 12.4,
      remainingSeconds: 47.6,
    })
  })

  it('prefers the server remaining time and handles incomplete payloads', () => {
    expect(summarizeGpuWait({
      state: 'waiting',
      requested_gpu_count: 0,
      eligible_gpu_count: Number.NaN,
      waited_seconds: -1,
      max_wait_seconds: 60,
      remaining_seconds: 8,
    })).toEqual({
      requestedGpuCount: 1,
      eligibleGpuCount: null,
      waitedSeconds: 0,
      remainingSeconds: 8,
    })
    expect(summarizeGpuWait(undefined).remainingSeconds).toBeNull()
  })

  it('advances from the server sample even when the browser wall clock differs', () => {
    const wait = { state: 'waiting', started_at: 1_793_000_000, deadline_at: 1_793_000_040,
      waited_seconds: 30, remaining_seconds: 10 }
    recordGpuWaitReceipt(wait, 100_000)
    expect(summarizeGpuWait(wait, 104_000)).toMatchObject({ waitedSeconds: 34, remainingSeconds: 6 })
    expect(summarizeGpuWait(wait, 111_000)).toMatchObject({ waitedSeconds: 41, remainingSeconds: 0 })
    expect(summarizeGpuWait(wait)).toMatchObject({ waitedSeconds: 30, remainingSeconds: 10 })
  })

  it('supports older absolute timestamps and unlimited waits', () => {
    const startedAt = 1_793_000_000
    expect(summarizeGpuWait({ state: 'waiting', started_at: startedAt, deadline_at: startedAt + 60 },
      (startedAt + 11) * 1000)).toMatchObject({ waitedSeconds: 11, remainingSeconds: 49 })
    const unlimited = { state: 'waiting', waited_seconds: 1, remaining_seconds: null, max_wait_seconds: 0 }
    recordGpuWaitReceipt(unlimited, 100_000)
    expect(summarizeGpuWait(unlimited, 105_000)).toMatchObject({ waitedSeconds: 6, remainingSeconds: null })
  })
})
