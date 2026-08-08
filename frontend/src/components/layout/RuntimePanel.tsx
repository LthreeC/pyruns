import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  CircleGauge,
  Loader2,
  RefreshCw,
  RotateCcw,
  X,
} from 'lucide-react'
import clsx from 'clsx'
import * as api from '@/api'
import type { GPUMetric, RuntimeInfo, SystemMetrics } from '@/types'
import { useRuntimeStore, useThemeStore, useToastStore, useWorkspaceStore } from '@/store'
import { errorMessage } from '@/utils/errors'

const CodeTextEditor = lazy(() => import('@/components/shared/CodeTextEditor'))

interface RuntimePanelProps {
  open: boolean
  left: number
  onClose: () => void
}

type RuntimePage = 'python' | 'env' | 'gpu'
type PythonRuntimeMode = 'follow' | 'conda' | 'python'
type GpuTaskMode = 'single' | 'multi'
type GpuSelectionMode = 'auto' | 'specified'

function quoteEnvValue(value: string) {
  if (value === '') {
    return '""'
  }
  if (/^[A-Za-z0-9_@%+=:,./-]+$/.test(value)) {
    return value
  }
  if (!value.includes("'")) {
    return `'${value}'`
  }
  return `"${value
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"')
    .replace(/\$/g, '\\$')
    .replace(/`/g, '\\`')}"`
}

function formatEnv(env: Record<string, string>) {
  return Object.entries(env || {})
    .map(([key, value]) => `${key}=${quoteEnvValue(String(value))}`)
    .join('\n')
}

function cleanSettingText(value: unknown) {
  return String(value || '').trim()
}

function runtimeLabel(runtime: RuntimeInfo | null, settings?: Record<string, any>) {
  if (!runtime) {
    const pythonExecutable = cleanSettingText(settings?.python_executable)
    if (pythonExecutable) {
      return `Python: ${pythonExecutable.split(/[\\/]/).pop() || 'custom'}`
    }
    const condaEnv = cleanSettingText(settings?.conda_env)
    if (condaEnv) {
      return `Conda: ${condaEnv}`
    }
    return 'Follow process'
  }
  if (runtime.python_executable) {
    return `Python: ${runtime.python_executable.split(/[\\/]/).pop() || 'custom'}`
  }
  if (runtime.conda_env) {
    return `Conda: ${runtime.conda_env}`
  }
  return runtime.process.conda_env ? `Follow: ${runtime.process.conda_env}` : 'Follow process'
}

function modeFromRuntime(runtime: RuntimeInfo | null): PythonRuntimeMode {
  if (runtime?.python_executable) {
    return 'python'
  }
  if (runtime?.conda_env) {
    return 'conda'
  }
  return 'follow'
}

function modeFromSettings(settings?: Record<string, any>): PythonRuntimeMode {
  if (cleanSettingText(settings?.python_executable)) {
    return 'python'
  }
  if (cleanSettingText(settings?.conda_env)) {
    return 'conda'
  }
  return 'follow'
}

function parseDeviceIds(value: string) {
  return Array.from(new Set(
    value
      .split(',')
      .map(item => item.trim())
      .filter(Boolean)
      .map(item => Number(item))
      .filter(item => Number.isInteger(item) && item >= 0)
  ))
}

function formatDeviceIds(value?: number[]) {
  return (value || []).join(',')
}

function formatDeviceIdsSetting(value: unknown) {
  if (Array.isArray(value)) {
    return formatDeviceIds(
      value
        .map(item => Number(item))
        .filter(item => Number.isInteger(item) && item >= 0)
    )
  }
  if (typeof value === 'string') {
    return formatDeviceIds(parseDeviceIds(value))
  }
  return ''
}

function numberInputValue(value: string, fallback: number, minimum = 0) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) {
    return fallback
  }
  return Math.max(minimum, parsed)
}

function boundedNumberInputValue(value: string, fallback: number, minimum: number, maximum: number) {
  return Math.min(maximum, numberInputValue(value, fallback, minimum))
}

function settingNumber(value: unknown, fallback: number, minimum = 0, maximum = Number.POSITIVE_INFINITY) {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) {
    return fallback
  }
  return Math.min(maximum, Math.max(minimum, parsed))
}

function settingBool(value: unknown, fallback: boolean) {
  if (typeof value === 'boolean') {
    return value
  }
  const text = cleanSettingText(value).toLowerCase()
  if (['1', 'true', 'yes', 'on'].includes(text)) {
    return true
  }
  if (['0', 'false', 'no', 'off'].includes(text)) {
    return false
  }
  return fallback
}

function isInsidePanel(panel: HTMLDivElement | null, target: EventTarget | null) {
  return Boolean(panel && target instanceof Node && panel.contains(target))
}

function gpuKey(gpu: GPUMetric) {
  return gpu.uuid || String(gpu.id)
}

function gpuMemoryGiB(memoryMb: number) {
  return Math.max(0, memoryMb) / 1024
}

function formatGiB(value: number) {
  if (!Number.isFinite(value)) {
    return '--'
  }
  const digits = Number.isInteger(value) ? 0 : value >= 10 ? 1 : 2
  return `${value.toFixed(digits)} GiB`
}

function gpuRuleReasons(
  gpu: GPUMetric,
  minFreeMemoryGiB: number,
  maxMemoryUsedPct: number,
  maxComputeUsedPct: number,
) {
  const reasons: string[] = []
  const totalGiB = gpuMemoryGiB(gpu.mem_total)
  const freeGiB = gpuMemoryGiB(gpu.mem_total - gpu.mem_used)
  const memoryUsedPct = gpu.mem_total > 0 ? (gpu.mem_used / gpu.mem_total) * 100 : 0

  if (minFreeMemoryGiB > totalGiB) {
    reasons.push(`Needs ${formatGiB(minFreeMemoryGiB)} free; this GPU has ${formatGiB(totalGiB)} total`)
  } else if (freeGiB < minFreeMemoryGiB) {
    reasons.push(`Free memory ${formatGiB(freeGiB)} is below ${formatGiB(minFreeMemoryGiB)}`)
  }
  if (memoryUsedPct > maxMemoryUsedPct) {
    reasons.push(`Memory use ${memoryUsedPct.toFixed(0)}% is above ${maxMemoryUsedPct}%`)
  }
  if (gpu.util > maxComputeUsedPct) {
    reasons.push(`Compute use ${gpu.util.toFixed(0)}% is above ${maxComputeUsedPct}%`)
  }
  return reasons
}

export default function RuntimePanel({ open, left, onClose }: RuntimePanelProps) {
  const refreshWorkspace = useWorkspaceStore(s => s.fetch)
  const workspaceSettings = useWorkspaceStore(s => s.workspace?.settings)
  const cachedRuntime = useRuntimeStore(s => s.runtime)
  const runtimeLoading = useRuntimeStore(s => s.loading)
  const fetchRuntime = useRuntimeStore(s => s.fetchRuntime)
  const updateRuntime = useRuntimeStore(s => s.updateRuntime)
  const theme = useThemeStore(s => s.theme)
  const panelRef = useRef<HTMLDivElement>(null)
  const closeGestureRef = useRef<{
    pointerId: number
    startedInside: boolean
    startX: number
    startY: number
    dragged: boolean
  } | null>(null)
  const runtimeLoadSeqRef = useRef(0)
  const gpuMetricsLoadSeqRef = useRef(0)
  const notify = useToastStore(state => state.notify)
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(cachedRuntime)
  const [envText, setEnvText] = useState('')
  const [pythonPath, setPythonPath] = useState('')
  const [condaEnv, setCondaEnv] = useState('')
  const [condaExecutable, setCondaExecutable] = useState('conda')
  const [runtimeMode, setRuntimeMode] = useState<PythonRuntimeMode>('follow')
  const [activePage, setActivePage] = useState<RuntimePage>('python')
  const [gpuSchedulerEnabled, setGpuSchedulerEnabled] = useState(false)
  const [gpuTaskMode, setGpuTaskMode] = useState<GpuTaskMode>('single')
  const [gpuSelectionMode, setGpuSelectionMode] = useState<GpuSelectionMode>('auto')
  const [gpuCount, setGpuCount] = useState('1')
  const [gpuDeviceIds, setGpuDeviceIds] = useState('')
  const [gpuMemoryUsedPct, setGpuMemoryUsedPct] = useState('40')
  const [gpuMinFreeMemoryGb, setGpuMinFreeMemoryGb] = useState('40')
  const [gpuComputeUsedPct, setGpuComputeUsedPct] = useState('30')
  const [gpuStableSeconds, setGpuStableSeconds] = useState('15')
  const [gpuMaxWaitHours, setGpuMaxWaitHours] = useState(48)
  const [gpuMaxTasksPerGpu, setGpuMaxTasksPerGpu] = useState('1')
  const [gpuRespectCudaVisibleDevices, setGpuRespectCudaVisibleDevices] = useState(true)
  const [gpuRequireSameModel, setGpuRequireSameModel] = useState(false)
  const [showCondaAdvanced, setShowCondaAdvanced] = useState(false)
  const [gpuMetrics, setGpuMetrics] = useState<SystemMetrics | null>(null)
  const [gpuMetricsLoading, setGpuMetricsLoading] = useState(false)
  const [gpuMetricsError, setGpuMetricsError] = useState('')
  const [expandedGpuKey, setExpandedGpuKey] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const condaAvailable = !!runtime?.conda.available
  const envCount = Object.keys(runtime?.global_env || {}).length
  const currentLabel = useMemo(() => runtimeLabel(runtime, workspaceSettings), [runtime, workspaceSettings])
  const codeMirrorTheme = theme === 'dark' ? 'dark' : 'light'
  const selectedConda = useMemo(() => {
    const found = runtime?.conda.envs.find(env => env.name === condaEnv)
    if (found) {
      return found
    }
    if (condaEnv && condaEnv === runtime?.process.conda_env) {
      return {
        name: runtime.process.conda_env,
        path: runtime.process.conda_prefix,
        python_executable: runtime.process.python_executable,
        active: true,
      }
    }
    return null
  }, [runtime, condaEnv])
  const requestedGpuCount = gpuTaskMode === 'multi'
    ? numberInputValue(gpuCount, 1, 1)
    : 1
  const selectedGpuIds = useMemo(() => parseDeviceIds(gpuDeviceIds), [gpuDeviceIds])
  const candidateGpus = useMemo(() => {
    const allGpus = gpuMetrics?.gpus || []
    if (gpuSelectionMode !== 'specified') {
      return allGpus
    }
    const selected = new Set(selectedGpuIds)
    return allGpus.filter(gpu => selected.has(gpu.index))
  }, [gpuMetrics, gpuSelectionMode, selectedGpuIds])
  const unavailableSelectedGpuIds = useMemo(() => {
    const allGpus = gpuMetrics?.gpus || []
    if (gpuSelectionMode !== 'specified' || !gpuMetrics || gpuMetricsError) {
      return []
    }
    const available = new Set(allGpus.map(gpu => gpu.index))
    return selectedGpuIds.filter(gpuId => !available.has(gpuId))
  }, [gpuMetrics, gpuMetricsError, gpuSelectionMode, selectedGpuIds])
  const gpuPreviewRows = useMemo(() => {
    const minFree = numberInputValue(gpuMinFreeMemoryGb, 0, 0)
    const memoryLimit = boundedNumberInputValue(gpuMemoryUsedPct, 40, 0, 100)
    const computeLimit = boundedNumberInputValue(gpuComputeUsedPct, 30, 0, 100)
    return candidateGpus.map(gpu => ({
      gpu,
      reasons: gpuRuleReasons(gpu, minFree, memoryLimit, computeLimit),
    }))
  }, [candidateGpus, gpuComputeUsedPct, gpuMemoryUsedPct, gpuMinFreeMemoryGb])
  const passingGpuCount = gpuPreviewRows.filter(row => row.reasons.length === 0).length
  const gpuValidationIssues = useMemo(() => {
    if (!gpuSchedulerEnabled) {
      return []
    }

    const issues: string[] = []
    const rawCount = Number(gpuCount)
    const rawMemoryLimit = Number(gpuMemoryUsedPct)
    const rawFreeMemory = Number(gpuMinFreeMemoryGb)
    const rawComputeLimit = Number(gpuComputeUsedPct)
    const rawStableSeconds = Number(gpuStableSeconds)
    const rawWaitHours = Number(gpuMaxWaitHours)
    const rawMaxTasks = Number(gpuMaxTasksPerGpu)
    const rawDeviceTokens = gpuDeviceIds.split(',').map(item => item.trim()).filter(Boolean)

    if (!Number.isInteger(rawCount) || rawCount < 1) {
      issues.push('GPUs per task must be a whole number of at least 1.')
    }
    if (!gpuMemoryUsedPct.trim() || !Number.isFinite(rawMemoryLimit) || rawMemoryLimit < 0 || rawMemoryLimit > 100) {
      issues.push('Maximum memory use must be between 0% and 100%.')
    }
    if (!gpuMinFreeMemoryGb.trim() || !Number.isFinite(rawFreeMemory) || rawFreeMemory < 0) {
      issues.push('Minimum free memory must be 0 GiB or more.')
    }
    if (!gpuComputeUsedPct.trim() || !Number.isFinite(rawComputeLimit) || rawComputeLimit < 0 || rawComputeLimit > 100) {
      issues.push('Maximum compute use must be between 0% and 100%.')
    }
    if (!gpuStableSeconds.trim() || !Number.isFinite(rawStableSeconds) || rawStableSeconds < 1) {
      issues.push('Stability window must be at least 1 second.')
    }
    if (!Number.isFinite(rawWaitHours) || rawWaitHours < 1) {
      issues.push('Maximum wait must be at least 1 hour.')
    }
    if (!gpuMaxTasksPerGpu.trim() || !Number.isInteger(rawMaxTasks) || rawMaxTasks < 1) {
      issues.push('Tasks per GPU must be a whole number of at least 1.')
    }
    if (gpuSelectionMode === 'specified') {
      if (rawDeviceTokens.some(item => !/^\d+$/.test(item))) {
        issues.push('GPU selection accepts comma-separated numeric indices, such as 0,1.')
      } else if (selectedGpuIds.length !== requestedGpuCount) {
        issues.push(`Choose exactly ${requestedGpuCount} unique GPU ${requestedGpuCount === 1 ? 'index' : 'indices'}.`)
      } else if (unavailableSelectedGpuIds.length > 0) {
        const label = unavailableSelectedGpuIds.join(', ')
        issues.push(`GPU ${unavailableSelectedGpuIds.length === 1 ? 'index' : 'indices'} ${label} ${unavailableSelectedGpuIds.length === 1 ? 'is' : 'are'} not detected on this machine.`)
      }
    }

    if (candidateGpus.length > 0 && requestedGpuCount > candidateGpus.length) {
      issues.push(`This machine exposes only ${candidateGpus.length} matching GPU${candidateGpus.length === 1 ? '' : 's'}.`)
    }
    if (
      Number.isFinite(rawFreeMemory)
      && rawFreeMemory >= 0
      && candidateGpus.length > 0
      && candidateGpus.every(gpu => rawFreeMemory > gpuMemoryGiB(gpu.mem_total))
    ) {
      const largestGpuGiB = Math.max(...candidateGpus.map(gpu => gpuMemoryGiB(gpu.mem_total)))
      issues.push(
        `${formatGiB(rawFreeMemory)} free is not possible on the configured GPU${candidateGpus.length === 1 ? '' : 's'} `
        + `(${formatGiB(largestGpuGiB)} maximum physical memory).`,
      )
    }
    return Array.from(new Set(issues))
  }, [
    candidateGpus,
    gpuCount,
    gpuDeviceIds,
    gpuComputeUsedPct,
    gpuMaxWaitHours,
    gpuMaxTasksPerGpu,
    gpuMemoryUsedPct,
    gpuMinFreeMemoryGb,
    gpuSchedulerEnabled,
    gpuSelectionMode,
    gpuStableSeconds,
    requestedGpuCount,
    selectedGpuIds,
    unavailableSelectedGpuIds,
  ])
  const gpuValidationMessage = gpuValidationIssues[0] || ''
  const gpuReadinessUnavailable = gpuSchedulerEnabled && !!gpuMetricsError && !gpuMetrics
  const gpuWaitingNow = gpuSchedulerEnabled
    && !!gpuMetrics
    && passingGpuCount < requestedGpuCount

  const applyRuntimeState = (next: RuntimeInfo) => {
    setRuntime(next)
    setEnvText(formatEnv(next.global_env))
    setPythonPath(next.python_executable)
    setCondaEnv(next.conda_env)
    setCondaExecutable(next.conda_executable || 'conda')
    setRuntimeMode(modeFromRuntime(next))
    setGpuSchedulerEnabled(next.gpu_scheduler?.enabled ?? false)
    setGpuTaskMode(next.gpu_scheduler?.task_mode === 'multi' ? 'multi' : 'single')
    setGpuSelectionMode(next.gpu_scheduler?.selection_mode === 'specified' ? 'specified' : 'auto')
    setGpuCount(String(next.gpu_scheduler?.gpus_per_task ?? 1))
    setGpuDeviceIds(formatDeviceIds(next.gpu_scheduler?.device_ids))
    setGpuMemoryUsedPct(String(next.gpu_scheduler?.memory_used_pct ?? 40))
    setGpuMinFreeMemoryGb(String(next.gpu_scheduler?.min_free_memory_gb ?? 40))
    setGpuComputeUsedPct(String(next.gpu_scheduler?.compute_used_pct ?? 30))
    setGpuStableSeconds(String(next.gpu_scheduler?.stable_seconds ?? 15))
    setGpuMaxWaitHours(Math.max(1, Math.round((next.gpu_scheduler?.max_wait_seconds ?? 172800) / 3600)))
    setGpuMaxTasksPerGpu(String(next.gpu_scheduler?.max_tasks_per_gpu ?? 1))
    setGpuRespectCudaVisibleDevices(next.gpu_scheduler?.respect_cuda_visible_devices ?? true)
    setGpuRequireSameModel(next.gpu_scheduler?.require_same_gpu_model ?? false)
  }

  const applyWorkspaceRuntimeSettings = (settings?: Record<string, any>) => {
    setRuntime(null)
    setEnvText(formatEnv(settings?.global_env || {}))
    setPythonPath(cleanSettingText(settings?.python_executable))
    setCondaEnv(cleanSettingText(settings?.conda_env))
    setCondaExecutable(cleanSettingText(settings?.conda_executable) || 'conda')
    setRuntimeMode(modeFromSettings(settings))
    setGpuSchedulerEnabled(settingBool(settings?.gpu_scheduler_enabled, false))
    setGpuTaskMode(cleanSettingText(settings?.gpu_scheduler_task_mode) === 'multi' ? 'multi' : 'single')
    setGpuSelectionMode(cleanSettingText(settings?.gpu_scheduler_selection_mode) === 'specified' ? 'specified' : 'auto')
    setGpuCount(String(settingNumber(settings?.gpu_scheduler_gpus_per_task, 1, 1)))
    setGpuDeviceIds(formatDeviceIdsSetting(settings?.gpu_scheduler_device_ids))
    setGpuMemoryUsedPct(String(settingNumber(settings?.gpu_scheduler_memory_used_pct, 40, 0, 100)))
    setGpuMinFreeMemoryGb(String(settingNumber(settings?.gpu_scheduler_min_free_memory_gb, 40, 0)))
    setGpuComputeUsedPct(String(settingNumber(settings?.gpu_scheduler_compute_used_pct, 30, 0, 100)))
    setGpuStableSeconds(String(settingNumber(settings?.gpu_scheduler_stable_seconds, 15, 1)))
    setGpuMaxWaitHours(Math.max(1, Math.round(settingNumber(settings?.gpu_scheduler_max_wait_seconds, 172800, 1) / 3600)))
    setGpuMaxTasksPerGpu(String(settingNumber(settings?.gpu_scheduler_max_tasks_per_gpu, 1, 1)))
    setGpuRespectCudaVisibleDevices(settingBool(settings?.gpu_scheduler_respect_cuda_visible_devices, true))
    setGpuRequireSameModel(settingBool(settings?.gpu_scheduler_require_same_gpu_model, false))
  }

  const loadRuntime = async (showFeedback = false) => {
    const loadSeq = runtimeLoadSeqRef.current + 1
    runtimeLoadSeqRef.current = loadSeq
    setLoading(true)
    setError('')
    try {
      const next = await fetchRuntime()
      if (loadSeq === runtimeLoadSeqRef.current) {
        applyRuntimeState(next)
      }
    } catch (err) {
      if (loadSeq !== runtimeLoadSeqRef.current) {
        return
      }
      const message = errorMessage(err, 'Could not refresh runtime.')
      setError(message)
      if (showFeedback) {
        notify({ tone: 'error', title: 'Could not refresh runtime', detail: message })
      }
    } finally {
      if (loadSeq === runtimeLoadSeqRef.current) {
        setLoading(false)
      }
    }
  }

  const loadGpuMetrics = async (showFeedback = false) => {
    const loadSeq = gpuMetricsLoadSeqRef.current + 1
    gpuMetricsLoadSeqRef.current = loadSeq
    setGpuMetricsLoading(true)
    try {
      const next = await api.getMetrics(false)
      if (loadSeq === gpuMetricsLoadSeqRef.current) {
        setGpuMetrics(next)
        setGpuMetricsError('')
      }
    } catch (err) {
      const message = errorMessage(err, 'GPU readiness preview is unavailable.')
      if (loadSeq === gpuMetricsLoadSeqRef.current) {
        setGpuMetricsError(message)
      }
      if (showFeedback) {
        notify({ tone: 'error', title: 'Could not refresh GPU preview', detail: message })
      }
    } finally {
      if (loadSeq === gpuMetricsLoadSeqRef.current) {
        setGpuMetricsLoading(false)
      }
    }
  }

  useEffect(() => {
    if (open) {
      if (cachedRuntime) {
        applyRuntimeState(cachedRuntime)
      } else {
        applyWorkspaceRuntimeSettings(workspaceSettings)
      }
      void loadRuntime()
    }
  }, [open])

  useEffect(() => {
    if (!open || activePage !== 'gpu') {
      return
    }
    void loadGpuMetrics()
  }, [activePage, open])

  useEffect(() => {
    if (!open) {
      return
    }

    const handleDocumentPointerDown = (event: PointerEvent) => {
      if (closeGestureRef.current) {
        return
      }
      closeGestureRef.current = {
        pointerId: event.pointerId,
        startedInside: isInsidePanel(panelRef.current, event.target),
        startX: event.clientX,
        startY: event.clientY,
        dragged: false,
      }
    }
    const handleDocumentPointerMove = (event: PointerEvent) => {
      const gesture = closeGestureRef.current
      if (!gesture || gesture.pointerId !== event.pointerId) {
        return
      }
      const moved = Math.abs(event.clientX - gesture.startX) > 6 || Math.abs(event.clientY - gesture.startY) > 6
      if (moved) {
        gesture.dragged = true
      }
    }
    const handleDocumentPointerUp = (event: PointerEvent) => {
      const gesture = closeGestureRef.current
      if (!gesture || gesture.pointerId !== event.pointerId) {
        return
      }
      closeGestureRef.current = null
      if (gesture.startedInside || gesture.dragged || isInsidePanel(panelRef.current, event.target)) {
        return
      }
      onClose()
    }
    const handleDocumentPointerCancel = (event: PointerEvent) => {
      if (closeGestureRef.current?.pointerId !== event.pointerId) {
        return
      }
      closeGestureRef.current = null
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        onClose()
        return
      }
      if (event.key !== 'Tab' || !panelRef.current) {
        return
      }
      const focusable = Array.from(panelRef.current.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )).filter(element => !element.hasAttribute('hidden') && element.getClientRects().length > 0)
      if (!focusable.length) {
        event.preventDefault()
        return
      }
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }

    const pointerListenerTimer = window.setTimeout(() => {
      document.addEventListener('pointerdown', handleDocumentPointerDown)
      document.addEventListener('pointermove', handleDocumentPointerMove)
      document.addEventListener('pointerup', handleDocumentPointerUp)
      document.addEventListener('pointercancel', handleDocumentPointerCancel)
    }, 0)
    const focusTimer = window.setTimeout(() => {
      panelRef.current?.querySelector<HTMLElement>('[data-runtime-initial-focus]')?.focus()
    }, 0)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      window.clearTimeout(pointerListenerTimer)
      window.clearTimeout(focusTimer)
      document.removeEventListener('pointerdown', handleDocumentPointerDown)
      document.removeEventListener('pointermove', handleDocumentPointerMove)
      document.removeEventListener('pointerup', handleDocumentPointerUp)
      document.removeEventListener('pointercancel', handleDocumentPointerCancel)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [open, onClose])

  const refreshWorkspaceInBackground = () => {
    void refreshWorkspace().catch(err => {
      notify({
        tone: 'error',
        title: 'Runtime saved, workspace refresh failed',
        detail: errorMessage(err, 'Refresh the workspace to see the latest runtime summary.'),
      })
    })
  }

  const saveRuntime = async (
    payload: Parameters<typeof api.updateRuntimeInfo>[0],
    successTitle = 'Runtime saved',
  ) => {
    runtimeLoadSeqRef.current += 1
    setSaving(true)
    setError('')
    try {
      applyRuntimeState(await updateRuntime(payload, false))
      notify({ tone: 'success', title: successTitle })
      refreshWorkspaceInBackground()
    } catch (err) {
      const message = errorMessage(err, 'Could not save runtime settings.')
      setError(message)
      notify({ tone: 'error', title: 'Could not save runtime', detail: message })
    } finally {
      setSaving(false)
    }
  }

  const savePythonRuntime = () => {
    if (runtimeMode === 'conda') {
      if (!condaEnv) {
        const message = 'Choose a conda environment before saving.'
        setError(message)
        notify({ tone: 'error', title: 'Conda environment required', detail: message })
        return
      }
      void saveRuntime({
        conda_env: condaEnv,
        conda_executable: condaExecutable,
        python_executable: '',
      }, 'Python runtime saved')
      return
    }
    if (runtimeMode === 'python') {
      void saveRuntime({
        python_executable: pythonPath,
        conda_env: '',
      }, 'Python runtime saved')
      return
    }
    void saveRuntime({
      conda_env: '',
      python_executable: '',
    }, 'Python runtime saved')
  }

  const chooseRuntimeMode = (mode: PythonRuntimeMode) => {
    setRuntimeMode(mode)
    setError('')
    if (mode === 'conda' && !condaEnv) {
      const activeConda = runtime?.conda.envs.find(env => env.active)?.name
      setCondaEnv(runtime?.conda_env || runtime?.process.conda_env || activeConda || runtime?.conda.envs[0]?.name || '')
    }
  }

  const saveGpuScheduler = () => {
    if (gpuValidationMessage) {
      const message = gpuValidationMessage
      setError(message)
      notify({ tone: 'error', title: 'Fix GPU scheduling settings', detail: message })
      return
    }

    void saveRuntime({
      gpu_scheduler: {
        enabled: gpuSchedulerEnabled,
        task_mode: gpuTaskMode,
        selection_mode: gpuSelectionMode,
        gpus_per_task: requestedGpuCount,
        device_ids: selectedGpuIds,
        memory_used_pct: boundedNumberInputValue(gpuMemoryUsedPct, 40, 0, 100),
        min_free_memory_gb: numberInputValue(gpuMinFreeMemoryGb, 40, 0),
        compute_used_pct: boundedNumberInputValue(gpuComputeUsedPct, 30, 0, 100),
        stable_seconds: numberInputValue(gpuStableSeconds, 15, 1),
        max_wait_seconds: gpuMaxWaitHours * 3600,
        max_tasks_per_gpu: numberInputValue(gpuMaxTasksPerGpu, 1, 1),
        respect_cuda_visible_devices: gpuRespectCudaVisibleDevices,
        require_same_gpu_model: gpuRequireSameModel,
      },
    }, 'GPU scheduler saved')
  }

  const resetGpuScheduler = () => {
    setError('')
    setExpandedGpuKey(null)
    if (runtime) {
      applyRuntimeState(runtime)
    } else {
      applyWorkspaceRuntimeSettings(workspaceSettings)
    }
  }

  const refreshPanel = () => {
    void loadRuntime(true)
    if (activePage === 'gpu') {
      void loadGpuMetrics(true)
    }
  }

  if (!open) {
    return null
  }

  const modeItems: Array<{
    id: PythonRuntimeMode
    title: string
  }> = [
    {
      id: 'follow',
      title: 'Follow',
    },
    {
      id: 'conda',
      title: 'Conda',
    },
    {
      id: 'python',
      title: 'Path',
    },
  ]

  return (
    <div
      ref={panelRef}
      role="dialog"
      aria-modal="true"
      aria-label="Runtime settings"
      className="runtime-panel fixed bottom-3 z-50 flex max-h-[calc(100vh-24px)] w-[620px] flex-col overflow-hidden rounded-lg border border-border bg-surface-raised shadow-xl"
      style={{ left, maxWidth: `calc(100vw - ${left + 12}px)` }}
      onClick={event => event.stopPropagation()}
    >
      <div className="flex h-12 items-center gap-1.5 border-b border-border-subtle px-2 sm:h-10 sm:gap-2 sm:px-3">
        <div className="hidden min-w-0 flex-1 items-center gap-2 sm:flex">
          <div className="text-sm font-semibold text-txt-primary">Runtime</div>
          <div className="truncate rounded-md bg-surface-overlay px-2 py-0.5 text-2xs text-txt-secondary">
            {currentLabel}
          </div>
        </div>
        <div role="tablist" aria-label="Runtime sections" className="inline-flex rounded-md bg-surface-overlay p-0.5">
          {(['python', 'env', 'gpu'] as RuntimePage[]).map(page => (
            <button
              key={page}
              type="button"
              onClick={() => setActivePage(page)}
              aria-pressed={activePage === page}
              role="tab"
              aria-selected={activePage === page}
              data-runtime-initial-focus={activePage === page ? 'true' : undefined}
              className={clsx(
                'inline-flex h-8 items-center gap-1.5 rounded-md px-2 text-xs font-medium transition-colors sm:h-7 sm:px-2.5',
                activePage === page
                  ? 'bg-surface-raised text-accent shadow-sm'
                  : 'text-txt-secondary hover:text-txt-primary'
              )}
            >
              {page === 'python' ? 'Python' : page === 'env' ? 'Env' : 'GPU'}
              {page === 'env' && envCount > 0 && <span className="h-1.5 w-1.5 rounded-full bg-status-completed" />}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={refreshPanel}
          disabled={loading || runtimeLoading || gpuMetricsLoading || saving}
          className="inline-flex h-10 w-10 items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary disabled:opacity-50 sm:h-8 sm:w-8"
          aria-label="Reload runtime"
        >
          {loading || runtimeLoading || gpuMetricsLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </button>
        <button
          type="button"
          onClick={onClose}
          className="inline-flex h-10 w-10 items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary sm:h-8 sm:w-8"
          aria-label="Close runtime panel"
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {error && (
          <div role="alert" className="mb-3 rounded-md bg-status-failed/10 px-3 py-2 text-sm text-status-failed">
            {error}
          </div>
        )}

        {activePage === 'python' && (
          <section className="space-y-3">
            <div className="flex items-center justify-between gap-3">
              <div className="inline-flex rounded-md bg-surface-overlay p-0.5">
                {modeItems.map(item => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => chooseRuntimeMode(item.id)}
                    className={clsx(
                      'rounded-md px-3 py-1.5 text-xs font-medium transition-colors',
                      runtimeMode === item.id
                        ? 'bg-surface-raised text-accent shadow-sm'
                        : 'text-txt-secondary hover:text-txt-primary'
                    )}
                  >
                    {item.title}
                  </button>
                ))}
              </div>
              <button
                type="button"
                onClick={savePythonRuntime}
                disabled={saving}
                className="inline-flex h-8 items-center justify-center gap-1.5 rounded-md bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
              >
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                Save
              </button>
            </div>

            {runtimeMode === 'follow' && (
              <div className="space-y-1 border-l border-border-subtle pl-3">
                <div className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Python</div>
                <div className="truncate font-mono text-sm text-txt-primary" title={runtime?.process.python_executable}>
                  {runtime?.process.python_executable || 'unknown'}
                </div>
              </div>
            )}

            {runtimeMode === 'conda' && (
              <div className="space-y-3">
                <div>
                  <label className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Conda</label>
                  <select
                    value={condaEnv}
                    disabled={saving}
                    onChange={event => setCondaEnv(event.target.value)}
                    className="h-9 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15 disabled:opacity-50"
                  >
                    <option value="">Choose environment</option>
                    {runtime?.process.conda_env && !runtime.conda.envs.some(env => env.name === runtime.process.conda_env) && (
                      <option value={runtime.process.conda_env}>
                        {runtime.process.conda_env} (current)
                      </option>
                    )}
                    {runtime?.conda.envs.map(env => (
                      <option key={env.name} value={env.name}>
                        {env.name}{env.active ? ' (active)' : ''}
                      </option>
                    ))}
                  </select>
                </div>
                <div className="space-y-0.5 border-l border-border-subtle pl-3">
                  <div className="text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Python</div>
                  <div
                    className="truncate font-mono text-sm text-txt-primary"
                    title={selectedConda?.python_executable || runtime?.process.python_executable}
                  >
                    {selectedConda?.python_executable || 'Choose a conda environment to preview Python path'}
                  </div>
                  {selectedConda?.path && (
                    <div className="truncate text-2xs text-txt-tertiary" title={selectedConda.path}>
                      {selectedConda.path}
                    </div>
                  )}
                </div>
                {runtime?.conda.error && (
                  <div className="rounded-md bg-status-failed/10 px-3 py-2 text-sm text-status-failed">
                    {runtime.conda.error}
                  </div>
                )}
                <button
                  type="button"
                  onClick={() => setShowCondaAdvanced(value => !value)}
                  className="text-2xs font-medium text-txt-tertiary transition-colors hover:text-txt-primary"
                >
                  {showCondaAdvanced ? 'Hide conda command' : 'Conda command'}
                </button>
                {showCondaAdvanced && (
                  <input
                    value={condaExecutable}
                    onChange={event => setCondaExecutable(event.target.value)}
                    className="h-8 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 font-mono text-xs text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15"
                    placeholder="conda"
                  />
                )}
              </div>
            )}

            {runtimeMode === 'python' && (
              <div>
                <label className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">Python path</label>
                <input
                  value={pythonPath}
                  onChange={event => setPythonPath(event.target.value)}
                  className="h-9 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 font-mono text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15"
                  placeholder={runtime?.process.python_executable || 'python path'}
                />
              </div>
            )}
          </section>
        )}

        {activePage === 'env' && (
          <section className="space-y-3">
            <Suspense fallback={(
              <div role="status" className="runtime-env-editor flex items-center justify-center rounded-md border border-border-subtle bg-surface-overlay text-xs text-txt-tertiary">
                Loading environment editor…
              </div>
            )}>
              <CodeTextEditor
                language="shell"
                value={envText}
                onChange={setEnvText}
                theme={codeMirrorTheme}
                className="runtime-env-editor"
                wrapStorageKey="pyruns.runtime.env.wrap"
                compactToolbar
                placeholder="KEY=value"
              />
            </Suspense>
            <div className="flex items-center justify-end">
              <button
                type="button"
                onClick={() => saveRuntime({ global_env_text: envText }, 'Workspace env saved')}
                disabled={saving}
                className="inline-flex h-8 items-center justify-center gap-1.5 rounded-md bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent/90 disabled:opacity-50"
              >
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                Save
              </button>
            </div>
          </section>
        )}

        {activePage === 'gpu' && (
          <section className="space-y-4" aria-labelledby="gpu-scheduler-title">
            <div className="flex items-center justify-between gap-4">
              <div className="min-w-0">
                <h2 id="gpu-scheduler-title" className="text-sm font-semibold text-txt-primary">GPU scheduling</h2>
                <p className="mt-0.5 text-xs text-txt-secondary">Start tasks only when requested GPUs are ready.</p>
              </div>
              <button
                type="button"
                role="switch"
                aria-checked={gpuSchedulerEnabled}
                aria-label="GPU scheduling"
                onClick={() => setGpuSchedulerEnabled(value => !value)}
                className={clsx(
                  'relative h-6 w-11 flex-none border rounded-full transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30',
                  gpuSchedulerEnabled ? 'border-accent bg-accent' : 'border-border-strong bg-surface-overlay',
                )}
              >
                <span
                  className={clsx(
                    'absolute left-[3px] top-[3px] h-[18px] w-[18px] rounded-full bg-white shadow-sm transition-transform',
                    gpuSchedulerEnabled ? 'translate-x-5' : 'translate-x-0',
                  )}
                />
              </button>
            </div>

            {gpuSchedulerEnabled && gpuValidationMessage && (
              <div role="alert" className="flex gap-2.5 rounded-md border border-rose-500/25 bg-rose-500/10 px-3 py-2.5 text-rose-700 dark:text-rose-300">
                <AlertTriangle className="mt-0.5 h-4 w-4 flex-none" />
                <div className="min-w-0">
                  <p className="text-xs font-medium leading-5">{gpuValidationMessage}</p>
                  <p className="text-2xs opacity-80">Adjust the highlighted rule before saving.</p>
                </div>
              </div>
            )}

            {gpuWaitingNow && !gpuValidationMessage && (
              <div className="flex gap-2 rounded-md border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none" />
                <span>Too few GPUs pass the live thresholds. New tasks will wait visibly in Monitor.</span>
              </div>
            )}

            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md bg-surface-overlay/70 px-3 py-2 text-xs">
              <span className="inline-flex items-center gap-1.5 font-medium text-txt-primary">
                <CircleGauge className="h-3.5 w-3.5 text-txt-tertiary" />
                {!gpuSchedulerEnabled
                  ? 'Scheduling is off'
                  : gpuMetricsLoading && !gpuMetrics
                    ? 'Checking live GPU thresholds…'
                    : gpuMetricsError && !gpuMetrics
                      ? 'Threshold preview unavailable'
                      : `${passingGpuCount} of ${candidateGpus.length} pass live thresholds`}
              </span>
              <span className="hidden h-4 w-px bg-border sm:block" />
              <span className={clsx('text-2xs', gpuValidationIssues.length ? 'text-rose-700 dark:text-rose-300' : 'text-txt-tertiary')}>
                {gpuValidationIssues.length
                  ? `${gpuValidationIssues.length} ${gpuValidationIssues.length === 1 ? 'rule needs' : 'rules need'} attention`
                  : gpuReadinessUnavailable
                    ? 'Cannot verify current GPUs'
                    : gpuMetricsLoading && !gpuMetrics
                      ? 'Checking current GPUs'
                      : gpuWaitingNow
                        ? 'Tasks would wait'
                        : gpuSchedulerEnabled ? 'Meets current thresholds' : 'No GPU admission checks'}
              </span>
            </div>

            <div className="grid gap-3 sm:grid-cols-3">
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-txt-secondary">GPUs per task</span>
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={gpuCount}
                  disabled={!gpuSchedulerEnabled}
                  onChange={event => {
                    const value = event.target.value
                    setGpuCount(value)
                    setGpuTaskMode(Number(value) > 1 ? 'multi' : 'single')
                  }}
                  className="h-9 w-full rounded-md border border-border-subtle bg-surface-raised px-2.5 text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15 disabled:cursor-not-allowed disabled:opacity-50"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-txt-secondary">Minimum free memory</span>
                <div className={clsx(
                  'flex h-9 overflow-hidden rounded-md border bg-surface-raised focus-within:ring-2',
                  gpuValidationIssues.some(issue => issue.includes('free is not possible') || issue.includes('Minimum free memory'))
                    ? 'border-rose-500 focus-within:border-rose-500 focus-within:ring-rose-500/15'
                    : 'border-border-subtle focus-within:border-accent focus-within:ring-accent/15',
                )}>
                  <input
                    type="number"
                    min={0}
                    value={gpuMinFreeMemoryGb}
                    disabled={!gpuSchedulerEnabled}
                    onChange={event => setGpuMinFreeMemoryGb(event.target.value)}
                    aria-invalid={gpuValidationIssues.some(issue => issue.includes('free is not possible') || issue.includes('Minimum free memory'))}
                    className="min-w-0 flex-1 bg-transparent px-2.5 text-sm text-txt-primary outline-none disabled:cursor-not-allowed disabled:opacity-50"
                  />
                  <span className="flex w-11 items-center justify-center border-l border-border-subtle text-xs text-txt-tertiary">GiB</span>
                </div>
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-txt-secondary">Maximum wait</span>
                <div className="flex h-9 overflow-hidden rounded-md border border-border-subtle bg-surface-raised focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/15">
                  <input
                    type="number"
                    min={1}
                    value={gpuMaxWaitHours}
                    disabled={!gpuSchedulerEnabled}
                    onChange={event => setGpuMaxWaitHours(numberInputValue(event.target.value, 48, 1))}
                    className="min-w-0 flex-1 bg-transparent px-2.5 text-sm text-txt-primary outline-none disabled:cursor-not-allowed disabled:opacity-50"
                  />
                  <span className="flex w-11 items-center justify-center border-l border-border-subtle text-xs text-txt-tertiary">hr</span>
                </div>
              </label>
            </div>

            <div className="overflow-hidden rounded-md border border-border-subtle">
              {gpuMetricsError && !gpuMetrics ? (
                <div className="flex items-center justify-between gap-3 px-3 py-3 text-xs text-txt-secondary">
                  <span>GPU preview unavailable. Saved rules will still apply.</span>
                  <button type="button" onClick={() => void loadGpuMetrics(true)} className="font-medium text-accent hover:text-accent-hover">Retry</button>
                </div>
              ) : gpuMetricsLoading && !gpuMetrics ? (
                <div className="flex items-center gap-2 px-3 py-3 text-xs text-txt-tertiary">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> Checking GPUs…
                </div>
              ) : gpuPreviewRows.length === 0 ? (
                <div className="px-3 py-3 text-xs text-txt-tertiary">
                  {gpuSelectionMode === 'specified' ? 'No configured GPU indices were found.' : 'No NVIDIA GPUs detected.'}
                </div>
              ) : (
                <div className="divide-y divide-border-subtle">
                  {gpuPreviewRows.map(({ gpu, reasons }) => {
                    const key = gpuKey(gpu)
                    const expanded = expandedGpuKey === key
                    const freeGiB = gpuMemoryGiB(gpu.mem_total - gpu.mem_used)
                    return (
                      <div key={key}>
                        <button
                          type="button"
                          aria-expanded={expanded}
                          onClick={() => setExpandedGpuKey(expanded ? null : key)}
                          className="flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-surface-overlay focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/30"
                        >
                          <span className="min-w-0 flex-1 truncate text-xs font-medium text-txt-primary" title={gpu.name}>
                            GPU {gpu.index} · {gpu.name}
                          </span>
                          <span className="hidden flex-none text-2xs tabular-nums text-txt-tertiary sm:inline">
                            {formatGiB(freeGiB)} / {formatGiB(gpuMemoryGiB(gpu.mem_total))} free
                          </span>
                          <span className={clsx(
                            'flex-none text-xs font-medium',
                            !gpuSchedulerEnabled
                              ? 'text-txt-secondary'
                              : reasons.length
                                ? 'text-rose-700 dark:text-rose-300'
                                : 'text-emerald-700 dark:text-emerald-300',
                          )}>
                            {!gpuSchedulerEnabled ? 'Preview' : reasons.length ? 'Blocked' : 'Pass'}
                          </span>
                          <ChevronDown className={clsx('h-3.5 w-3.5 flex-none text-txt-tertiary transition-transform', expanded && 'rotate-180')} />
                        </button>
                        {expanded && (
                          <div className="border-t border-border-subtle bg-surface-overlay/45 px-3 py-2 text-2xs leading-5 text-txt-secondary">
                            <div className="mb-1 tabular-nums sm:hidden">{formatGiB(freeGiB)} / {formatGiB(gpuMemoryGiB(gpu.mem_total))} free</div>
                            {reasons.length
                              ? reasons.map(reason => <div key={reason}>• {reason}</div>)
                              : !gpuSchedulerEnabled
                                ? <div>Current metrics pass the saved thresholds. Scheduling is off.</div>
                                : <div>All current thresholds pass. The stability window still applies before launch.</div>}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>

            <details className="group rounded-md border border-border-subtle bg-surface-raised">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-medium text-txt-primary hover:bg-surface-overlay">
                <span>Advanced scheduling rules</span>
                <span className="flex items-center gap-2 text-2xs font-normal text-txt-tertiary">
                  <span className="hidden sm:inline">Selection, thresholds, stability</span>
                  <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
                </span>
              </summary>
              <div className="space-y-3 border-t border-border-subtle p-3">
                <div>
                  <div className="mb-1 text-xs font-medium text-txt-secondary">GPU selection</div>
                  <div className="inline-flex rounded-md bg-surface-overlay p-0.5">
                    {[
                      { id: 'auto' as GpuSelectionMode, label: 'Auto pick' },
                      { id: 'specified' as GpuSelectionMode, label: 'Specific indices' },
                    ].map(item => (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => setGpuSelectionMode(item.id)}
                        aria-pressed={gpuSelectionMode === item.id}
                        className={clsx(
                          'h-7 rounded-md px-2.5 text-xs font-medium transition-colors',
                          gpuSelectionMode === item.id
                            ? 'bg-surface-raised text-accent shadow-sm'
                            : 'text-txt-secondary hover:text-txt-primary',
                        )}
                      >
                        {item.label}
                      </button>
                    ))}
                  </div>
                </div>

                {gpuSelectionMode === 'specified' && (
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium text-txt-secondary">GPU indices</span>
                    <input
                      value={gpuDeviceIds}
                      onChange={event => setGpuDeviceIds(event.target.value)}
                      className="h-9 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 font-mono text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15"
                      placeholder="0,1"
                    />
                  </label>
                )}

                <div className="grid gap-3 sm:grid-cols-2">
                  <CompactNumberField label="Maximum memory use" value={gpuMemoryUsedPct} suffix="%" min={0} max={100} onChange={setGpuMemoryUsedPct} />
                  <CompactNumberField label="Maximum compute use" value={gpuComputeUsedPct} suffix="%" min={0} max={100} onChange={setGpuComputeUsedPct} />
                  <CompactNumberField label="Stable for" value={gpuStableSeconds} suffix="sec" min={1} onChange={setGpuStableSeconds} />
                  <CompactNumberField label="Tasks per GPU" value={gpuMaxTasksPerGpu} suffix="max" min={1} onChange={setGpuMaxTasksPerGpu} />
                </div>

                <p className="text-2xs leading-5 text-txt-tertiary">
                  The preview checks live thresholds. The stability window is enforced when a task enters the queue.
                </p>

                <label className="flex items-start gap-2 text-xs text-txt-secondary">
                  <input
                    type="checkbox"
                    checked={gpuRespectCudaVisibleDevices}
                    onChange={event => setGpuRespectCudaVisibleDevices(event.target.checked)}
                    className="mt-0.5 h-4 w-4 rounded border-border-subtle bg-surface-overlay text-accent focus:ring-accent/25"
                  />
                  <span>
                    Respect task CUDA_VISIBLE_DEVICES
                    <span className="mt-0.5 block text-2xs leading-4 text-txt-tertiary">
                      Indices, GPU UUIDs and MIG IDs are validated. Invalid identifiers never bypass scheduling safeguards.
                    </span>
                  </span>
                </label>

                <label className="flex items-center gap-2 text-xs text-txt-secondary">
                  <input
                    type="checkbox"
                    checked={gpuRequireSameModel}
                    onChange={event => setGpuRequireSameModel(event.target.checked)}
                    className="h-4 w-4 rounded border-border-subtle bg-surface-overlay text-accent focus:ring-accent/25"
                  />
                  <span>Require the same model for multi-GPU tasks</span>
                </label>
              </div>
            </details>

            <div className="flex items-end justify-between gap-3 border-t border-border-subtle pt-3">
              <button
                type="button"
                onClick={resetGpuScheduler}
                disabled={saving}
                className="inline-flex h-8 items-center gap-1.5 rounded-md px-2 text-xs font-medium text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary disabled:opacity-50"
              >
                <RotateCcw className="h-3.5 w-3.5" /> Reset
              </button>
              <div className="text-right">
                <button
                  type="button"
                  onClick={saveGpuScheduler}
                  disabled={saving || gpuValidationIssues.length > 0}
                  className="inline-flex h-8 min-w-20 items-center justify-center gap-1.5 rounded-md bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:bg-surface-hover disabled:text-txt-tertiary"
                >
                  {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                  Save
                </button>
                {gpuValidationIssues.length > 0 && (
                  <div className="mt-1 text-2xs text-txt-tertiary">Fix {gpuValidationIssues.length} {gpuValidationIssues.length === 1 ? 'rule' : 'rules'}</div>
                )}
              </div>
            </div>
          </section>
        )}
      </div>
    </div>
  )
}

function CompactNumberField({
  label,
  value,
  suffix,
  min,
  max,
  onChange,
}: {
  label: string
  value: string
  suffix: string
  min: number
  max?: number
  onChange: (value: string) => void
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-txt-secondary">{label}</span>
      <div className="flex h-9 overflow-hidden rounded-md border border-border-subtle bg-surface-overlay focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/15">
        <input
          type="number"
          min={min}
          max={max}
          value={value}
          onChange={event => onChange(event.target.value)}
          className="min-w-0 flex-1 bg-transparent px-2.5 text-sm text-txt-primary outline-none"
        />
        <span className="flex min-w-11 items-center justify-center border-l border-border-subtle px-2 text-xs text-txt-tertiary">{suffix}</span>
      </div>
    </label>
  )
}
