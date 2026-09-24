import type { GPUMetric } from '@/types'

type GpuMemory = Pick<GPUMetric, 'mem_used' | 'mem_total'>

function memoryValues(gpu: GpuMemory): [number, number] | null {
  const used = gpu.mem_used
  const total = gpu.mem_total
  return used != null && total != null
    && Number.isFinite(used) && Number.isFinite(total)
    && total > 0 && used >= 0 && used <= total
    ? [used, total]
    : null
}

export function gpuMemoryUsedPercent(gpu: GpuMemory): number | null {
  const values = memoryValues(gpu)
  return values ? (values[0] / values[1]) * 100 : null
}

export function gpuFreeMemoryMb(gpu: GpuMemory): number | null {
  const values = memoryValues(gpu)
  return values ? values[1] - values[0] : null
}

export function summarizeGpuMetrics(gpus: GPUMetric[]) {
  const utils = gpus.map(gpu => gpu.util)
  const totals = gpus.map(gpu => gpu.mem_total)
  const memory = gpus.map(memoryValues)
  const averageUtil = utils.length > 0
    && utils.every((value): value is number => value != null && Number.isFinite(value) && value >= 0 && value <= 100)
    ? utils.reduce((sum, value) => sum + value, 0) / utils.length
    : null
  const largestMemoryGiB = totals.length > 0
    && totals.every((value): value is number => value != null && Number.isFinite(value) && value > 0)
    ? Math.max(...totals) / 1024
    : null
  const memoryPercent = memory.length > 0 && memory.every((value): value is [number, number] => value != null)
    ? memory.reduce((sum, value) => sum + value[0], 0) / memory.reduce((sum, value) => sum + value[1], 0) * 100
    : null
  return { averageUtil, memoryPercent, largestMemoryGiB }
}
