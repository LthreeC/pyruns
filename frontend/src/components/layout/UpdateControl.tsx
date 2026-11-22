import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Clock3, RefreshCw } from 'lucide-react'
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
import { t, useTranslation } from '@/i18n'
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
interface UpdateFailure { title: string; detail: string }

function hasUnsavedWork() {
  return useRuntimeStore.getState().dirty || useGeneratorStore.getState().dirty
    || useTaskDetailDraftStore.getState().dirty || useLauncherStore.getState().loading
}

function scheduledLabel(scheduled: UiScheduledOperation) {
  return scheduled.operation === 'restart'
    ? t('Restart scheduled to load v{{version}}', { version: scheduled.target_version })
    : t('Update scheduled for v{{version}}', { version: scheduled.target_version })
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
            requesting: 'Requesting update or restart…',
            acknowledged: 'Request accepted. Waiting for the interfaces to restart…',
            disconnected: 'The service is temporarily unreachable. Waiting to reconnect…',
            verifying: 'The restarted service responded. Loading the result…',
          }[phase])}
        </p>
        <p className="mt-2 text-xs tabular-nums text-txt-secondary">
          {t('Target v{{version}} · Waited {{seconds}}s', { version: targetVersion, seconds: elapsed })}
        </p>
        {!restarting && <p className="mt-3 text-xs leading-5 text-txt-secondary">
          {t('The web service stops during installation. Live pip progress is unavailable until it returns; this page reconnects automatically.')}
        </p>}
        {takingLong && <p className="mt-3 text-xs text-amber-800 dark:text-amber-300">
          {t('This is taking longer than usual. Reloading checks the service again; it does not start another installation.')}
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

export default function UpdateControl({ compact = false }: { compact?: boolean }) {
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
  const instanceIdRef = useRef('')
  const systemInfoRevision = useRef(0)
  const reportedResults = useRef(new Set<string>())
  const notify = useToastStore(state => state.notify)

  const showFailure = useCallback((title: string, detail: string) => {
    setFailure({ title, detail })
    setFailureOpen(true)
  }, [])
  useEffect(() => {
    const dialog = failureDialog.current
    if (failureOpen && dialog && !dialog.open) dialog.showModal()
    else if (!failureOpen && dialog?.open) dialog.close()
  }, [failureOpen])
  const closeFailure = () => {
    setFailureOpen(false)
    window.requestAnimationFrame(() => updateButton.current?.focus())
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
    if (result.ok) {
      if (alreadySeen) return
      notify({
        tone: 'success',
        title: t('Pyruns updated'),
        detail: result.installed_version || info.version,
      })
    } else {
      const detail = result.error
        ? t('Update failed: {{error}} Version {{version}} was restarted.', { error: result.error, version: info.version })
        : result.exit_code === 0
          ? t('pip completed without installing the confirmed release; version {{version}} was restarted.', { version: info.version })
          : result.exit_code === 124
            ? t('The update timed out; version {{version}} was restarted.', { version: info.version })
            : t('pip exited with code {{code}}; version {{version}} was restarted.', { code: result.exit_code, version: info.version })
      setFailure({ title: t('Pyruns update failed'), detail })
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

  const waitForRestart = useCallback(async (previousInstanceId: string) => {
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
        throw new Error(t('Pyruns stopped the update before restarting. Check the terminal details and try again.'))
      }
      if (Date.now() - startedAt >= SLOW_UPDATE_MS) {
        setTakingLong(true)
      }
    }
    throw new Error(
      t('Pyruns did not restart within 8 minutes. Check the terminal status, then try again.'),
    )
  }, [])

  const handleUpdateResponse = useCallback(async (response: UiUpdateResponse) => {
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
    setPhase('acknowledged')
    await waitForRestart(response.instance_id)
  }, [notify, waitForRestart])

  const startUpdate = useCallback(async () => {
    if (checking || operation) return

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

    const confirmed = await requestConfirmation({
      title: t('Update Pyruns to v{{version}}?', { version: versionCheck.latest_version }),
      description: t('Pyruns upgrades from v{{version}} and restarts the interfaces sharing this installation when the affected tasks finish. Save open drafts before restarting.', { version: versionCheck.current_version }),
      confirmLabel: t('Update and Restart'),
    })
    if (!confirmed) return

    setOperation('update')
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
      await handleUpdateResponse(response)
    } catch (error) {
      setOperation(null)
      showFailure(t('Could not update Pyruns'), errorMessage(error))
    }
  }, [checking, handleUpdateResponse, notify, operation, showFailure])

  const startRestart = useCallback(async () => {
    if (checking || operation || !systemInfo?.restart_required) return
    setFailureOpen(false)

    const targetVersion = systemInfo.installed_version || systemInfo.version
    const confirmed = await requestConfirmation({
      title: t('Restart Pyruns to load v{{version}}?', { version: targetVersion }),
      description: t('Pyruns restarts the interfaces sharing this installation when their active work finishes. Save open drafts before restarting.'),
      confirmLabel: t('Restart Interfaces'),
    })
    if (!confirmed) return

    setOperation('restart')
    setPhase('requesting')
    setStartedAt(Date.now())
    setTargetVersion(targetVersion)
    setTakingLong(false)
    try {
      const response = await requestWithTimeout(
        signal => api.restartPyruns(signal),
        UPDATE_START_TIMEOUT_MS,
        t('Pyruns did not acknowledge the restart request within 30 seconds. Try again.'),
      )
      await handleUpdateResponse(response)
    } catch (error) {
      setOperation(null)
      showFailure(t('Could not restart Pyruns'), errorMessage(error))
    }
  }, [checking, handleUpdateResponse, operation, systemInfo, showFailure])

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

  if (!systemInfo) {
    return (
      <div
        aria-hidden="true"
        className={clsx('h-10 flex-none', compact ? 'w-10' : 'w-[78px]')}
      />
    )
  }
  const restartRequired = systemInfo.restart_required
  const backendRestarting = systemInfo.update_state === 'restarting'
  const schedule = systemInfo.scheduled_operation
  const waiting = !backendRestarting && schedule && !schedule.error ? schedule : null
  if (!systemInfo.update_supported && !failure && !waiting && !reloadReady) return null
  const busy = checking || operation !== null || backendRestarting
  const showFailureButton = failure && !waiting && !reloadReady
  let buttonLabel = t('Check for Pyruns updates, installed version {{version}}', { version: systemInfo.version })
  if (reloadReady) buttonLabel = t('Reload updated interface')
  else if (waiting) buttonLabel = scheduledLabel(waiting)
  else if (showFailureButton) buttonLabel = t('View update failure details')
  else if (checking) buttonLabel = t('Checking for Pyruns updates')
  else if (operation === 'update') buttonLabel = t('Updating Pyruns')
  else if (operation === 'restart' || backendRestarting) buttonLabel = t('Restarting Pyruns')
  else if (restartRequired) buttonLabel = t(
    'Restart Pyruns to load installed version {{installed}}; this interface is running {{current}}',
    { installed: systemInfo.installed_version, current: systemInfo.version },
  )

  return (
    <>
      <button
        ref={updateButton}
        type="button"
        onClick={() => {
          if (reloadReady) window.location.reload()
          else if (waiting) void cancelScheduled(waiting)
          else if (failure) setFailureOpen(true)
          else void (restartRequired ? startRestart() : startUpdate())
        }}
        disabled={!reloadReady && (cancelling || (showFailureButton ? checking || operation !== null : busy))}
        aria-label={buttonLabel}
        title={buttonLabel}
        className={clsx(
          'relative flex h-10 flex-none items-center justify-center gap-1 rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/35 disabled:cursor-wait disabled:opacity-60',
          (waiting || reloadReady || restartRequired) && 'bg-amber-500/10 text-amber-700 hover:bg-amber-500/15 dark:text-amber-300',
          compact ? 'h-11 w-10 sm:h-11' : 'w-[78px] px-1.5',
        )}
      >
        {!compact && (
          <span className="min-w-0 truncate font-mono text-[10px] text-txt-secondary">
            v{restartRequired ? systemInfo.installed_version : systemInfo.version}
          </span>
        )}
        {waiting && !reloadReady ? (
          <Clock3 aria-hidden="true" className="h-3.5 w-3.5 flex-none" />
        ) : showFailureButton && !busy ? <AlertTriangle aria-hidden="true" className="h-3.5 w-3.5 text-rose-700 dark:text-rose-300" /> : <RefreshCw
          aria-hidden="true"
          className={clsx(
            'h-3.5 w-3.5 flex-none',
            busy && 'animate-spin motion-reduce:animate-none',
            restartRequired && !busy && 'text-amber-600 dark:text-amber-300',
          )}
        />}
        {restartRequired && !waiting && !reloadReady && (
          <span
            aria-hidden="true"
            className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-amber-500"
          />
        )}
      </button>
      <UpdateProgressDialog operation={operation} takingLong={takingLong} phase={phase} startedAt={startedAt} targetVersion={targetVersion} />
      <dialog ref={failureDialog} aria-labelledby="pyruns-update-failure-title" onCancel={event => { event.preventDefault(); closeFailure() }}
        className="fixed inset-0 z-[160] m-auto max-h-[calc(100dvh-2rem)] w-[min(520px,calc(100vw-1.5rem))] overflow-y-auto rounded-md border border-border-subtle bg-surface-raised p-5 text-txt-primary shadow-xl backdrop:bg-black/55">
        <h2 id="pyruns-update-failure-title" className="text-base font-semibold">{failure?.title}</h2>
        <pre className="my-4 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-surface-base p-3 font-mono text-xs">{failure?.detail}</pre>
        <div className="flex flex-wrap justify-end gap-2">
          <CopyButton value={failure?.detail || ''} text={t('Copy failure details')} label={t('Copy failure details')} />
          <button type="button" onClick={closeFailure} className="touch-target min-h-11 rounded-md border border-border px-3 text-sm">{t('Close')}</button>
          <button type="button" disabled={busy || Boolean(waiting) || reloadReady || !systemInfo.update_supported} onClick={() => {
            setFailure(null)
            void (restartRequired ? startRestart() : startUpdate())
          }} className="touch-target min-h-11 rounded-md bg-accent px-3 text-sm text-white disabled:opacity-50">{t('Retry')}</button>
        </div>
      </dialog>
    </>
  )
}
