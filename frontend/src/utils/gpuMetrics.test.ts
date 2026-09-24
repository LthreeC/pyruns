import { describe, expect, it } from 'vitest'
import type { GPUMetric } from '@/types'
import { gpuFreeMemoryMb, gpuMemoryUsedPercent, summarizeGpuMetrics } from './gpuMetrics'

const gpu: GPUMetric = {
  id: 0, index: 0, name: 'GPU', uuid: 'GPU-A',
  util: 0, mem_used: 0, mem_total: 8192, processes: [],
}

describe('GPU telemetry with unavailable readings', () => {
  it('distinguishes an idle GPU from an unavailable reading', () => {
    expect(gpuMemoryUsedPercent(gpu)).toBe(0)
    expect(gpuFreeMemoryMb(gpu)).toBe(8192)
    expect(summarizeGpuMetrics([gpu])).toEqual({ averageUtil: 0, memoryPercent: 0, largestMemoryGiB: 8 })
    expect(summarizeGpuMetrics([])).toEqual({ averageUtil: null, memoryPercent: null, largestMemoryGiB: null })
  })

  it.each([null, NaN, Infinity, -1, 9000])('keeps unavailable or invalid used memory %s unknown', used => {
    const missing = { ...gpu, mem_used: used }
    expect(gpuMemoryUsedPercent(missing)).toBeNull()
    expect(gpuFreeMemoryMb(missing)).toBeNull()
    expect(summarizeGpuMetrics([gpu, missing])).toEqual({ averageUtil: 0, memoryPercent: null, largestMemoryGiB: 8 })
  })

  it('does not advertise a complete average or capacity when a GPU has missing readings', () => {
    const missing = { ...gpu, util: null, mem_total: null }
    expect(summarizeGpuMetrics([gpu, missing])).toEqual({ averageUtil: null, memoryPercent: null, largestMemoryGiB: null })
  })

  it('weights memory use by capacity while averaging device utilization', () => {
    const larger = { ...gpu, util: 80, mem_used: 8192, mem_total: 24576 }
    expect(summarizeGpuMetrics([gpu, larger])).toEqual({ averageUtil: 40, memoryPercent: 25, largestMemoryGiB: 24 })
  })
})
