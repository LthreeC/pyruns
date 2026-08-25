import {
  Component,
  Suspense,
  lazy,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ComponentType,
  type ReactNode,
} from 'react'
import {
  Routes,
  Route,
  type BlockerFunction,
  useBlocker,
  useLocation,
  useNavigate,
  useSearchParams,
} from 'react-router-dom'
import { RefreshCw, ShieldAlert } from 'lucide-react'
import AppShell from '@/components/layout/AppShell'
import ConfirmDialog from '@/components/shared/ConfirmDialog'
import ConfirmationHost from '@/components/shared/ConfirmationHost'
import ToastHost from '@/components/shared/ToastHost'
import { ApiError, beginAuthorizationAttempt, recoverSession, subscribeUnauthorized } from '@/api'
import {
  applyThemeClass,
  WorkspaceChangeRequiresDiscardError,
  useGeneratorStore,
  useLauncherStore,
  useRuntimeStore,
  useTaskDetailDraftStore,
  useWorkspaceStore,
  useThemeStore,
} from '@/store'

function lazyWithReload<T extends ComponentType<any>>(
  key: string,
  loader: () => Promise<{ default: T }>,
) {
  const storageKey = `pyruns.chunk-reload.${key}`
  return lazy(async () => {
    try {
      const module = await loader()
      try {
        window.sessionStorage.removeItem(storageKey)
      } catch {
        // Session storage is optional; successful loading needs no recovery state.
      }
      return module
    } catch (error) {
      let shouldReload = false
      try {
        shouldReload = window.sessionStorage.getItem(storageKey) !== '1'
        if (shouldReload) {
          window.sessionStorage.setItem(storageKey, '1')
        }
      } catch {
        // Fall through to the visible recovery screen when storage is unavailable.
      }
      if (shouldReload) {
        await new Promise(resolve => window.setTimeout(resolve, 250))
        window.location.reload()
        return new Promise<never>(() => {})
      }
      throw error
    }
  })
}

const DashboardPage = lazyWithReload('dashboard', () => import('@/components/dashboard/DashboardPage'))
const GeneratorPage = lazyWithReload('generator', () => import('@/components/generator/GeneratorPage'))
const ManagerPage = lazyWithReload('manager', () => import('@/components/manager/ManagerPage'))
const MonitorPage = lazyWithReload('monitor', () => import('@/components/monitor/MonitorPage'))
const LauncherPage = lazyWithReload('launcher', () => import('@/components/launcher/LauncherPage'))

class RouteErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  render() {
    if (!this.state.failed) {
      return this.props.children
    }
    return (
      <div role="alert" className="flex min-h-dvh items-center justify-center bg-surface-base p-6">
        <div className="w-full max-w-md rounded-lg border border-border-default bg-surface-raised p-6 text-center shadow-lg">
          <h1 className="text-base font-semibold text-txt-primary">Reconnect to the interface</h1>
          <p className="mt-2 text-sm leading-6 text-txt-secondary">
            This screen could not finish loading. The interface may have been updated while it was open.
          </p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="touch-target mt-5 inline-flex min-h-11 items-center justify-center rounded-md bg-accent px-4 text-sm font-semibold text-white transition-colors hover:bg-accent-hover sm:min-h-10"
          >
            Reload interface
          </button>
        </div>
      </div>
    )
  }
}

function RouteLoadingFallback() {
  return (
    <div role="status" aria-live="polite" className="flex h-full min-h-[16rem] items-center justify-center bg-surface-base text-sm text-txt-tertiary">
      Loading workspace...
    </div>
  )
}

type ConnectionState =
  | { status: 'connecting' }
  | { status: 'ready' }
  | { status: 'unauthorized' }
  | { status: 'workspace-changed'; unsavedChanges: string[] }
  | { status: 'failed'; message: string }

function describeUnsavedChanges(changes: string[]) {
  if (changes.length <= 1) {
    return changes[0] || 'local changes'
  }
  return `${changes.slice(0, -1).join(', ')} and ${changes[changes.length - 1]}`
}

function ConnectionScreen({
  state,
  onRetry,
}: {
  state: Exclude<ConnectionState, { status: 'ready' }>
  onRetry: () => void
}) {
  if (state.status === 'connecting') {
    return (
      <main
        role="status"
        aria-live="polite"
        className="flex min-h-dvh items-center justify-center bg-surface-base p-6 text-sm text-txt-tertiary"
      >
        Connecting to the Pyruns server...
      </main>
    )
  }

  const unauthorized = state.status === 'unauthorized'
  const workspaceChanged = state.status === 'workspace-changed'
  return (
    <main role="alert" className="flex min-h-dvh items-center justify-center bg-surface-base p-6">
      <div className="w-full max-w-md text-center">
        <ShieldAlert aria-hidden="true" className="mx-auto h-8 w-8 text-amber-700 dark:text-amber-300" />
        <h1 className="mt-4 text-lg font-semibold text-txt-primary">
          {workspaceChanged ? 'Workspace changed' : unauthorized ? 'Session expired' : 'Unable to connect'}
        </h1>
        <p className="mt-2 text-sm leading-6 text-txt-secondary">
          {workspaceChanged
            ? `The server selected another workspace. Reconnecting will discard these unsaved changes: ${describeUnsavedChanges(state.unsavedChanges)}.`
            : unauthorized
            ? 'This browser no longer has access to the local Pyruns server. Open the latest UI link shown in the terminal, or retry after restoring the session.'
            : 'The local Pyruns server did not finish loading the workspace. Check that it is still running, then try again.'}
        </p>
        {state.status === 'failed' && (
          <p className="mt-2 break-words font-mono text-xs leading-5 text-txt-tertiary">{state.message}</p>
        )}
        <button
          type="button"
          onClick={onRetry}
          className="mt-5 inline-flex min-h-11 items-center justify-center gap-2 rounded-md bg-accent px-4 text-sm font-semibold text-white transition-colors hover:bg-accent-hover"
        >
          <RefreshCw aria-hidden="true" className="h-4 w-4" />
          {workspaceChanged ? 'Discard drafts and reconnect' : 'Retry connection'}
        </button>
      </div>
    </main>
  )
}

function ConnectionOverlay({
  state,
  onRetry,
}: {
  state: Exclude<ConnectionState, { status: 'ready' }>
  onRetry: () => void
}) {
  const connecting = state.status === 'connecting'
  const unauthorized = state.status === 'unauthorized'
  const workspaceChanged = state.status === 'workspace-changed'
  const actionRef = useRef<HTMLButtonElement>(null)
  const title = connecting
    ? 'Reconnecting'
    : workspaceChanged
      ? 'Workspace changed'
      : unauthorized
        ? 'Session expired'
        : 'Unable to connect'

  return (
    <div
      role={connecting ? 'status' : 'alertdialog'}
      aria-live={connecting ? 'polite' : undefined}
      aria-modal={connecting ? undefined : 'true'}
      aria-labelledby="connection-overlay-title"
      aria-describedby="connection-overlay-description"
      onPointerDown={event => event.stopPropagation()}
      onPointerUp={event => event.stopPropagation()}
      onClick={event => event.stopPropagation()}
      onKeyDown={event => {
        event.stopPropagation()
        if (!connecting && event.key === 'Tab') {
          event.preventDefault()
          actionRef.current?.focus()
        }
      }}
      className="fixed inset-0 z-[1000] flex items-center justify-center bg-black/50 p-6"
    >
      <div className="w-full max-w-md rounded-md border border-border-subtle bg-surface-raised p-6 text-center shadow-xl">
        {connecting
          ? <RefreshCw aria-hidden="true" className="mx-auto h-8 w-8 animate-spin text-accent" />
          : <ShieldAlert aria-hidden="true" className="mx-auto h-8 w-8 text-amber-700 dark:text-amber-300" />}
        <h1 id="connection-overlay-title" className="mt-4 text-lg font-semibold text-txt-primary">
          {title}
        </h1>
        <p id="connection-overlay-description" className="mt-2 text-sm leading-6 text-txt-secondary">
          {connecting
            ? 'Checking the local Pyruns server...'
            : workspaceChanged
              ? `The server selected another workspace. Your local edits are still intact: ${describeUnsavedChanges(state.unsavedChanges)}. Discard them only when you are ready to reconnect.`
              : unauthorized
              ? 'Open the latest UI link shown in the terminal, then retry here. Unsaved edits on this page are being kept.'
              : 'Check that the local Pyruns server is still running, then try again. Unsaved edits on this page are being kept.'}
        </p>
        {state.status === 'failed' && (
          <p className="mt-2 break-words font-mono text-xs leading-5 text-txt-tertiary">{state.message}</p>
        )}
        {!connecting && (
          <button
            ref={actionRef}
            type="button"
            autoFocus
            onClick={onRetry}
            className={`mt-5 inline-flex min-h-11 items-center justify-center gap-2 rounded-md px-4 text-sm font-semibold text-white transition-colors focus-visible:outline-none focus-visible:ring-2 ${
              workspaceChanged
                ? 'bg-rose-700 hover:bg-rose-800 focus-visible:ring-rose-500/40 dark:bg-rose-600 dark:hover:bg-rose-500'
                : 'bg-accent hover:bg-accent-hover focus-visible:ring-accent/40'
            }`}
          >
            <RefreshCw aria-hidden="true" className="h-4 w-4" />
            {workspaceChanged ? 'Discard drafts and reconnect' : 'Retry connection'}
          </button>
        )}
      </div>
    </div>
  )
}

const ROUTE_TITLES: Record<string, string> = {
  '/': 'Dashboard',
  '/launcher': 'Choose Workspace',
  '/generator': 'Generator',
  '/manager': 'Task Manager',
  '/monitor': 'Monitor',
}

const LAUNCHER_SEARCH_PARAMS = ['launcher', 'mode', 'script', 'config']

function searchWithoutLauncherState(search: string) {
  const params = new URLSearchParams(search)
  LAUNCHER_SEARCH_PARAMS.forEach(key => params.delete(key))
  return params.toString()
}

function NotFoundPage() {
  const navigate = useNavigate()
  return (
    <div className="flex h-full min-h-[20rem] items-center justify-center bg-surface-base p-6 text-center">
      <div>
        <h1 className="text-lg font-semibold text-txt-primary">Page not found</h1>
        <p className="mt-2 text-sm text-txt-secondary">This Pyruns page does not exist.</p>
        <button type="button" onClick={() => navigate('/')} className="touch-target mt-4 min-h-11 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-white sm:min-h-10">
          Go to Dashboard
        </button>
      </div>
    </div>
  )
}

export default function App() {
  const location = useLocation()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const [showLauncher, setShowLauncher] = useState(false)
  const [connectionState, setConnectionState] = useState<ConnectionState>({ status: 'connecting' })
  const connectionAttempt = useRef(0)
  const [hasConnected, setHasConnected] = useState(false)
  const appRoot = useRef<HTMLDivElement>(null)
  const focusBeforeConnection = useRef<HTMLElement | null>(null)
  const fetchWorkspace = useWorkspaceStore(s => s.fetch)
  const theme = useThemeStore(s => s.theme)
  const runtimeDirty = useRuntimeStore(s => s.dirty)
  const generatorDirty = useGeneratorStore(s => s.dirty)
  const launcherLoading = useLauncherStore(s => s.loading)
  const taskDetailDirty = useTaskDetailDraftStore(s => s.dirty)
  const shouldWarnBeforeUnload = runtimeDirty || generatorDirty || taskDetailDirty || launcherLoading
  const shouldBlockNavigation = useCallback<BlockerFunction>(
    ({ currentLocation, nextLocation }) => {
      if (useLauncherStore.getState().loading) {
        return true
      }
      if (!useTaskDetailDraftStore.getState().dirty) {
        return false
      }
      if (currentLocation.pathname !== nextLocation.pathname) {
        return true
      }
      return searchWithoutLauncherState(currentLocation.search)
        !== searchWithoutLauncherState(nextLocation.search)
    },
    [],
  )
  const navigationBlocker = useBlocker(shouldBlockNavigation)

  const rememberAppFocus = useCallback(() => {
    const activeElement = document.activeElement
    const root = appRoot.current
    if (!root) {
      return
    }
    if (focusBeforeConnection.current?.isConnected) {
      if (activeElement instanceof HTMLElement && root.contains(activeElement)) {
        activeElement.blur()
      }
      return
    }
    const activeInApp = activeElement instanceof HTMLElement && root.contains(activeElement)
    const activeCanRestore = activeInApp
      && activeElement.tabIndex >= 0
    const dialogs = Array.from(
      root.querySelectorAll<HTMLElement>('[role="dialog"][aria-modal="true"]'),
    )
    const visibleDialog = dialogs.reverse().find(dialog => dialog.getClientRects().length > 0)
    focusBeforeConnection.current = activeCanRestore ? activeElement : visibleDialog || null
    if (activeInApp) {
      activeElement.blur()
    }
  }, [])

  const connect = useCallback(async (discardUnsavedChanges = false) => {
    const attempt = ++connectionAttempt.current
    beginAuthorizationAttempt()
    setConnectionState({ status: 'connecting' })
    let error: unknown
    try {
      await fetchWorkspace({ discardUnsavedChanges })
      if (attempt === connectionAttempt.current) {
        setHasConnected(true)
        setConnectionState({ status: 'ready' })
      }
      return
    } catch (caught) {
      error = caught
    }

    if (attempt !== connectionAttempt.current) {
      return
    }
    if (error instanceof ApiError && error.status === 401) {
      if (await recoverSession() && attempt === connectionAttempt.current) {
        beginAuthorizationAttempt()
        try {
          await fetchWorkspace({ discardUnsavedChanges })
          if (attempt === connectionAttempt.current) {
            setHasConnected(true)
            setConnectionState({ status: 'ready' })
          }
          return
        } catch (retryError) {
          error = retryError
        }
      }
    }
    if (attempt !== connectionAttempt.current) {
      return
    }
    if (error instanceof ApiError && error.status === 401) {
      setConnectionState({ status: 'unauthorized' })
      return
    }
    if (error instanceof WorkspaceChangeRequiresDiscardError) {
      setConnectionState({
        status: 'workspace-changed',
        unsavedChanges: error.unsavedChanges,
      })
      return
    }
    setConnectionState({
      status: 'failed',
      message: error instanceof Error ? error.message : 'Unknown connection error',
    })
  }, [fetchWorkspace])

  useEffect(() => {
    applyThemeClass(theme)
  }, [theme])

  useEffect(() => subscribeUnauthorized(() => {
    rememberAppFocus()
    setConnectionState({ status: 'unauthorized' })
  }), [rememberAppFocus])

  useEffect(() => {
    if (appRoot.current) {
      appRoot.current.inert = hasConnected && connectionState.status !== 'ready'
    }
  }, [connectionState.status, hasConnected])

  useEffect(() => {
    void connect()
    return () => {
      connectionAttempt.current += 1
    }
  }, [connect])

  useEffect(() => {
    if (!shouldWarnBeforeUnload) {
      return
    }
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', handleBeforeUnload)
    return () => window.removeEventListener('beforeunload', handleBeforeUnload)
  }, [shouldWarnBeforeUnload])

  useEffect(() => {
    if (navigationBlocker.state === 'blocked' && !launcherLoading && !taskDetailDirty) {
      navigationBlocker.reset()
    }
  }, [launcherLoading, navigationBlocker, taskDetailDirty])

  useEffect(() => {
    setShowLauncher(searchParams.get('launcher') === '1' || location.pathname === '/launcher')
  }, [location.pathname, searchParams])

  useEffect(() => {
    const pageTitle = connectionState.status === 'unauthorized'
      ? 'Session expired'
      : connectionState.status === 'failed'
        ? 'Unable to connect'
        : ROUTE_TITLES[location.pathname] || 'Page not found'
    document.title = `${pageTitle} · Pyruns`
    if (connectionState.status !== 'ready') {
      return
    }
    const frame = window.requestAnimationFrame(() => {
      const previousFocus = focusBeforeConnection.current
      focusBeforeConnection.current = null
      if (previousFocus?.isConnected) {
        const dialog = previousFocus.matches('[role="dialog"][aria-modal="true"]')
          ? previousFocus
          : previousFocus.closest<HTMLElement>('[role="dialog"][aria-modal="true"]')
        const dialogFallback = dialog?.querySelector<HTMLElement>(
          '[data-runtime-initial-focus], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        )
        for (const target of [previousFocus, dialogFallback]) {
          target?.focus()
          if (target && document.activeElement === target) {
            return
          }
        }
      }
      document.getElementById('route-heading')?.focus()
    })
    return () => window.cancelAnimationFrame(frame)
  }, [connectionState.status, location.pathname])

  const closeLauncher = () => {
    setShowLauncher(false)
    if (location.pathname === '/launcher') {
      navigate('/', { replace: true })
      return
    }
    const nextParams = new URLSearchParams(searchParams)
    nextParams.delete('launcher')
    nextParams.delete('mode')
    nextParams.delete('script')
    nextParams.delete('config')
    setSearchParams(nextParams, { replace: true })
  }

  if (!hasConnected && connectionState.status !== 'ready') {
    return (
      <ConnectionScreen
        state={connectionState}
        onRetry={() => void connect(connectionState.status === 'workspace-changed')}
      />
    )
  }

  const connectionBlocked = connectionState.status !== 'ready'
  return (
    <>
      <div
        ref={appRoot}
        aria-hidden={connectionBlocked || undefined}
      >
        <RouteErrorBoundary key={location.pathname}>
          <Suspense fallback={<RouteLoadingFallback />}>
            <Routes>
              <Route element={<AppShell />}>
                <Route index element={<DashboardPage />} />
                <Route path="launcher" element={<DashboardPage />} />
                <Route path="generator" element={<GeneratorPage />} />
                <Route path="manager" element={<ManagerPage />} />
                <Route path="monitor" element={<MonitorPage />} />
                <Route path="*" element={<NotFoundPage />} />
              </Route>
            </Routes>
            {showLauncher && <LauncherPage onClose={closeLauncher} />}
          </Suspense>
        </RouteErrorBoundary>
        <ConfirmDialog
          open={navigationBlocker.state === 'blocked' && !launcherLoading && taskDetailDirty}
          title="Discard unsaved task details?"
          description="Your Notes, Env, or rename draft will be lost when you leave this page."
          confirmLabel="Discard and Leave"
          confirmVariant="danger"
          onConfirm={() => {
            if (navigationBlocker.state === 'blocked') {
              navigationBlocker.proceed()
            }
          }}
          onCancel={() => {
            if (navigationBlocker.state === 'blocked') {
              navigationBlocker.reset()
            }
          }}
        />
        <ConfirmationHost />
        <ToastHost />
      </div>
      {connectionBlocked && (
        <ConnectionOverlay
          state={connectionState}
          onRetry={() => void connect(connectionState.status === 'workspace-changed')}
        />
      )}
    </>
  )
}
