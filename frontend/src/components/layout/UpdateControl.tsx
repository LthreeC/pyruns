import { useCallback, useEffect, useRef, useState } from 'react'
import { RefreshCw } from 'lucide-react'
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
import type { SystemInfo, UiVersionCheck } from '@/types'
import { t, useTranslation } from '@/i18n'

const RESTART_POLL_MS = 500
const RESTART_REQUEST_TIMEOUT_MS = 5_000
const UPDATE_CHECK_TIMEOUT_MS = 10_000
const UPDATE_START_TIMEOUT_MS = 30_000
const INSTANCE_POLL_MS = 5_000
const SLOW_UPDATE_MS = 90_000
const UPDATE_RESTART_TIMEOUT_MS = 480_000
const UPDATE_NOTICE_PREFIX = 'pyruns.update.notice.'

type FullProcessOperation = 'update' | 'restart'

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
}: {
  operation: FullProcessOperation | null
  takingLong: boolean
}) {
  const ref = useRef<HTMLDialogElement>(null)
  const open = operation !== null
  const restarting = operation === 'restart'

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
          {takingLong
            ? restarting
              ? t('The shared restart is still waiting. The terminal contains the current status.')
              : t('The package update is still running. The terminal contains the current pip status.')
            : restarting
              ? t('Restarting every idle interface that shares this installation...')
              : t('Installing the package and restarting every interface that shares it...')}
        </p>
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
  const instanceIdRef = useRef('')
  const runtimeDirty = useRuntimeStore(state => state.dirty)
  const generatorDirty = useGeneratorStore(state => state.dirty)
  const taskDetailDirty = useTaskDetailDraftStore(state => state.dirty)
  const launcherLoading = useLauncherStore(state => state.loading)
  const notify = useToastStore(state => state.notify)
  const hasUnsavedWork = runtimeDirty || generatorDirty || taskDetailDirty || launcherLoading

  const reportUpdateResult = useCallback((info: SystemInfo) => {
    const result = info.last_update
    if (!result) return
    const noticeKey = `${UPDATE_NOTICE_PREFIX}${info.instance_id}`
    try {
      if (window.sessionStorage.getItem(noticeKey) === '1') return
      window.sessionStorage.setItem(noticeKey, '1')
    } catch {
      // A blocked session store should not hide the update result.
    }
    if (result.ok) {
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
      notify({
        tone: 'error',
        title: t('Pyruns update failed'),
        detail,
      })
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
      try {
        const info = await requestWithTimeout(
          signal => api.getSystemInfo(signal),
          RESTART_REQUEST_TIMEOUT_MS,
          t('Pyruns status check timed out.'),
          controller.signal,
        )
        if (!active) return
        if (instanceIdRef.current && info.instance_id !== instanceIdRef.current) {
          window.location.reload()
          return
        }
        instanceIdRef.current = info.instance_id
        setSystemInfo(info)
        reportUpdateResult(info)
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
  }, [reportUpdateResult])

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
      }
      if (info && info.instance_id !== previousInstanceId) {
        window.location.reload()
        return
      }
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

  const startUpdate = useCallback(async () => {
    if (checking || operation) return

    setChecking(true)
    let versionCheck: UiVersionCheck
    try {
      versionCheck = await requestWithTimeout(
        signal => api.checkPyrunsUpdate(signal),
        UPDATE_CHECK_TIMEOUT_MS,
        t('The Pyruns update check did not respond within 10 seconds. Try again.'),
      )
    } catch (error) {
      notify({
        tone: 'error',
        title: t('Could not check for updates'),
        detail: errorMessage(error),
      })
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
      description: hasUnsavedWork
        ? t('Upgrade from v{{version}}. Every interface sharing this installation will restart when idle, and unsaved drafts will be discarded. No update starts while a task is queued or running.', { version: versionCheck.current_version })
        : t('Upgrade from v{{version}}, then restart every interface sharing this installation. No update starts while a task is queued or running.', { version: versionCheck.current_version }),
      confirmLabel: t('Update and Restart'),
    })
    if (!confirmed) return

    setOperation('update')
    setTakingLong(false)
    try {
      const response = await requestWithTimeout(
        signal => api.updatePyruns(versionCheck.latest_version, signal),
        UPDATE_START_TIMEOUT_MS,
        t('Pyruns did not acknowledge the update request within 30 seconds. Try again.'),
      )
      await waitForRestart(response.instance_id)
    } catch (error) {
      setOperation(null)
      notify({
        tone: 'error',
        title: t('Could not update Pyruns'),
        detail: errorMessage(error),
      })
    }
  }, [checking, hasUnsavedWork, notify, operation, waitForRestart])

  const startRestart = useCallback(async () => {
    if (checking || operation || !systemInfo?.restart_required) return

    const targetVersion = systemInfo.installed_version || systemInfo.version
    const confirmed = await requestConfirmation({
      title: t('Restart Pyruns to load v{{version}}?', { version: targetVersion }),
      description: hasUnsavedWork
        ? t('This interface is running v{{current}}, while v{{installed}} is installed. Every interface sharing this installation will restart when idle, and unsaved drafts will be discarded.', { current: systemInfo.version, installed: targetVersion })
        : t('This interface is running v{{current}}, while v{{installed}} is installed. Restart every interface sharing this installation when idle.', { current: systemInfo.version, installed: targetVersion }),
      confirmLabel: t('Restart Interfaces'),
    })
    if (!confirmed) return

    setOperation('restart')
    setTakingLong(false)
    try {
      const response = await requestWithTimeout(
        signal => api.restartPyruns(signal),
        UPDATE_START_TIMEOUT_MS,
        t('Pyruns did not acknowledge the restart request within 30 seconds. Try again.'),
      )
      await waitForRestart(response.instance_id)
    } catch (error) {
      setOperation(null)
      notify({
        tone: 'error',
        title: t('Could not restart Pyruns'),
        detail: errorMessage(error),
      })
    }
  }, [checking, hasUnsavedWork, notify, operation, systemInfo, waitForRestart])

  if (!systemInfo) {
    return (
      <div
        aria-hidden="true"
        className={clsx('h-10 flex-none', compact ? 'w-10' : 'w-[78px]')}
      />
    )
  }
  if (!systemInfo.update_supported) return null

  const restartRequired = systemInfo.restart_required
  const backendRestarting = systemInfo.update_state === 'restarting'
  const busy = checking || operation !== null || backendRestarting
  const buttonLabel = checking
    ? t('Checking for Pyruns updates')
    : operation === 'update'
      ? t('Updating Pyruns')
      : operation === 'restart' || backendRestarting
        ? t('Restarting Pyruns')
        : restartRequired
          ? t('Restart Pyruns to load installed version {{installed}}; this interface is running {{current}}', { installed: systemInfo.installed_version, current: systemInfo.version })
          : t('Check for Pyruns updates, installed version {{version}}', { version: systemInfo.version })

  return (
    <>
      <button
        type="button"
        onClick={() => void (restartRequired ? startRestart() : startUpdate())}
        disabled={busy}
        aria-label={buttonLabel}
        title={buttonLabel}
        className={clsx(
          'relative flex h-10 flex-none items-center justify-center gap-1 rounded-md text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/35 disabled:cursor-wait disabled:opacity-60',
          restartRequired && 'bg-amber-500/10 text-amber-700 hover:bg-amber-500/15 dark:text-amber-300',
          compact ? 'h-11 w-10 sm:h-11' : 'w-[78px] px-1.5',
        )}
      >
        {!compact && (
          <span className="min-w-0 truncate font-mono text-[10px] text-txt-secondary">
            v{restartRequired ? systemInfo.installed_version : systemInfo.version}
          </span>
        )}
        <RefreshCw
          aria-hidden="true"
          className={clsx(
            'h-3.5 w-3.5 flex-none',
            busy && 'animate-spin motion-reduce:animate-none',
            restartRequired && !busy && 'text-amber-600 dark:text-amber-300',
          )}
        />
        {restartRequired && (
          <span
            aria-hidden="true"
            className="absolute right-1 top-1 h-1.5 w-1.5 rounded-full bg-amber-500"
          />
        )}
      </button>
      <UpdateProgressDialog operation={operation} takingLong={takingLong} />
    </>
  )
}
