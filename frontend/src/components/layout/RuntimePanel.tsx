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
import { requestConfirmation, useRuntimeStore, useThemeStore, useToastStore, useWorkspaceStore } from '@/store'
import { errorMessage } from '@/utils/errors'
import { gpuFreeMemoryMb, gpuMemoryUsedPercent } from '@/utils/gpuMetrics'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import ToggleSwitch from '@/components/shared/ToggleSwitch'
import { t, useTranslation } from '@/i18n'
import { useOnboardingStore } from '@/components/onboarding/store'

const CodeTextEditor = lazy(() => import('@/components/shared/CodeTextEditor'))

interface RuntimePanelProps {
  open: boolean
  left: number
  onClose: () => void
}

type RuntimePage = 'python' | 'env' | 'gpu'
type RuntimeDirtyPages = Record<RuntimePage, boolean>
type RuntimePageRevisions = Record<RuntimePage, number>
const RUNTIME_PAGES: RuntimePage[] = ['python', 'env', 'gpu']
const GPU_PREVIEW_TIMEOUT_MS = 10_000
type PythonRuntimeMode = 'follow' | 'conda' | 'python'
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
    return t('Follow process')
  }
  if (runtime.python_executable) {
    return `Python: ${runtime.python_executable.split(/[\\/]/).pop() || 'custom'}`
  }
  if (runtime.conda_env) {
    return `Conda: ${runtime.conda_env}`
  }
  return runtime.process.conda_env ? t('Follow: {{environment}}', { environment: runtime.process.conda_env }) : t('Follow process')
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
  if (value == null || (typeof value === 'string' && !value.trim())) {
    return fallback
  }
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

function cleanRuntimeDirtyPages(): RuntimeDirtyPages {
  return { python: false, env: false, gpu: false }
}

function initialRuntimePageRevisions(): RuntimePageRevisions {
  return { python: 0, env: 0, gpu: 0 }
}

function gpuKey(gpu: GPUMetric) {
  return gpu.uuid || String(gpu.id)
}

function gpuMemoryGiB(memoryMb: number | null) {
  return memoryMb != null && Number.isFinite(memoryMb) ? Math.max(0, memoryMb) / 1024 : null
}

function formatGiB(value: number | null) {
  if (value == null || !Number.isFinite(value)) {
    return t('Unknown')
  }
  const digits = Number.isInteger(value) ? 0 : value >= 10 ? 1 : 2
  return `${value.toFixed(digits)} GiB`
}

function gpuRuleReasons(
  gpu: GPUMetric,
  minFreeMemoryGiB: number,
  minIdlePct: number,
) {
  const reasons: string[] = []
  const totalGiB = gpuMemoryGiB(gpu.mem_total)
  const freeGiB = gpuMemoryGiB(gpuFreeMemoryMb(gpu))
  const memoryUsedPct = gpuMemoryUsedPercent(gpu)

  if (memoryUsedPct == null) {
    reasons.push(t('Memory metrics unavailable; waiting for a valid reading.'))
  }
  if (totalGiB != null && minFreeMemoryGiB > totalGiB) {
    reasons.push(t('Needs {{required}} free; this GPU has {{total}} total', { required: formatGiB(minFreeMemoryGiB), total: formatGiB(totalGiB) }))
  } else if (freeGiB != null && freeGiB < minFreeMemoryGiB) {
    reasons.push(t('Free memory {{free}} is below {{required}}', { free: formatGiB(freeGiB), required: formatGiB(minFreeMemoryGiB) }))
  }
  if (gpu.util == null || !Number.isFinite(gpu.util) || gpu.util < 0 || gpu.util > 100) {
    reasons.push(t('Compute metrics unavailable; waiting for a valid reading.'))
  } else if (100 - gpu.util < minIdlePct) {
    reasons.push(t('GPU idle {{idle}}% is below {{required}}%', { idle: (100 - gpu.util).toFixed(0), required: minIdlePct }))
  }
  return reasons
}

export default function RuntimePanel({ open, left, onClose }: RuntimePanelProps) {
  const touring = useOnboardingStore(state => state.phase === 'tour')
  const { i18n } = useTranslation()
  const language = i18n.language
  const refreshWorkspace = useWorkspaceStore(s => s.fetch)
  const workspaceSettings = useWorkspaceStore(s => s.workspace?.settings)
  const workspaceRunRoot = useWorkspaceStore(s => s.workspace?.run_root)
  const cachedRuntime = useRuntimeStore(s => s.runtime)
  const runtimeLoading = useRuntimeStore(s => s.loading)
  const fetchRuntime = useRuntimeStore(s => s.fetchRuntime)
  const updateRuntime = useRuntimeStore(s => s.updateRuntime)
  const setRuntimeDirty = useRuntimeStore(s => s.setDirty)
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
  const runtimeSaveSeqRef = useRef(0)
  const gpuMetricsLoadSeqRef = useRef(0)
  const gpuMetricsControllerRef = useRef<AbortController | null>(null)
  const runtimeDirtyPagesRef = useRef<RuntimeDirtyPages>(cleanRuntimeDirtyPages())
  const runtimePageRevisionsRef = useRef<RuntimePageRevisions>(initialRuntimePageRevisions())
  const notify = useToastStore(state => state.notify)
  const [runtime, setRuntime] = useState<RuntimeInfo | null>(cachedRuntime)
  const [envText, setEnvText] = useState('')
  const [pythonPath, setPythonPath] = useState('')
  const [taskStorage, setTaskStorage] = useState<'shared' | 'local'>('shared')
  const [condaEnv, setCondaEnv] = useState('')
  const [condaExecutable, setCondaExecutable] = useState('conda')
  const [runtimeMode, setRuntimeMode] = useState<PythonRuntimeMode>('follow')
  const [activePage, setActivePage] = useState<RuntimePage>('python')
  const [gpuSchedulerEnabled, setGpuSchedulerEnabled] = useState(false)
  const [gpuSelectionMode, setGpuSelectionMode] = useState<GpuSelectionMode>('auto')
  const [gpuCount, setGpuCount] = useState('1')
  const [gpuDeviceIds, setGpuDeviceIds] = useState('')
  const [gpuMinFreeMemoryGb, setGpuMinFreeMemoryGb] = useState('40')
  const [gpuMinIdlePct, setGpuMinIdlePct] = useState('70')
  const [gpuStableSeconds, setGpuStableSeconds] = useState('15')
  const [gpuMaxWaitHours, setGpuMaxWaitHours] = useState('48')
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
  const [runtimeDirtyPages, setRuntimeDirtyPages] = useState<RuntimeDirtyPages>(cleanRuntimeDirtyPages)
  const [discardConfirmOpen, setDiscardConfirmOpen] = useState(false)

  const runtimeDirty = Object.values(runtimeDirtyPages).some(Boolean)
  const markRuntimeDirty = (page: RuntimePage) => {
    runtimeLoadSeqRef.current += 1
    setLoading(false)
    runtimePageRevisionsRef.current[page] += 1
    runtimeDirtyPagesRef.current = { ...runtimeDirtyPagesRef.current, [page]: true }
    setRuntimeDirtyPages(current => current[page] ? current : { ...current, [page]: true })
  }
  const clearRuntimeDirtyPage = (page: RuntimePage) => {
    runtimeDirtyPagesRef.current = { ...runtimeDirtyPagesRef.current, [page]: false }
    setRuntimeDirtyPages(current => current[page] ? { ...current, [page]: false } : current)
  }
  const clearAllRuntimeDirty = () => {
    const clean = cleanRuntimeDirtyPages()
    runtimeDirtyPagesRef.current = clean
    setRuntimeDirtyPages(clean)
  }
  const requestClose = () => {
    if (runtimeDirty) {
      setDiscardConfirmOpen(true)
      return
    }
    onClose()
  }

  const condaAvailable = !!runtime?.conda.available
  const envCount = Object.keys(runtime?.global_env || {}).length
  const currentLabel = useMemo(() => runtimeLabel(runtime, workspaceSettings), [runtime, workspaceSettings, language])
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
  const requestedGpuCount = numberInputValue(gpuCount, 1, 1)
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
    const minIdle = boundedNumberInputValue(gpuMinIdlePct, 70, 0, 100)
    return candidateGpus.map(gpu => ({
      gpu,
      reasons: gpuRuleReasons(gpu, minFree, minIdle),
    }))
  }, [candidateGpus, gpuMinIdlePct, gpuMinFreeMemoryGb, language])
  const passingGpuCount = gpuPreviewRows.filter(row => row.reasons.length === 0).length
  const passingSameModelCount = useMemo(() => {
    const models = new Map<string, number>()
    for (const { gpu, reasons } of gpuPreviewRows) {
      if (reasons.length) continue
      const model = gpu.name?.trim() || 'GPU'
      models.set(model, (models.get(model) || 0) + 1)
    }
    return Math.max(0, ...models.values())
  }, [gpuPreviewRows])
  const sameModelInsufficient = gpuRequireSameModel && requestedGpuCount > 1
    && passingGpuCount >= requestedGpuCount && passingSameModelCount < requestedGpuCount
  const gpuValidationIssues = useMemo(() => {
    if (!gpuSchedulerEnabled) {
      return []
    }

    const issues: string[] = []
    const rawCount = Number(gpuCount)
    const rawFreeMemory = Number(gpuMinFreeMemoryGb)
    const rawIdle = Number(gpuMinIdlePct)
    const rawStableSeconds = Number(gpuStableSeconds)
    const rawWaitHours = Number(gpuMaxWaitHours)
    const rawDeviceTokens = gpuDeviceIds.split(',').map(item => item.trim()).filter(Boolean)

    if (!Number.isInteger(rawCount) || rawCount < 1) {
      issues.push(t('GPUs required must be a whole number of at least 1.'))
    }
    if (!gpuMinFreeMemoryGb.trim() || !Number.isFinite(rawFreeMemory) || rawFreeMemory < 0) {
      issues.push(t('Minimum free memory must be 0 GiB or more.'))
    }
    if (!gpuMinIdlePct.trim() || !Number.isFinite(rawIdle) || rawIdle < 0 || rawIdle > 100) {
      issues.push(t('Minimum GPU idle must be between 0% and 100%.'))
    }
    if (!gpuStableSeconds.trim() || !Number.isFinite(rawStableSeconds) || rawStableSeconds < 0) {
      issues.push(t('Stability window must be 0 seconds or more.'))
    }
    if (!gpuMaxWaitHours.trim() || !Number.isFinite(rawWaitHours) || rawWaitHours < 0) {
      issues.push(t('Maximum wait must be 0 hours or more (0 means unlimited).'))
    }
    if (gpuSelectionMode === 'specified') {
      if (rawDeviceTokens.some(item => !/^\d+$/.test(item))) {
        issues.push(t('GPU selection accepts comma-separated numeric indices, such as 0,1.'))
      } else if (selectedGpuIds.length !== requestedGpuCount) {
        issues.push(t('Choose exactly {{count}} unique GPU indices.', { count: requestedGpuCount }))
      } else if (unavailableSelectedGpuIds.length > 0) {
        const label = unavailableSelectedGpuIds.join(', ')
        issues.push(t('GPU indices {{indices}} are not detected on this machine.', { indices: label }))
      }
    }

    if (candidateGpus.length > 0 && requestedGpuCount > candidateGpus.length) {
      issues.push(t('This machine exposes only {{count}} matching GPUs.', { count: candidateGpus.length }))
    }
    if (
      Number.isFinite(rawFreeMemory)
      && rawFreeMemory >= 0
      && candidateGpus.length > 0
      && candidateGpus.every(gpu => {
        const totalGiB = gpuMemoryGiB(gpu.mem_total)
        return totalGiB != null && rawFreeMemory > totalGiB
      })
    ) {
      const largestGpuGiB = Math.max(...candidateGpus.map(gpu => gpuMemoryGiB(gpu.mem_total) ?? 0))
      issues.push(
        t('{{required}} free is not possible on the configured GPUs ({{maximum}} maximum physical memory).', {
          required: formatGiB(rawFreeMemory), maximum: formatGiB(largestGpuGiB),
        }),
      )
    }
    return Array.from(new Set(issues))
  }, [
    candidateGpus,
    gpuCount,
    gpuDeviceIds,
    gpuMinIdlePct,
    gpuMaxWaitHours,
    gpuMinFreeMemoryGb,
    gpuSchedulerEnabled,
    gpuSelectionMode,
    gpuStableSeconds,
    requestedGpuCount,
    selectedGpuIds,
    unavailableSelectedGpuIds,
    language,
  ])
  const gpuValidationMessage = gpuValidationIssues[0] || ''
  const freeMemoryInvalid = gpuSchedulerEnabled && (
    !gpuMinFreeMemoryGb.trim() || !Number.isFinite(Number(gpuMinFreeMemoryGb)) || Number(gpuMinFreeMemoryGb) < 0
    || (candidateGpus.length > 0 && candidateGpus.every(gpu => {
      const totalGiB = gpuMemoryGiB(gpu.mem_total)
      return totalGiB != null && Number(gpuMinFreeMemoryGb) > totalGiB
    }))
  )
  const maxWaitInvalid = gpuSchedulerEnabled && (
    !gpuMaxWaitHours.trim() || !Number.isFinite(Number(gpuMaxWaitHours)) || Number(gpuMaxWaitHours) < 0
  )
  const gpuReadinessUnavailable = gpuSchedulerEnabled && !!gpuMetricsError && !gpuMetrics
  const gpuWaitingNow = gpuSchedulerEnabled
    && !!gpuMetrics
    && (passingGpuCount < requestedGpuCount || sameModelInsufficient)

  const applyPythonRuntimeState = (next: RuntimeInfo) => {
    setTaskStorage(next.task_storage || 'shared')
    setPythonPath(next.python_executable)
    setCondaEnv(next.conda_env)
    setCondaExecutable(next.conda_executable || 'conda')
    setRuntimeMode(modeFromRuntime(next))
  }

  const applyEnvRuntimeState = (next: RuntimeInfo) => {
    setEnvText(formatEnv(next.global_env))
  }

  const applyGpuRuntimeState = (next: RuntimeInfo) => {
    setGpuSchedulerEnabled(next.gpu_scheduler?.enabled ?? false)
    setGpuSelectionMode(next.gpu_scheduler?.selection_mode === 'specified' ? 'specified' : 'auto')
    setGpuCount(String(next.gpu_scheduler?.gpus_per_task ?? 1))
    setGpuDeviceIds(formatDeviceIds(next.gpu_scheduler?.device_ids))
    setGpuMinFreeMemoryGb(String(next.gpu_scheduler?.min_free_memory_gb ?? 8))
    setGpuMinIdlePct(String(next.gpu_scheduler?.min_idle_pct ?? 70))
    setGpuStableSeconds(String(next.gpu_scheduler?.stable_seconds ?? 15))
    setGpuMaxWaitHours(String(Math.max(0, (next.gpu_scheduler?.max_wait_seconds ?? 172800) / 3600)))
    setGpuRespectCudaVisibleDevices(next.gpu_scheduler?.respect_cuda_visible_devices ?? true)
    setGpuRequireSameModel(next.gpu_scheduler?.require_same_gpu_model ?? false)
  }

  const applyRuntimePageState = (next: RuntimeInfo, page: RuntimePage) => {
    if (page === 'python') {
      applyPythonRuntimeState(next)
    } else if (page === 'env') {
      applyEnvRuntimeState(next)
    } else {
      applyGpuRuntimeState(next)
    }
  }

  const applyRuntimeState = (next: RuntimeInfo) => {
    setRuntime(next)
    applyPythonRuntimeState(next)
    applyEnvRuntimeState(next)
    applyGpuRuntimeState(next)
    clearAllRuntimeDirty()
  }

  const applyGpuWorkspaceSettings = (settings?: Record<string, any>) => {
    setGpuSchedulerEnabled(settingBool(settings?.gpu_scheduler_enabled, false))
    setGpuSelectionMode(['specified', 'fixed', 'manual'].includes(cleanSettingText(settings?.gpu_scheduler_selection_mode).toLowerCase()) ? 'specified' : 'auto')
    setGpuCount(String(cleanSettingText(settings?.gpu_scheduler_task_mode).toLowerCase() === 'multi'
      ? settingNumber(settings?.gpu_scheduler_gpus_per_task, 1, 1) : 1))
    setGpuDeviceIds(formatDeviceIdsSetting(settings?.gpu_scheduler_device_ids))
    setGpuMinFreeMemoryGb(String(settingNumber(settings?.gpu_scheduler_min_free_memory_gb, 8, 0)))
    setGpuMinIdlePct(String(Object.prototype.hasOwnProperty.call(settings || {}, 'gpu_scheduler_min_idle_pct')
      ? settingNumber(settings?.gpu_scheduler_min_idle_pct, 70, 0, 100)
      : 100 - settingNumber(settings?.gpu_scheduler_compute_used_pct, 30, 0, 100)))
    setGpuStableSeconds(String(settingNumber(settings?.gpu_scheduler_stable_seconds, 15, 0)))
    setGpuMaxWaitHours(String(settingNumber(settings?.gpu_scheduler_max_wait_seconds, 172800, 0) / 3600))
    setGpuRespectCudaVisibleDevices(settingBool(settings?.gpu_scheduler_respect_cuda_visible_devices, true))
    setGpuRequireSameModel(settingBool(settings?.gpu_scheduler_require_same_gpu_model, false))
  }

  const applyWorkspaceRuntimeSettings = (settings?: Record<string, any>) => {
    setRuntime(null)
    setEnvText(formatEnv(settings?.global_env || {}))
    setPythonPath(cleanSettingText(settings?.python_executable))
    setTaskStorage(settings?.task_storage === 'local' ? 'local' : 'shared')
    setCondaEnv(cleanSettingText(settings?.conda_env))
    setCondaExecutable(cleanSettingText(settings?.conda_executable) || 'conda')
    setRuntimeMode(modeFromSettings(settings))
    applyGpuWorkspaceSettings(settings)
    clearAllRuntimeDirty()
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
      const message = errorMessage(err, t('Could not refresh runtime.'))
      setError(message)
      if (showFeedback) {
        notify({ tone: 'error', title: t('Could not refresh runtime'), detail: message })
      }
    } finally {
      if (loadSeq === runtimeLoadSeqRef.current) {
        setLoading(false)
      }
    }
  }

  const loadGpuMetrics = async (showFeedback = false) => {
    gpuMetricsControllerRef.current?.abort()
    const controller = new AbortController()
    gpuMetricsControllerRef.current = controller
    const loadSeq = gpuMetricsLoadSeqRef.current + 1
    gpuMetricsLoadSeqRef.current = loadSeq
    let timedOut = false
    const timeoutId = window.setTimeout(() => {
      timedOut = true
      controller.abort()
    }, GPU_PREVIEW_TIMEOUT_MS)
    setGpuMetricsLoading(true)
    setGpuMetricsError('')
    try {
      const next = await api.getMetrics({}, controller.signal)
      if (loadSeq === gpuMetricsLoadSeqRef.current) {
        setGpuMetrics(next)
        setGpuMetricsError('')
      }
    } catch (err) {
      if (loadSeq !== gpuMetricsLoadSeqRef.current || (controller.signal.aborted && !timedOut)) return
      const message = timedOut
        ? t('GPU preview timed out. Check the connection and retry.')
        : errorMessage(err, t('GPU readiness preview is unavailable.'))
      setGpuMetricsError(message)
      if (showFeedback) {
        notify({ tone: 'error', title: t('Could not refresh GPU preview'), detail: message })
      }
    } finally {
      window.clearTimeout(timeoutId)
      if (gpuMetricsControllerRef.current === controller) gpuMetricsControllerRef.current = null
      if (loadSeq === gpuMetricsLoadSeqRef.current) {
        setGpuMetricsLoading(false)
      }
    }
  }

  useEffect(() => {
    if (open) {
      runtimeSaveSeqRef.current += 1
      setSaving(false)
      if (cachedRuntime) {
        applyRuntimeState(cachedRuntime)
      } else {
        applyWorkspaceRuntimeSettings(workspaceSettings)
      }
      void loadRuntime()
    }
  }, [open, workspaceRunRoot])

  useEffect(() => {
    if (!open || activePage !== 'gpu') {
      return
    }
    void loadGpuMetrics()
    return () => {
      gpuMetricsLoadSeqRef.current += 1
      gpuMetricsControllerRef.current?.abort()
      gpuMetricsControllerRef.current = null
    }
  }, [activePage, open])

  useEffect(() => {
    setRuntimeDirty(open && runtimeDirty)
    return () => setRuntimeDirty(false)
  }, [open, runtimeDirty, setRuntimeDirty])

  useEffect(() => {
    if (!open || !runtimeDirty) {
      return
    }
    const guardUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', guardUnload)
    return () => window.removeEventListener('beforeunload', guardUnload)
  }, [open, runtimeDirty])

  useEffect(() => {
    if (!open) {
      return
    }

    const handleDocumentPointerDown = (event: PointerEvent) => {
      if (useOnboardingStore.getState().phase === 'tour') return
      if (document.querySelector('dialog[open]')) {
        closeGestureRef.current = null
        return
      }
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
      if (document.querySelector('dialog[open]')) {
        closeGestureRef.current = null
        return
      }
      const gesture = closeGestureRef.current
      if (!gesture || gesture.pointerId !== event.pointerId) {
        return
      }
      closeGestureRef.current = null
      if (gesture.startedInside || gesture.dragged || isInsidePanel(panelRef.current, event.target)) {
        return
      }
      requestClose()
    }
    const handleDocumentPointerCancel = (event: PointerEvent) => {
      if (closeGestureRef.current?.pointerId !== event.pointerId) {
        return
      }
      closeGestureRef.current = null
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (useOnboardingStore.getState().phase === 'tour') return
      if (document.querySelector('dialog[open]')) {
        return
      }
      if (event.key === 'Escape') {
        requestClose()
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
      if (useOnboardingStore.getState().phase === 'tour') return
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
  }, [open, onClose, runtimeDirty])

  const refreshWorkspaceInBackground = () => {
    void refreshWorkspace().catch(err => {
      notify({
        tone: 'error',
        title: t('Runtime saved, workspace refresh failed'),
        detail: errorMessage(err, t('Refresh the workspace to see the latest runtime summary.')),
      })
    })
  }

  const saveRuntime = async (
    page: RuntimePage,
    payload: Parameters<typeof api.updateRuntimeInfo>[0],
    successTitle = t('Runtime saved'),
  ) => {
    runtimeLoadSeqRef.current += 1
    const saveSeq = ++runtimeSaveSeqRef.current
    const pageRevision = runtimePageRevisionsRef.current[page]
    const workspaceKey = String(workspaceRunRoot || '')
    const requestIsCurrent = () => (
      saveSeq === runtimeSaveSeqRef.current
      && workspaceKey === String(useWorkspaceStore.getState().workspace?.run_root || '')
    )
    setSaving(true)
    setError('')
    try {
      const next = await updateRuntime(payload, false)
      if (!requestIsCurrent()) {
        return
      }
      setRuntime(next)
      if (pageRevision === runtimePageRevisionsRef.current[page]) {
        applyRuntimePageState(next, page)
        clearRuntimeDirtyPage(page)
      }
      notify({ tone: 'success', title: successTitle })
      refreshWorkspaceInBackground()
    } catch (err) {
      if (!requestIsCurrent()) {
        return
      }
      const message = errorMessage(err, t('Could not save runtime settings.'))
      setError(message)
      notify({ tone: 'error', title: t('Could not save runtime'), detail: message })
    } finally {
      if (requestIsCurrent()) {
        setSaving(false)
      }
    }
  }

  const savePythonRuntime = () => {
    const storageUpdate = taskStorage !== (runtime?.task_storage ?? workspaceSettings?.task_storage ?? 'shared')
      ? { task_storage: taskStorage } : {}
    if (runtimeMode === 'conda') {
      if (!condaEnv) {
        const message = t('Choose a conda environment before saving.')
        setError(message)
        notify({ tone: 'error', title: t('Conda environment required'), detail: message })
        return
      }
      void saveRuntime('python', {
        ...storageUpdate,
        conda_env: condaEnv,
        conda_executable: condaExecutable,
        python_executable: '',
      }, t('Python runtime saved'))
      return
    }
    if (runtimeMode === 'python') {
      void saveRuntime('python', {
        ...storageUpdate,
        python_executable: pythonPath,
        conda_env: '',
      }, t('Python runtime saved'))
      return
    }
    void saveRuntime('python', {
      ...storageUpdate,
      conda_env: '',
      python_executable: '',
    }, t('Python runtime saved'))
  }

  const chooseRuntimeMode = (mode: PythonRuntimeMode) => {
    if (mode === runtimeMode) {
      return
    }
    setRuntimeMode(mode)
    markRuntimeDirty('python')
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
      notify({ tone: 'error', title: t('Fix GPU scheduling settings'), detail: message })
      return
    }

    void saveRuntime('gpu', {
      gpu_scheduler: {
        enabled: gpuSchedulerEnabled,
        task_mode: requestedGpuCount > 1 ? 'multi' : 'single',
        selection_mode: gpuSelectionMode,
        gpus_per_task: requestedGpuCount,
        device_ids: gpuSelectionMode === 'specified' ? selectedGpuIds : [],
        min_free_memory_gb: numberInputValue(gpuMinFreeMemoryGb, 8, 0),
        min_idle_pct: boundedNumberInputValue(gpuMinIdlePct, 70, 0, 100),
        stable_seconds: numberInputValue(gpuStableSeconds, 15, 0),
        max_wait_seconds: Number(gpuMaxWaitHours) * 3600,
        respect_cuda_visible_devices: gpuRespectCudaVisibleDevices,
        require_same_gpu_model: gpuRequireSameModel,
      },
    }, t('GPU scheduler saved'))
  }

  const resetGpuScheduler = () => {
    setError('')
    setExpandedGpuKey(null)
    if (runtime) {
      applyGpuRuntimeState(runtime)
    } else {
      applyGpuWorkspaceSettings(workspaceSettings)
    }
    clearRuntimeDirtyPage('gpu')
  }

  const refreshPanel = async () => {
    if (runtimeDirty && !(await requestConfirmation({
      title: t('Discard runtime changes?'),
      description: t('Discard unsaved runtime changes and reload settings?'),
      confirmLabel: t('Discard and Reload'),
      confirmVariant: 'danger',
    }))) {
      return
    }
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
      title: t('Follow'),
    },
    {
      id: 'conda',
      title: 'Conda',
    },
    {
      id: 'python',
      title: t('Path'),
    },
  ]

  return (
    <div
      ref={panelRef}
      data-tour-panel="runtime"
      role="dialog"
      aria-modal={touring ? undefined : 'true'}
      aria-label={t('Runtime settings')}
      className="runtime-panel fixed bottom-3 z-50 flex max-h-[calc(100dvh-24px)] w-[620px] flex-col overflow-hidden rounded-lg border border-border bg-surface-raised shadow-xl"
      style={{ left, maxWidth: `calc(100vw - ${left + 12}px)` }}
      onClick={event => event.stopPropagation()}
    >
      <div data-tour="runtime-controls" className="flex h-12 items-center gap-1.5 border-b border-border-subtle px-2 sm:h-10 sm:gap-2 sm:px-3">
        <div className="hidden min-w-0 flex-1 items-center gap-2 sm:flex">
          <div className="text-sm font-semibold text-txt-primary">{t('Runtime')}</div>
          <div className="truncate rounded-md bg-surface-overlay px-2 py-0.5 text-2xs text-txt-secondary">
            {currentLabel}
          </div>
        </div>
        <div role="tablist" aria-label={t('Runtime sections')} className="inline-flex rounded-md bg-surface-overlay p-0.5">
          {RUNTIME_PAGES.map((page, pageIndex) => (
            <button
              key={page}
              type="button"
              onClick={() => setActivePage(page)}
              aria-pressed={activePage === page}
              role="tab"
              aria-selected={activePage === page}
              aria-controls={`runtime-panel-${page}`}
              id={`runtime-tab-${page}`}
              tabIndex={activePage === page ? 0 : -1}
              data-runtime-initial-focus={activePage === page ? 'true' : undefined}
              onKeyDown={event => {
                let nextIndex: number | null = null
                if (event.key === 'ArrowRight') nextIndex = (pageIndex + 1) % RUNTIME_PAGES.length
                if (event.key === 'ArrowLeft') nextIndex = (pageIndex - 1 + RUNTIME_PAGES.length) % RUNTIME_PAGES.length
                if (event.key === 'Home') nextIndex = 0
                if (event.key === 'End') nextIndex = RUNTIME_PAGES.length - 1
                if (nextIndex == null) return
                event.preventDefault()
                const nextPage = RUNTIME_PAGES[nextIndex]
                setActivePage(nextPage)
                window.requestAnimationFrame(() => document.getElementById(`runtime-tab-${nextPage}`)?.focus())
              }}
              className={clsx(
                'touch-target inline-flex h-11 min-w-11 items-center justify-center gap-1.5 rounded-md px-2 text-xs font-medium transition-colors sm:h-7 sm:min-w-0 sm:px-2.5',
                activePage === page
                  ? 'bg-surface-raised text-accent shadow-sm'
                  : 'text-txt-secondary hover:text-txt-primary'
              )}
            >
              {page === 'python' ? 'Python' : page === 'env' ? t('Env') : 'GPU'}
              {runtimeDirtyPages[page] ? (
                <>
                  <span className="h-1.5 w-1.5 rounded-full bg-amber-500" aria-hidden="true" />
                  <span className="sr-only"> {t('(unsaved changes)')}</span>
                </>
              ) : page === 'env' && envCount > 0 ? (
                <span className="h-1.5 w-1.5 rounded-full bg-status-completed" aria-hidden="true" />
              ) : null}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => { void refreshPanel() }}
          disabled={loading || runtimeLoading || gpuMetricsLoading || saving}
          className="touch-target inline-flex h-11 w-11 items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary disabled:opacity-50 sm:h-8 sm:w-8"
          aria-label={t('Reload runtime')}
        >
          {loading || runtimeLoading || gpuMetricsLoading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </button>
        <button
          type="button"
          onClick={requestClose}
          data-tour-close="runtime"
          className="touch-target inline-flex h-11 w-11 items-center justify-center rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary sm:h-8 sm:w-8"
          aria-label={t('Close runtime panel')}
        >
          <X className="h-4 w-4" />
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {error && (
          <div role="alert" className="mb-3 rounded-md bg-status-failed/10 px-3 py-2 text-sm text-rose-700 dark:text-rose-300">
            {error}
          </div>
        )}

        {activePage === 'python' && (
          <section id="runtime-panel-python" role="tabpanel" aria-labelledby="runtime-tab-python" className="space-y-3">
            <div className="flex items-center justify-between gap-3">
              <div className="inline-flex rounded-md bg-surface-overlay p-0.5">
                {modeItems.map(item => (
                  <button
                    key={item.id}
                    type="button"
                    onClick={() => chooseRuntimeMode(item.id)}
                    className={clsx(
                      'touch-target h-11 rounded-md px-3 text-xs font-medium transition-colors sm:h-auto sm:py-1.5',
                      runtimeMode === item.id
                        ? 'bg-surface-raised text-accent shadow-sm'
                        : 'text-txt-secondary hover:text-txt-primary'
                    )}
                    aria-pressed={runtimeMode === item.id}
                  >
                    {item.title}
                  </button>
                ))}
              </div>
              <button
                type="button"
                onClick={savePythonRuntime}
                disabled={saving || !runtimeDirtyPages.python}
                className="touch-target inline-flex h-11 items-center justify-center gap-1.5 rounded-md bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-50 sm:h-8"
              >
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                  {t('Save')}
                </button>
            </div>

            <label className="block text-xs font-medium text-txt-secondary">
              {t('Save new tasks to')}
              <select className="mt-1 block w-full rounded-md border border-border-subtle bg-surface-overlay p-2 text-sm text-txt-primary"
                value={taskStorage} onChange={event => {
                  setTaskStorage(event.target.value === 'local' ? 'local' : 'shared')
                  markRuntimeDirty('python')
                }}>
                <option value="shared">{t('Shared tasks folder')}</option>
                <option value="local">{t('This device’s tasks folder')}</option>
              </select>
              <span className="mt-1 block font-normal text-txt-tertiary">{t('Existing tasks stay in their original folders.')}</span>
            </label>

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
                    aria-label={t('Conda environment')}
                    value={condaEnv}
                    disabled={saving}
                    onChange={event => { setCondaEnv(event.target.value); markRuntimeDirty('python') }}
                    className="touch-input h-11 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15 disabled:opacity-50 sm:h-9"
                  >
                    <option value="">{t('Choose environment')}</option>
                    {runtime?.process.conda_env && !runtime.conda.envs.some(env => env.name === runtime.process.conda_env) && (
                      <option value={runtime.process.conda_env}>
                        {runtime.process.conda_env} {t('(current)')}
                      </option>
                    )}
                    {runtime?.conda.envs.map(env => (
                      <option key={env.name} value={env.name}>
                        {env.name}{env.active ? t(' (active)') : ''}
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
                    {selectedConda?.python_executable || t('Choose a conda environment to preview Python path')}
                  </div>
                  {selectedConda?.path && (
                    <div className="truncate text-2xs text-txt-tertiary" title={selectedConda.path}>
                      {selectedConda.path}
                    </div>
                  )}
                </div>
                {runtime?.conda.error && (
                  <div className="rounded-md bg-status-failed/10 px-3 py-2 text-sm text-rose-700 dark:text-rose-300">
                    {runtime.conda.error}
                  </div>
                )}
                <button
                  type="button"
                  onClick={() => setShowCondaAdvanced(value => !value)}
                  className="touch-target text-2xs font-medium text-txt-tertiary transition-colors hover:text-txt-primary"
                >
                  {showCondaAdvanced ? t('Hide conda command') : t('Conda command')}
                </button>
                {showCondaAdvanced && (
                  <input
                    aria-label={t('Conda executable')}
                    value={condaExecutable}
                    onChange={event => { setCondaExecutable(event.target.value); markRuntimeDirty('python') }}
                    className="touch-input h-11 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 font-mono text-xs text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15 sm:h-8"
                    placeholder="conda"
                  />
                )}
              </div>
            )}

            {runtimeMode === 'python' && (
              <div>
                <label className="mb-1 block text-2xs uppercase tracking-[0.14em] text-txt-tertiary">{t('Python path')}</label>
                <input
                  aria-label={t('Python executable path')}
                  value={pythonPath}
                  onChange={event => { setPythonPath(event.target.value); markRuntimeDirty('python') }}
                  className="touch-input h-11 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 font-mono text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15 sm:h-9"
                  placeholder={runtime?.process.python_executable || t('python path')}
                />
              </div>
            )}
          </section>
        )}

        {activePage === 'env' && (
          <section id="runtime-panel-env" role="tabpanel" aria-labelledby="runtime-tab-env" className="space-y-3">
            <Suspense fallback={(
              <div role="status" className="runtime-env-editor flex items-center justify-center rounded-md border border-border-subtle bg-surface-overlay text-xs text-txt-tertiary">
                {t('Loading environment editor…')}
              </div>
            )}>
              <CodeTextEditor
                language="shell"
                value={envText}
                onChange={value => { setEnvText(value); markRuntimeDirty('env') }}
                ariaLabel={t('Workspace environment variables editor')}
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
                onClick={() => saveRuntime('env', { global_env_text: envText }, t('Workspace env saved'))}
                disabled={saving || !runtimeDirtyPages.env}
                className="touch-target inline-flex h-11 items-center justify-center gap-1.5 rounded-md bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-50 sm:h-8"
              >
                {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                  {t('Save')}
                </button>
            </div>
          </section>
        )}

        {activePage === 'gpu' && (
          <section id="runtime-panel-gpu" role="tabpanel" aria-labelledby="runtime-tab-gpu" className="space-y-4">
            <div className="flex items-center justify-between gap-4">
              <div className="min-w-0">
                <h2 id="gpu-scheduler-title" className="text-sm font-semibold text-txt-primary">{t('GPU scheduling')}</h2>
                <p className="mt-0.5 text-xs text-txt-secondary">{t('Start tasks only when requested GPUs are ready.')}</p>
              </div>
              <ToggleSwitch
                checked={gpuSchedulerEnabled}
                label={t('GPU scheduling')}
                onChange={checked => {
                  setGpuSchedulerEnabled(checked)
                  markRuntimeDirty('gpu')
                }}
              />
            </div>

            {gpuSchedulerEnabled && gpuValidationMessage && (
              <div role="alert" className="flex gap-2.5 rounded-md border border-rose-500/25 bg-rose-500/10 px-3 py-2.5 text-rose-700 dark:text-rose-300">
                <AlertTriangle className="mt-0.5 h-4 w-4 flex-none" />
                <div className="min-w-0">
                  <p className="text-xs font-medium leading-5">{gpuValidationMessage}</p>
                  <p className="text-2xs opacity-80">{t('Adjust the highlighted rule before saving.')}</p>
                </div>
              </div>
            )}

            {gpuWaitingNow && !gpuValidationMessage && (
              <div className="flex gap-2 rounded-md border border-amber-500/25 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
                <AlertTriangle className="mt-0.5 h-3.5 w-3.5 flex-none" />
                <span>{sameModelInsufficient
                  ? t('Need {{count}} ready GPUs of the same model; the largest group has {{available}}.', { count: requestedGpuCount, available: passingSameModelCount })
                  : t('Too few GPUs pass the memory and idle thresholds. New tasks will wait visibly in Monitor.')}</span>
              </div>
            )}

            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 rounded-md bg-surface-overlay/70 px-3 py-2 text-xs">
              <span className="inline-flex items-center gap-1.5 font-medium text-txt-primary">
                <CircleGauge className="h-3.5 w-3.5 text-txt-tertiary" />
                {!gpuSchedulerEnabled
                  ? t('Scheduling is off')
                  : gpuMetricsLoading && !gpuMetrics
                    ? t('Checking live GPU thresholds…')
                    : gpuMetricsError && !gpuMetrics
                      ? t('Threshold preview unavailable')
                      : t('{{passing}} of {{total}} pass memory and idle thresholds', { passing: passingGpuCount, total: candidateGpus.length })}
              </span>
              <span className="hidden h-4 w-px bg-border sm:block" />
              <span className={clsx('text-2xs', gpuValidationIssues.length ? 'text-rose-700 dark:text-rose-300' : 'text-txt-tertiary')}>
                {gpuValidationIssues.length
                  ? t('{{count}} rules need attention', { count: gpuValidationIssues.length })
                  : gpuReadinessUnavailable
                    ? t('Cannot verify current GPUs')
                    : gpuMetricsLoading && !gpuMetrics
                      ? t('Checking current GPUs')
                      : gpuWaitingNow
                        ? t('Tasks would wait')
                        : gpuSchedulerEnabled ? t('Candidate GPUs ready') : t('No GPU admission checks')}
              </span>
            </div>

            <div className="grid gap-3 sm:grid-cols-3">
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-txt-secondary">{t('GPUs required')}</span>
                <input
                  type="number"
                  min={1}
                  step={1}
                  value={gpuCount}
                  disabled={!gpuSchedulerEnabled}
                  onChange={event => {
                    const value = event.target.value
                    setGpuCount(value)
                    markRuntimeDirty('gpu')
                  }}
                  className="touch-input h-11 w-full rounded-md border border-border-subtle bg-surface-raised px-2.5 text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15 disabled:cursor-not-allowed disabled:opacity-50 sm:h-9"
                />
              </label>
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-txt-secondary">{t('Minimum free memory')}</span>
                <div className={clsx(
                  'touch-input flex h-11 overflow-hidden rounded-md border bg-surface-raised focus-within:ring-2 sm:h-9',
                  freeMemoryInvalid
                    ? 'border-rose-500 focus-within:border-rose-500 focus-within:ring-rose-500/15'
                    : 'border-border-subtle focus-within:border-accent focus-within:ring-accent/15',
                )}>
                  <input
                    type="number"
                    min={0}
                    step="any"
                    value={gpuMinFreeMemoryGb}
                    disabled={!gpuSchedulerEnabled}
                    onChange={event => { setGpuMinFreeMemoryGb(event.target.value); markRuntimeDirty('gpu') }}
                    aria-invalid={freeMemoryInvalid}
                    className="min-w-0 flex-1 bg-transparent px-2.5 text-sm text-txt-primary outline-none disabled:cursor-not-allowed disabled:opacity-50"
                  />
                  <span className="flex w-11 items-center justify-center border-l border-border-subtle text-xs text-txt-tertiary">GiB</span>
                </div>
              </label>
              <CompactNumberField label={t('Minimum GPU idle')} value={gpuMinIdlePct} suffix="%" min={0} max={100} disabled={!gpuSchedulerEnabled} onChange={value => { setGpuMinIdlePct(value); markRuntimeDirty('gpu') }} />
              <CompactNumberField label={t('Stable for')} value={gpuStableSeconds} suffix={t('sec')} min={0} disabled={!gpuSchedulerEnabled} onChange={value => { setGpuStableSeconds(value); markRuntimeDirty('gpu') }} />
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-txt-secondary">{t('Maximum wait')}</span>
                <div className={clsx(
                  'touch-input flex h-11 overflow-hidden rounded-md border bg-surface-raised focus-within:ring-2 sm:h-9',
                  maxWaitInvalid
                    ? 'border-rose-500 focus-within:border-rose-500 focus-within:ring-rose-500/15'
                    : 'border-border-subtle focus-within:border-accent focus-within:ring-accent/15',
                )}>
                  <input
                    type="number"
                    min={0}
                    step="any"
                    value={gpuMaxWaitHours}
                    disabled={!gpuSchedulerEnabled}
                    onChange={event => { setGpuMaxWaitHours(event.target.value); markRuntimeDirty('gpu') }}
                    aria-invalid={maxWaitInvalid}
                    className="min-w-0 flex-1 bg-transparent px-2.5 text-sm text-txt-primary outline-none disabled:cursor-not-allowed disabled:opacity-50"
                  />
                  <span className="flex w-11 items-center justify-center border-l border-border-subtle text-xs text-txt-tertiary">{t('hr')}</span>
                </div>
              </label>
            </div>
            <p className="text-2xs leading-5 text-txt-tertiary">
              {t('GPU idle is 100% minus utilization. Stable for 0 starts immediately; maximum wait 0 waits indefinitely.')}
            </p>
            <p className="text-2xs leading-5 text-txt-tertiary">
              {t('Preview checks memory, idle and model. At launch, task CUDA_VISIBLE_DEVICES, reservations and stability also apply.')}
            </p>

            {gpuMetricsError && (
              <div role="alert" className="flex items-start justify-between gap-3 rounded-md border border-amber-500/20 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
                <div className="min-w-0 break-words">
                  <p>{gpuMetricsError}</p>
                  <p className="mt-1">{gpuMetrics ? t('Showing the last available GPU values.') : t('GPU preview unavailable.')} {t('Saved rules will still apply.')}</p>
                </div>
                <button type="button" onClick={() => void loadGpuMetrics(true)} disabled={gpuMetricsLoading} className="touch-target flex-none rounded-md px-2 py-1 font-medium text-accent hover:bg-surface-overlay disabled:opacity-50">{t('Retry')}</button>
              </div>
            )}
            <div className="overflow-hidden rounded-md border border-border-subtle" aria-busy={gpuMetricsLoading}>
              {gpuMetricsLoading && !gpuMetrics ? (
                <div className="flex items-center gap-2 px-3 py-3 text-xs text-txt-tertiary">
                  <Loader2 className="h-3.5 w-3.5 animate-spin" /> {t('Checking GPUs…')}</div>
              ) : gpuMetricsError && !gpuMetrics ? null : gpuPreviewRows.length === 0 ? (
                <div className="px-3 py-3 text-xs text-txt-tertiary">
                  {gpuSelectionMode === 'specified' ? t('No configured GPU indices were found.') : t('No NVIDIA GPUs detected.')}
                </div>
              ) : (
                <div className="divide-y divide-border-subtle">
                  {gpuPreviewRows.map(({ gpu, reasons }) => {
                    const key = gpuKey(gpu)
                    const expanded = expandedGpuKey === key
                    const freeGiB = gpuMemoryGiB(gpuFreeMemoryMb(gpu))
                    return (
                      <div key={key}>
                        <button
                          type="button"
                          aria-expanded={expanded}
                          onClick={() => setExpandedGpuKey(expanded ? null : key)}
                          className="touch-target flex w-full items-center gap-3 px-3 py-2.5 text-left transition-colors hover:bg-surface-overlay focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-accent/30"
                        >
                          <span className="min-w-0 flex-1 truncate text-xs font-medium text-txt-primary" title={gpu.name}>
                            GPU {gpu.index} · {gpu.name}
                          </span>
                          <span className="hidden flex-none text-2xs tabular-nums text-txt-tertiary sm:inline">
                            {formatGiB(freeGiB)} / {formatGiB(gpuMemoryGiB(gpu.mem_total))} {t('free')}
                          </span>
                          <span className={clsx(
                            'flex-none text-xs font-medium',
                            !gpuSchedulerEnabled
                              ? 'text-txt-secondary'
                              : reasons.length
                                ? 'text-rose-700 dark:text-rose-300'
                                : 'text-emerald-700 dark:text-emerald-300',
                          )}>
                            {!gpuSchedulerEnabled ? t('Preview') : reasons.length ? t('Blocked') : t('Pass')}
                          </span>
                          <ChevronDown className={clsx('h-3.5 w-3.5 flex-none text-txt-tertiary transition-transform', expanded && 'rotate-180')} />
                        </button>
                        {expanded && (
                          <div className="border-t border-border-subtle bg-surface-overlay/45 px-3 py-2 text-2xs leading-5 text-txt-secondary">
                            <div className="mb-1 tabular-nums sm:hidden">{formatGiB(freeGiB)} / {formatGiB(gpuMemoryGiB(gpu.mem_total))} {t('free')}</div>
                            {reasons.length
                              ? reasons.map(reason => <div key={reason}>• {reason}</div>)
                              : !gpuSchedulerEnabled
                                ? <div>{t('Memory and idle thresholds pass. Scheduling is off.')}</div>
                                : <div>{t('Memory and idle thresholds pass. All requested GPUs must pass the launch checks.')}</div>}
                          </div>
                        )}
                      </div>
                    )
                  })}
                </div>
              )}
            </div>

            <details className="group rounded-md border border-border-subtle bg-surface-raised">
              <summary className="touch-target flex cursor-pointer list-none items-center justify-between gap-3 px-3 py-2.5 text-xs font-medium text-txt-primary hover:bg-surface-overlay">
                <span>{t('Advanced scheduling rules')}</span>
                <span className="flex items-center gap-2 text-2xs font-normal text-txt-tertiary">
                  <span className="hidden sm:inline">{t('Selection and visibility')}</span>
                  <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
                </span>
              </summary>
              <div className="space-y-3 border-t border-border-subtle p-3">
                <div>
                  <div className="mb-1 text-xs font-medium text-txt-secondary">{t('GPU selection')}</div>
                  <div className="inline-flex rounded-md bg-surface-overlay p-0.5">
                    {[
                      { id: 'auto' as GpuSelectionMode, label: t('Auto pick') },
                      { id: 'specified' as GpuSelectionMode, label: t('Specific indices') },
                    ].map(item => (
                      <button
                        key={item.id}
                        type="button"
                        onClick={() => { setGpuSelectionMode(item.id); markRuntimeDirty('gpu') }}
                        aria-pressed={gpuSelectionMode === item.id}
                        className={clsx(
                          'touch-target h-11 rounded-md px-2.5 text-xs font-medium transition-colors sm:h-7',
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
                    <span className="mb-1 block text-xs font-medium text-txt-secondary">{t('GPU indices')}</span>
                    <input
                      value={gpuDeviceIds}
                      onChange={event => { setGpuDeviceIds(event.target.value); markRuntimeDirty('gpu') }}
                      className="touch-input h-11 w-full rounded-md border border-border-subtle bg-surface-overlay px-2.5 font-mono text-sm text-txt-primary outline-none transition focus:border-accent focus:ring-2 focus:ring-accent/15 sm:h-9"
                      placeholder="0,1"
                    />
                  </label>
                )}

                <label className="touch-target flex items-start gap-2 text-xs text-txt-secondary">
                  <input
                    type="checkbox"
                    checked={gpuRespectCudaVisibleDevices}
                    onChange={event => { setGpuRespectCudaVisibleDevices(event.target.checked); markRuntimeDirty('gpu') }}
                    className="mt-0.5 h-4 w-4 rounded border-border-subtle bg-surface-overlay text-accent focus:ring-accent/25"
                  />
                  <span>
                    {t('Respect task CUDA_VISIBLE_DEVICES')}
                    <span className="mt-0.5 block text-2xs leading-4 text-txt-tertiary">
                      {t('Indices, GPU UUIDs and MIG IDs are validated. Invalid identifiers never bypass scheduling safeguards.')}
                    </span>
                  </span>
                </label>

                <label className="touch-target flex items-center gap-2 text-xs text-txt-secondary">
                  <input
                    type="checkbox"
                    checked={gpuRequireSameModel}
                    onChange={event => { setGpuRequireSameModel(event.target.checked); markRuntimeDirty('gpu') }}
                    className="h-4 w-4 rounded border-border-subtle bg-surface-overlay text-accent focus:ring-accent/25"
                  />
                  <span>{t('Require the same model for multi-GPU tasks')}</span>
                </label>
              </div>
            </details>

            <div className="flex items-end justify-between gap-3 border-t border-border-subtle pt-3">
              <button
                type="button"
                onClick={resetGpuScheduler}
                disabled={saving}
                className="touch-target inline-flex h-11 items-center gap-1.5 rounded-md px-2 text-xs font-medium text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary disabled:opacity-50 sm:h-8"
              >
                <RotateCcw className="h-3.5 w-3.5" /> {t('Reset')}</button>
              <div className="text-right">
                <button
                  type="button"
                  onClick={saveGpuScheduler}
                  disabled={saving || !runtimeDirtyPages.gpu || gpuValidationIssues.length > 0}
                  className="touch-target inline-flex h-11 min-w-20 items-center justify-center gap-1.5 rounded-md bg-accent px-3 text-xs font-medium text-white transition-colors hover:bg-accent-hover disabled:cursor-not-allowed disabled:bg-surface-hover disabled:text-txt-tertiary sm:h-8"
                >
                  {saving ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
                  {t('Save')}
                </button>
                {gpuValidationIssues.length > 0 && (
                  <div className="mt-1 text-2xs text-txt-tertiary">{t('Fix {{count}} rules', { count: gpuValidationIssues.length })}</div>
                )}
              </div>
            </div>
          </section>
        )}
      </div>
      <ConfirmDialog
        open={discardConfirmOpen}
        title={t('Discard unsaved runtime changes?')}
        description={t('Your runtime edits have not been saved. Closing this panel will discard them.')}
        confirmLabel={t('Discard')}
        confirmVariant="danger"
        onConfirm={() => {
          setDiscardConfirmOpen(false)
          clearAllRuntimeDirty()
          setRuntimeDirty(false)
          onClose()
        }}
        onCancel={() => setDiscardConfirmOpen(false)}
      />
    </div>
  )
}

function CompactNumberField({
  label,
  value,
  suffix,
  min,
  max,
  disabled = false,
  onChange,
}: {
  label: string
  value: string
  suffix: string
  min: number
  max?: number
  disabled?: boolean
  onChange: (value: string) => void
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-txt-secondary">{label}</span>
      <div className="touch-input flex h-11 overflow-hidden rounded-md border border-border-subtle bg-surface-overlay focus-within:border-accent focus-within:ring-2 focus-within:ring-accent/15 sm:h-9">
        <input
          type="number"
          min={min}
          max={max}
          step="any"
          disabled={disabled}
          value={value}
          onChange={event => onChange(event.target.value)}
          className="min-w-0 flex-1 bg-transparent px-2.5 text-sm text-txt-primary outline-none"
        />
        <span className="flex min-w-11 items-center justify-center border-l border-border-subtle px-2 text-xs text-txt-tertiary">{suffix}</span>
      </div>
    </label>
  )
}
