import type { GPUWaitStatus } from '@/types'
import { parseTaskTimestampMillis } from './taskRuntime'

export interface GPUWaitSummary {
  requestedGpuCount: number
  eligibleGpuCount: number | null
  waitedSeconds: number
  remainingSeconds: number | null
}

const receivedAt = new WeakMap<GPUWaitStatus, number>()

export function recordGpuWaitReceipt(wait: GPUWaitStatus | null | undefined, nowMillis: number) {
  if (wait && typeof wait === 'object') receivedAt.set(wait, nowMillis)
}

function finite(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

export function summarizeGpuWait(wait: GPUWaitStatus | null | undefined, nowMillis?: number): GPUWaitSummary {
  const requested = finite(wait?.requested_gpu_count)
  const eligible = finite(wait?.eligible_gpu_count)
  const startedAt = parseTaskTimestampMillis(wait?.started_at)
  const deadline = parseTaskTimestampMillis(wait?.deadline_at)
  const maxWait = finite(wait?.max_wait_seconds)
  const sampledWait = finite(wait?.waited_seconds)
  const sampledRemaining = finite(wait?.remaining_seconds)
  const receipt = wait ? receivedAt.get(wait) : undefined
  // Advance from the server's elapsed duration using the browser's clock delta.
  // The browser and runner can have different wall clocks.
  const advance = nowMillis === undefined || receipt === undefined ? 0 : Math.max(0, (nowMillis - receipt) / 1000)
  const waitedSeconds = sampledWait !== null
    ? Math.max(0, sampledWait) + advance
    : startedAt !== null && nowMillis !== undefined ? Math.max(0, (nowMillis - startedAt) / 1000) : 0
  const limit = deadline !== null && startedAt !== null ? (deadline - startedAt) / 1000 : maxWait
  let remainingSeconds: number | null = null
  if (maxWait !== 0) {
    if (sampledRemaining !== null) remainingSeconds = Math.max(0, sampledRemaining - advance)
    else if (limit !== null && limit > 0) remainingSeconds = Math.max(0, limit - waitedSeconds)
    else if (deadline !== null && nowMillis !== undefined) remainingSeconds = Math.max(0, (deadline - nowMillis) / 1000)
  }

  return {
    requestedGpuCount: requested === null ? 1 : Math.max(1, Math.trunc(requested)),
    eligibleGpuCount: eligible === null ? null : Math.max(0, Math.trunc(eligible)),
    waitedSeconds,
    remainingSeconds,
  }
}
