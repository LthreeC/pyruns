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

const RESTART_POLL_MS = 500
const INSTANCE_POLL_MS = 5_000
const SLOW_UPDATE_MS = 90_000
const UPDATE_NOTICE_PREFIX = 'pyruns.update.notice.'

type FullProcessOperation = 'update' | 'restart'

function wait(milliseconds: number) {
  return new Promise(resolve => window.setTimeout(resolve, milliseconds))
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
          {restarting ? 'Restarting Pyruns' : 'Updating Pyruns'}
        </h2>
        <p id="pyruns-update-detail" aria-live="polite" className="mt-2 text-sm leading-6 text-txt-secondary">
          {takingLong
            ? restarting
              ? 'The shared restart is still waiting. The terminal contains the current status.'
              : 'The package update is still running. The terminal contains the current pip status.'
            : restarting
              ? 'Restarting every idle interface that shares this installation...'
              : 'Installing the package and restarting every interface that shares it...'}
        </p>
        {takingLong && (
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="touch-target mt-5 inline-flex min-h-11 items-center justify-center gap-2 rounded-md border border-border-subtle px-4 text-sm font-medium text-txt-primary transition-colors hover:bg-surface-overlay focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/35 sm:min-h-10"
          >
            <RefreshCw aria-hidden="true" className="h-4 w-4" />
            Reload interface
          </button>
        )}
      </div>
    </dialog>
  )
}

export default function UpdateControl({ compact = false }: { compact?: boolean }) {
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
        title: 'Pyruns updated',
        detail: result.installed_version || info.version,
      })
    } else {
      const detail = result.exit_code === 0
        ? `pip completed without installing the confirmed release; version ${info.version} was restarted.`
        : `pip exited with code ${result.exit_code}; version ${info.version} was restarted.`
      notify({
        tone: 'error',
        title: 'Pyruns update failed',
        detail,
      })
    }
  }, [notify])

  useEffect(() => {
    let active = true
    let polling = false
    const pollInstance = async () => {
      if (!active || polling) return
      polling = true
      try {
        const info = await api.getSystemInfo()
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
        polling = false
      }
    }
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') void pollInstance()
    }
    void pollInstance()
    const timer = window.setInterval(() => void pollInstance(), INSTANCE_POLL_MS)
    document.addEventListener('visibilitychange', handleVisibility)
    return () => {
      active = false
      window.clearInterval(timer)
      document.removeEventListener('visibilitychange', handleVisibility)
    }
  }, [reportUpdateResult])

  const waitForRestart = useCallback(async (previousInstanceId: string) => {
    const startedAt = Date.now()
    while (true) {
      await wait(RESTART_POLL_MS)
      try {
        const info = await api.getSystemInfo()
        if (info.instance_id !== previousInstanceId) {
          window.location.reload()
          return
        }
      } catch {
        // A connection failure is expected while the old process is replaced.
      }
      if (Date.now() - startedAt >= SLOW_UPDATE_MS) {
        setTakingLong(true)
      }
    }
  }, [])

  const startUpdate = useCallback(async () => {
    if (checking || operation) return

    setChecking(true)
    let versionCheck: UiVersionCheck
    try {
      versionCheck = await api.checkPyrunsUpdate()
    } catch (error) {
      notify({
        tone: 'error',
        title: 'Could not check for updates',
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
        title: sameVersion ? 'Pyruns is up to date' : 'No newer Pyruns release',
        detail: sameVersion
          ? `v${versionCheck.latest_version} is the latest version on PyPI.`
          : `Installed v${versionCheck.current_version}; PyPI currently offers v${versionCheck.latest_version}.`,
      })
      return
    }

    const confirmed = await requestConfirmation({
      title: `Update Pyruns to v${versionCheck.latest_version}?`,
      description: hasUnsavedWork
        ? `Upgrade from v${versionCheck.current_version}. Every interface sharing this installation will restart when idle, and unsaved drafts will be discarded. No update starts while a task is queued or running.`
        : `Upgrade from v${versionCheck.current_version}, then restart every interface sharing this installation. No update starts while a task is queued or running.`,
      confirmLabel: 'Update and Restart',
    })
    if (!confirmed) return

    setOperation('update')
    setTakingLong(false)
    try {
      const response = await api.updatePyruns(versionCheck.latest_version)
      await waitForRestart(response.instance_id)
    } catch (error) {
      setOperation(null)
      notify({
        tone: 'error',
        title: 'Could not update Pyruns',
        detail: errorMessage(error),
      })
    }
  }, [checking, hasUnsavedWork, notify, operation, waitForRestart])

  const startRestart = useCallback(async () => {
    if (checking || operation || !systemInfo?.restart_required) return

    const targetVersion = systemInfo.installed_version || systemInfo.version
    const confirmed = await requestConfirmation({
      title: `Restart Pyruns to load v${targetVersion}?`,
      description: hasUnsavedWork
        ? `This interface is running v${systemInfo.version}, while v${targetVersion} is installed. Every interface sharing this installation will restart when idle, and unsaved drafts will be discarded.`
        : `This interface is running v${systemInfo.version}, while v${targetVersion} is installed. Restart every interface sharing this installation when idle.`,
      confirmLabel: 'Restart Interfaces',
    })
    if (!confirmed) return

    setOperation('restart')
    setTakingLong(false)
    try {
      const response = await api.restartPyruns()
      await waitForRestart(response.instance_id)
    } catch (error) {
      setOperation(null)
      notify({
        tone: 'error',
        title: 'Could not restart Pyruns',
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
    ? 'Checking for Pyruns updates'
    : operation === 'update'
      ? 'Updating Pyruns'
      : operation === 'restart' || backendRestarting
        ? 'Restarting Pyruns'
        : restartRequired
          ? `Restart Pyruns to load installed version ${systemInfo.installed_version}; this interface is running ${systemInfo.version}`
          : `Check for Pyruns updates, installed version ${systemInfo.version}`

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
