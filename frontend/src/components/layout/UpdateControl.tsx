import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Clock3, Download, RefreshCw } from 'lucide-react'
import clsx from 'clsx'

import * as api from '@/api'
import { errorMessage } from '@/utils/errors'
import {
  requestConfirmation,
  useGeneratorStore,
  useLauncherStore,
  useRuntimeStore,
  useTaskDetailDraftStore,
  useToastStore,
} from '@/store'
import type { SystemInfo, UiScheduledOperation, UiUpdateResponse, UiVersionCheck } from '@/types'
import { t, translateMessage, useTranslation } from '@/i18n'
import CopyButton from '@/components/shared/CopyButton'

const RESTART_POLL_MS = 500
const RESTART_REQUEST_TIMEOUT_MS = 5_000
const UPDATE_CHECK_TIMEOUT_MS = 10_000
const UPDATE_START_TIMEOUT_MS = 30_000
const INSTANCE_POLL_MS = 5_000
const SLOW_UPDATE_MS = 90_000
const UPDATE_RESTART_TIMEOUT_MS = 480_000
const UPDATE_NOTICE_PREFIX = 'pyruns.update.notice.'

type FullProcessOperation = 'update' | 'restart'
type UpdatePhase = 'requesting' | 'acknowledged' | 'disconnected' | 'verifying'
interface UpdateFailure {
  title: string
  detail: string
  operation: FullProcessOperation
  retryable?: boolean
}

const RESTART_DESCRIPTION = 'This restarts this Web UI using the files on disk. Tasks continue running. Save open drafts before continuing.'
const LEGACY_RESTART_REFUSAL = 'The running UI already matches the installed Pyruns version.'

function hasUnsavedWork() {
  return useRuntimeStore.getState().dirty || useGeneratorStore.getState().dirty
    || useTaskDetailDraftStore.getState().dirty || useLauncherStore.getState().loading
}

function scheduledLabel(scheduled: UiScheduledOperation) {
  if (scheduled.operation === 'upgrade') {
    return t('Update scheduled for v{{version}}', { version: scheduled.target_version })
  }
  return scheduled.target_version
    ? t('Restart scheduled to load v{{version}}', { version: scheduled.target_version })
    : t('Restart scheduled')
}

function wait(milliseconds: number) {
  return new Promise(resolve => window.setTimeout(resolve, milliseconds))
}

async function requestWithTimeout<T>(
  request: (signal: AbortSignal) => Promise<T>,
  milliseconds: number,
  timeoutMessage: string,
  parentSignal?: AbortSignal,
) {
  const controller = new AbortController()
  const abortFromParent = () => controller.abort()
  if (parentSignal?.aborted) controller.abort()
  else parentSignal?.addEventListener('abort', abortFromParent, { once: true })
  const requestTimeout = window.setTimeout(() => controller.abort(), milliseconds)
  try {
    return await request(controller.signal)
  } catch (error) {
    if (parentSignal?.aborted) throw error
    if (controller.signal.aborted) throw new Error(timeoutMessage)
    throw error
  } finally {
    window.clearTimeout(requestTimeout)
    parentSignal?.removeEventListener('abort', abortFromParent)
  }
}

function UpdateProgressDialog({
  operation,
  takingLong,
  phase,
  startedAt,
  targetVersion,
}: {
  operation: FullProcessOperation | null
  takingLong: boolean
  phase: UpdatePhase
  startedAt: number
  targetVersion: string
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const open = operation !== null
  const restarting = operation === 'restart'
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    if (!open) return
    const tick = () => setElapsed(Math.max(0, Math.floor((Date.now() - startedAt) / 1000)))
    tick()
    const timer = window.setInterval(tick, 1000)
    return () => window.clearInterval(timer)
  }, [open, startedAt])

  useEffect(() => {
    const dialog = ref.current
    if (open && dialog && !dialog.open) {
      dialog.showModal()
    } else if (!open && dialog?.open) {
      dialog.close()
    }
  }, [open])

  if (!open) return null

  return (
    <dialog
      ref={ref}
      aria-labelledby="pyruns-update-title"
      aria-describedby="pyruns-update-detail"
      aria-modal="true"
      onCancel={event => event.preventDefault()}
      className="fixed inset-0 z-[160] m-auto w-[min(420px,calc(100vw-1.5rem))] rounded-md border border-border-subtle bg-surface-raised p-0 shadow-xl backdrop:bg-black/55"
    >
      <div className="px-6 py-7 text-center">
        <RefreshCw
          aria-hidden="true"
          className="mx-auto h-7 w-7 animate-spin text-accent motion-reduce:animate-none"
        />
        <h2 id="pyruns-update-title" className="mt-4 text-base font-semibold text-txt-primary">
          {restarting ? t('Restarting Pyruns') : t('Updating Pyruns')}
        </h2>
        <p id="pyruns-update-detail" aria-live="polite" className="mt-2 text-sm leading-6 text-txt-secondary">
          {t({
            requesting: restarting ? 'Requesting a UI restart…' : 'Requesting an update…',
            acknowledged: restarting ? 'Request accepted. Waiting for this interface to restart…' : 'Request accepted. Waiting for installation and restart…',
            disconnected: 'The service is temporarily unreachable. Waiting to reconnect…',
            verifying: 'The restarted service responded. Loading the result…',
          }[phase])}
        </p>
        <p className="mt-2 text-xs tabular-nums text-txt-secondary">
          {targetVersion
            ? t('Target v{{version}} · Waited {{seconds}}s', { version: targetVersion, seconds: elapsed })
            : t('Waited {{seconds}}s', { seconds: elapsed })}
        </p>
        {!restarting && <p className="mt-3 text-xs leading-5 text-txt-secondary">
          {t('The web service stops during installation. Live pip progress is unavailable until it returns; this page reconnects automatically.')}
        </p>}
        {takingLong && <p className="mt-3 text-xs text-amber-800 dark:text-amber-300">
          {restarting
            ? t('This is taking longer than usual. Reload to check if the service is back.')
            : t('This is taking longer than usual. Reloading checks the service again; it does not start another installation.')}
        </p>}
        {takingLong && (
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="touch-target mt-5 inline-flex min-h-11 items-center justify-center gap-2 rounded-md border border-border-subtle px-4 text-sm font-medium text-txt-primary transition-colors hover:bg-surface-overlay focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/35 sm:min-h-10"
          >
            <RefreshCw aria-hidden="true" className="h-4 w-4" />
            {t('Reload interface')}
          </button>
        )}
      </div>
    </dialog>
  )
}

export default function UpdateControl({ compact, showVersion, showRestartLabel }: {
  compact: boolean; showVersion: boolean; showRestartLabel: boolean
}) {
  useTranslation()
  const [systemInfo, setSystemInfo] = useState<SystemInfo | null>(null)
  const [checking, setChecking] = useState(false)
  const [operation, setOperation] = useState<FullProcessOperation | null>(null)
  const [takingLong, setTakingLong] = useState(false)
  const [phase, setPhase] = useState<UpdatePhase>('requesting')
  const [startedAt, setStartedAt] = useState(0)
  const [targetVersion, setTargetVersion] = useState('')
  const [cancelling, setCancelling] = useState(false)
  const [reloadReady, setReloadReady] = useState(false)
  const [failure, setFailure] = useState<UpdateFailure | null>(null)
  const [failureOpen, setFailureOpen] = useState(false)
  const failureDialog = useRef<HTMLDialogElement>(null)
  const updateButton = useRef<HTMLButtonElement>(null)
  const restartButton = useRef<HTMLButtonElement>(null)
  const instanceIdRef = useRef('')
  const systemInfoRevision = useRef(0)
  const reportedResults = useRef(new Set<string>())
  const notify = useToastStore(state => state.notify)
  const updateSupported = systemInfo?.update_supported === true
  const restartSupported = systemInfo?.restart_supported ?? systemInfo?.update_supported ?? false

  const showFailure = useCallback((title: string, detail: string, operation: FullProcessOperation = 'update', retryable = true) => {
    setFailure({ title, detail, operation, retryable })
    setFailureOpen(true)
  }, [])
  useEffect(() => {
    const dialog = failureDialog.current
    if (failureOpen && dialog && !dialog.open) dialog.showModal()
    else if (!failureOpen && dialog?.open) dialog.close()
  }, [failureOpen])
  const closeFailure = () => {
    setFailureOpen(false)
    const trigger = failure?.operation === 'restart' ? restartButton : updateButton
    window.requestAnimationFrame(() => (trigger.current ?? updateButton.current)?.focus())
  }

  const reportUpdateResult = useCallback((info: SystemInfo) => {
    const result = info.last_update
    if (!result) return
    const noticeKey = `${UPDATE_NOTICE_PREFIX}${info.instance_id}`
    if (reportedResults.current.has(noticeKey)) return
    reportedResults.current.add(noticeKey)
    let alreadySeen = false
    try {
      alreadySeen = window.sessionStorage.getItem(noticeKey) === '1'
      window.sessionStorage.setItem(noticeKey, '1')
    } catch {
      // A blocked session store should not hide the update result.
    }
    const restart = result.operation === 'restart'
    if (result.ok) {
      if (alreadySeen) return
      notify({
        tone: 'success',
        title: t(restart ? 'Pyruns restarted' : 'Pyruns updated'),
        detail: result.installed_version || info.version,
      })
    } else {
      const detail = restart
        ? result.error || t('This interface restarted with version {{version}}.', { version: info.version })
        : result.error
        ? t('Update failed: {{error}} Version {{version}} was restarted.', { error: result.error, version: info.version })
        : result.exit_code === 0
          ? t('pip completed without installing the confirmed release; version {{version}} was restarted.', { version: info.version })
          : result.exit_code === 124
            ? t('The update timed out; version {{version}} was restarted.', { version: info.version })
            : t('pip exited with code {{code}}; version {{version}} was restarted.', { code: result.exit_code, version: info.version })
      setFailure({ title: t(restart ? 'Could not load the requested version' : 'Pyruns update failed'), detail, operation: restart ? 'restart' : 'update' })
      if (!alreadySeen) setFailureOpen(true)
    }
  }, [notify])

  useEffect(() => {
    let active = true
    let polling = false
    let timer: number | undefined
    let requestController: AbortController | null = null
    const pollInstance = async () => {
      if (!active || polling) return
      polling = true
      const controller = new AbortController()
      requestController = controller
      const revision = systemInfoRevision.current
      try {
        const info = await requestWithTimeout(
          signal => api.getSystemInfo(signal),
          RESTART_REQUEST_TIMEOUT_MS,
          t('Pyruns status check timed out.'),
          controller.signal,
        )
        if (!active || revision !== systemInfoRevision.current) return
        if (instanceIdRef.current && info.instance_id !== instanceIdRef.current) {
          if (hasUnsavedWork()) setReloadReady(true)
          else window.location.reload()
          return
        }
        instanceIdRef.current = info.instance_id
        setSystemInfo(info)
        reportUpdateResult(info)
        const scheduled = info.scheduled_operation
        if (scheduled?.error && !reportedResults.current.has(scheduled.id)) {
          reportedResults.current.add(scheduled.id)
          showFailure(
            scheduled.operation === 'restart' ? t('Could not restart Pyruns') : t('Could not update Pyruns'),
            scheduled.error,
            scheduled.operation === 'restart' ? 'restart' : 'update',
          )
        }
      } catch {
        // Connection loss is expected while this or another shared UI restarts.
      } finally {
        // An aborted request can settle after a newly started visible-tab
        // request. Only the current request may release the polling guard;
        // otherwise the interval can start overlapping status requests.
        if (requestController === controller) {
          polling = false
          requestController = null
        }
      }
    }
    const stopPolling = () => {
      if (timer !== undefined) {
        window.clearInterval(timer)
        timer = undefined
      }
      requestController?.abort()
      requestController = null
      polling = false
    }
    const startPolling = () => {
      if (!active || document.visibilityState === 'hidden' || timer !== undefined) return
      timer = window.setInterval(() => void pollInstance(), INSTANCE_POLL_MS)
    }
    const handleVisibility = () => {
      if (document.visibilityState === 'hidden') {
        stopPolling()
        return
      }
      void pollInstance()
      startPolling()
    }
    if (document.visibilityState !== 'hidden') {
      void pollInstance()
      startPolling()
    }
    document.addEventListener('visibilitychange', handleVisibility)
    return () => {
      active = false
      stopPolling()
      document.removeEventListener('visibilitychange', handleVisibility)
    }
  }, [reportUpdateResult, showFailure])

  const waitForRestart = useCallback(async (previousInstanceId: string, operation: FullProcessOperation) => {
    const startedAt = Date.now()
    while (Date.now() - startedAt < UPDATE_RESTART_TIMEOUT_MS) {
      await wait(RESTART_POLL_MS)
      let info: SystemInfo | undefined
      try {
        info = await requestWithTimeout(
          signal => api.getSystemInfo(signal),
          RESTART_REQUEST_TIMEOUT_MS,
          t('Pyruns status check timed out.'),
        )
      } catch {
        // A connection failure is expected while the old process is replaced.
        setPhase('disconnected')
      }
      if (info && info.instance_id !== previousInstanceId) {
        setPhase('verifying')
        window.location.reload()
        return
      }
      if (info) setPhase('acknowledged')
      if (info && ['idle', 'restart_required'].includes(info.update_state)) {
        setSystemInfo(info)
        throw new Error(t(operation === 'restart'
          ? 'Pyruns did not complete the restart. Check the terminal details and try again.'
          : 'Pyruns stopped the update before restarting. Check the terminal details and try again.'))
      }
      if (Date.now() - startedAt >= SLOW_UPDATE_MS) {
        setTakingLong(true)
      }
    }
    throw new Error(
      t('Pyruns did not restart within 8 minutes. Check the terminal status, then try again.'),
    )
  }, [])

  const handleOperationResponse = useCallback(async (response: UiUpdateResponse, operation: FullProcessOperation) => {
    const scheduled = response.scheduled_operation
    if (response.state === 'waiting' && scheduled) {
      systemInfoRevision.current += 1
      setSystemInfo(info => info && { ...info, update_state: 'waiting', scheduled_operation: scheduled })
      setOperation(null)
      notify({
        tone: 'info',
        title: scheduledLabel(scheduled),
        detail: t('Starts automatically when the affected tasks finish.'),
      })
      return
    }
    setOperation(operation)
    if (response.target_version !== undefined) setTargetVersion(response.target_version)
    setPhase('acknowledged')
    await waitForRestart(response.instance_id, operation)
  }, [notify, waitForRestart])

  const startUpdate = useCallback(async () => {
    if (checking || operation || !updateSupported) return

    setChecking(true)
    setFailureOpen(false)
    let versionCheck: UiVersionCheck
    try {
      versionCheck = await requestWithTimeout(
        signal => api.checkPyrunsUpdate(signal),
        UPDATE_CHECK_TIMEOUT_MS,
        t('The Pyruns update check did not respond within 10 seconds. Try again.'),
      )
    } catch (error) {
      showFailure(t('Could not check for updates'), errorMessage(error))
      return
    } finally {
      setChecking(false)
    }

    if (!versionCheck.update_available) {
      const sameVersion = versionCheck.current_version === versionCheck.latest_version
      notify({
        tone: 'info',
        title: sameVersion ? t('Pyruns is up to date') : t('No newer Pyruns release'),
        detail: sameVersion
          ? t('v{{version}} is the latest version on PyPI.', { version: versionCheck.latest_version })
          : t('Installed v{{current}}; PyPI currently offers v{{latest}}.', { current: versionCheck.current_version, latest: versionCheck.latest_version }),
      })
      return
    }

    let restarting = versionCheck.operation === 'restart'
    const confirmed = await requestConfirmation(restarting ? {
      title: t('Restart Pyruns to load v{{version}}?', { version: versionCheck.latest_version }),
      description: t(RESTART_DESCRIPTION),
      confirmLabel: t('Restart UI'),
    } : {
      title: t('Update Pyruns to v{{version}}?', { version: versionCheck.latest_version }),
      description: t('The Web UI will briefly disconnect. Tasks continue running. Save open drafts before continuing.'),
      confirmLabel: t('Update and Restart'),
    })
    if (!confirmed) return

    setOperation(restarting ? 'restart' : 'update')
    setPhase('requesting')
    setStartedAt(Date.now())
    setTargetVersion(versionCheck.latest_version)
    setTakingLong(false)
    try {
      const response = await requestWithTimeout(
        signal => api.updatePyruns(versionCheck.latest_version, signal),
        UPDATE_START_TIMEOUT_MS,
        t('Pyruns did not acknowledge the update request within 30 seconds. Try again.'),
      )
      if (response.operation) restarting = response.operation === 'restart'
      await handleOperationResponse(response, restarting ? 'restart' : 'update')
    } catch (error) {
      setOperation(null)
      showFailure(t(restarting ? 'Could not restart Pyruns' : 'Could not update Pyruns'), errorMessage(error), restarting ? 'restart' : 'update')
    }
  }, [checking, handleOperationResponse, notify, operation, showFailure, updateSupported])

  const startRestart = useCallback(async () => {
    if (checking || operation || !restartSupported) return
    setFailureOpen(false)

    const confirmed = await requestConfirmation({
      title: t('Restart this Web UI?'),
      description: t(RESTART_DESCRIPTION),
      confirmLabel: t('Restart UI'),
    })
    if (!confirmed) return

    setOperation('restart')
    setPhase('requesting')
    setStartedAt(Date.now())
    setTargetVersion('')
    setTakingLong(false)
    try {
      const response = await requestWithTimeout(
        signal => api.restartPyruns(signal),
        UPDATE_START_TIMEOUT_MS,
        t('Pyruns did not acknowledge the restart request within 30 seconds. Try again.'),
      )
      await handleOperationResponse(response, 'restart')
    } catch (error) {
      setOperation(null)
      const legacy = error instanceof api.ApiError
        && (error.status === 404 || error.status === 405 || error.message === LEGACY_RESTART_REFUSAL)
      const detail = legacy
        ? t('This service is running an older Pyruns build. Stop it from its original terminal, then run pyr ui from the environment with the updated build.')
        : errorMessage(error)
      showFailure(t('Could not restart Pyruns'), detail, 'restart', !legacy)
    }
  }, [checking, handleOperationResponse, operation, restartSupported, showFailure])

  const cancelScheduled = async (scheduled: UiScheduledOperation) => {
    const confirmed = await requestConfirmation({
      title: scheduledLabel(scheduled),
      description: t('Starts automatically when the affected tasks finish.'),
      confirmLabel: t('Cancel schedule'),
    })
    if (!confirmed) return
    setCancelling(true)
    try {
      await requestWithTimeout(
        signal => api.cancelScheduledPyrunsOperation(scheduled.id, signal),
        RESTART_REQUEST_TIMEOUT_MS,
        t('Pyruns did not acknowledge the cancellation.'),
      )
      systemInfoRevision.current += 1
      setSystemInfo(info => info?.scheduled_operation?.id === scheduled.id ? {
        ...info,
        update_state: info.restart_required ? 'restart_required' : 'idle',
        scheduled_operation: null,
      } : info)
      notify({ tone: 'info', title: t('Schedule cancelled') })
    } catch (error) {
      notify({ tone: 'error', title: t('Could not cancel the schedule'), detail: errorMessage(error) })
    } finally {
      setCancelling(false)
    }
  }

  const version = systemInfo?.version
  const restartRequired = systemInfo?.restart_required
  const backendRestarting = systemInfo?.update_state === 'restarting'
  const schedule = systemInfo?.scheduled_operation
  const waiting = !backendRestarting && schedule && !schedule.error ? schedule : null
  const updateFailed = failure?.operation === 'update'
  const updateUnavailable = !updateSupported && !updateFailed && !waiting && !reloadReady
  const showUpdateButton = !systemInfo || restartSupported || !updateUnavailable
  const showRestartButton = !systemInfo || restartSupported || updateSupported
  if (!showUpdateButton && !showRestartButton) return null
  const busy = checking || operation !== null || backendRestarting
  const showFailureButton = updateFailed && !waiting && !reloadReady
  const controlsClass = clsx(
    'touch-target inline-flex items-center justify-center gap-1 rounded-md px-1.5 text-txt-secondary transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/35',
    compact ? 'size-11' : 'h-10 min-w-8',
  )
  const interactiveClass = 'enabled:hover:bg-surface-overlay enabled:hover:text-txt-primary disabled:opacity-60'
  const readingStatusLabel = t('Reading Pyruns status…')
  const unavailableReason = updateUnavailable && systemInfo
    ? translateMessage(systemInfo.update_unavailable_reason || 'Pyruns updates are unavailable on this server.')
    : undefined
  const restartLabel = !systemInfo ? readingStatusLabel
    : !restartSupported ? t('UI restart is unavailable on this server.')
    : restartRequired ? t('Restart Pyruns to load installed version {{installed}}; this interface is running {{current}}', {
      installed: systemInfo.installed_version, current: systemInfo.version,
    }) : t('Restart UI')
  let buttonLabel = readingStatusLabel
  if (version) buttonLabel = t(updateUnavailable
    ? 'Pyruns updates unavailable, running version {{version}}'
    : 'Check for Pyruns updates, installed version {{version}}', { version })
  if (reloadReady) buttonLabel = t('Reload updated interface')
  else if (waiting) buttonLabel = scheduledLabel(waiting)
  else if (showFailureButton) buttonLabel = t('View update failure details')
  else if (checking) buttonLabel = t('Checking for Pyruns updates')
  else if (operation === 'update') buttonLabel = t('Updating Pyruns')
  else if (operation === 'restart' || backendRestarting) buttonLabel = t('Restarting Pyruns')

  return (
    <>
      <div className={clsx('flex flex-none items-center gap-0.5', compact && 'flex-col')}>
        {showUpdateButton && (
          <button
            ref={updateButton}
            type="button"
            onClick={() => {
              if (reloadReady) window.location.reload()
              else if (waiting) void cancelScheduled(waiting)
              else if (updateFailed) setFailureOpen(true)
              else void startUpdate()
            }}
            disabled={!systemInfo || (!reloadReady && (cancelling || (showFailureButton ? checking || operation !== null : busy)))}
            aria-disabled={Boolean(systemInfo && updateUnavailable) || undefined}
            aria-label={buttonLabel}
            aria-description={unavailableReason}
            title={unavailableReason || buttonLabel}
            className={clsx(controlsClass, updateUnavailable ? 'cursor-not-allowed' : interactiveClass,
              'disabled:cursor-wait', (waiting || reloadReady) && 'bg-amber-500/10 text-amber-700 dark:text-amber-300')}
          >
            {waiting && !reloadReady ? <Clock3 aria-hidden="true" className="size-4 shrink-0" />
              : showFailureButton && !busy ? <AlertTriangle aria-hidden="true" className="size-4 shrink-0 text-rose-700 dark:text-rose-300" />
                : checking || operation === 'update' || reloadReady ? <RefreshCw aria-hidden="true" className={clsx('size-4 shrink-0', !reloadReady && 'motion-safe:animate-spin')} />
                  : <Download aria-hidden="true" className={clsx('size-4 shrink-0', updateUnavailable && 'opacity-50')} />}
            {showVersion && <span className={clsx('max-w-24 truncate font-mono text-[11px]', !version && 'w-[9ch]')}
              aria-hidden={!version || undefined}>{version ? `v${version}` : '—'}</span>}
          </button>
        )}
        {showRestartButton && (
          <button
            ref={restartButton}
            type="button"
            onClick={() => void startRestart()}
            disabled={!restartSupported || busy || Boolean(waiting) || cancelling || reloadReady}
            aria-label={t('Restart UI')}
            title={restartLabel}
            className={clsx(controlsClass, interactiveClass, 'whitespace-nowrap text-xs font-medium', restartSupported ? 'disabled:cursor-wait' : 'disabled:cursor-not-allowed', restartRequired && 'bg-amber-500/10 text-amber-800 dark:text-amber-300')}
          >
            <RefreshCw aria-hidden="true" className={clsx('size-4 shrink-0', (operation === 'restart' || backendRestarting) && 'motion-safe:animate-spin')} />
            {showRestartLabel && <span>{t('Restart UI')}</span>}
          </button>
        )}
      </div>
      <UpdateProgressDialog operation={operation} takingLong={takingLong} phase={phase} startedAt={startedAt} targetVersion={targetVersion} />
      <dialog ref={failureDialog} aria-labelledby="pyruns-update-failure-title" onCancel={event => { event.preventDefault(); closeFailure() }}
        className="fixed inset-0 z-[160] m-auto max-h-[calc(100dvh-2rem)] w-[min(520px,calc(100vw-1.5rem))] overflow-y-auto rounded-md border border-border-subtle bg-surface-raised p-5 text-txt-primary shadow-xl backdrop:bg-black/55">
        <h2 id="pyruns-update-failure-title" className="text-base font-semibold">{failure?.title}</h2>
        <pre className="my-4 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-surface-base p-3 font-mono text-xs">{failure?.detail}</pre>
        <div className="flex flex-wrap justify-end gap-2">
          <CopyButton value={failure?.detail || ''} text={t('Copy failure details')} label={t('Copy failure details')} />
          <button type="button" onClick={closeFailure} className="touch-target min-h-11 rounded-md border border-border px-3 text-sm">{t('Close')}</button>
          {failure?.retryable !== false && <button type="button" disabled={busy || Boolean(waiting) || reloadReady || !(failure?.operation === 'restart' ? restartSupported : updateSupported)} onClick={() => {
            setFailure(null)
            void (failure?.operation === 'restart' ? startRestart() : startUpdate())
          }} className="touch-target min-h-11 rounded-md bg-accent px-3 text-sm text-white disabled:opacity-50">{t('Retry')}</button>}
        </div>
      </dialog>
    </>
  )
}
