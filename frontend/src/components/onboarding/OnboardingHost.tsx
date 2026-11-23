import { useEffect, useId, useRef, useState } from 'react'
import { ArrowRight, Compass, LoaderCircle, X } from 'lucide-react'
import { useLocation, useNavigate } from 'react-router-dom'
import {
  useGeneratorStore,
  useLauncherStore,
  usePreferencesStore,
  useRuntimeStore,
  useTaskDetailDraftStore,
} from '@/store'
import { useTranslation } from '@/i18n'
import { useOnboardingStore } from './store'

export default function OnboardingHost({ enabled }: { enabled: boolean }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const location = useLocation()
  const phase = useOnboardingStore(state => state.phase)
  const hasSeen = useOnboardingStore(state => state.hasSeen)
  const generatorDirty = useGeneratorStore(state => state.dirty)
  const runtimeDirty = useRuntimeStore(state => state.dirty)
  const detailDirty = useTaskDetailDraftStore(state => state.dirty)
  const launcherLoading = useLauncherStore(state => state.loading)
  const preferencesOpen = usePreferencesStore(state => state.isOpen)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const startButtonRef = useRef<HTMLButtonElement>(null)
  const titleId = useId()
  const descriptionId = useId()
  const [error, setError] = useState(false)
  const [loading, setLoading] = useState(false)
  const [tourModule, setTourModule] = useState<typeof import('./tour') | null>(null)
  const dirty = generatorDirty || runtimeDirty || detailDirty || launcherLoading
  const launcherOpen = location.pathname === '/launcher' || new URLSearchParams(location.search).get('launcher') === '1'
  const panelOpen = preferencesOpen || launcherOpen || Boolean(document.querySelector('[data-tour-panel="runtime"]'))

  useEffect(() => {
    if (!enabled || hasSeen || dirty || panelOpen || phase !== 'idle') return
    const offerGuide = () => {
      if (document.querySelector('[data-tour="home"]') && !document.querySelector('[data-route-unavailable], dialog[open], [role="dialog"]')) {
        useOnboardingStore.getState().open(document.querySelector<HTMLElement>('[data-tour="preferences"]'))
      }
    }
    // The sidebar can render while the first workspace route is still loading.
    const observer = new MutationObserver(offerGuide)
    observer.observe(document.getElementById('root')!, { childList: true, subtree: true })
    const frame = window.requestAnimationFrame(offerGuide)
    return () => {
      window.cancelAnimationFrame(frame)
      observer.disconnect()
    }
  }, [enabled, hasSeen, dirty, panelOpen, phase, location.key])

  useEffect(() => {
    const dialog = dialogRef.current
    if (enabled && phase === 'welcome') {
      dialog?.showModal()
      startButtonRef.current?.focus()
    }
    return () => dialog?.close()
  }, [enabled, phase])

  useEffect(() => {
    if (!enabled && phase !== 'idle') useOnboardingStore.getState().finish()
  }, [enabled, phase])

  useEffect(() => {
    if (!loading) return
    if (!enabled || phase !== 'welcome' || dirty || panelOpen) {
      setLoading(false)
      return
    }
    let cancelled = false
    void import('./tour').then(module => {
      if (cancelled) return
      setTourModule(module)
      setLoading(false)
      useOnboardingStore.getState().start()
    }).catch(() => {
      if (cancelled) return
      setLoading(false)
      setError(true)
    })
    return () => { cancelled = true }
  }, [enabled, phase, loading, dirty, panelOpen])

  useEffect(() => {
    if (!enabled || phase !== 'tour' || !tourModule) return
    const showError = () => {
      useOnboardingStore.getState().finish()
      setError(true)
      useOnboardingStore.getState().open()
    }
    let tour: { destroy: () => void } | undefined
    try {
      tour = tourModule.startOnboardingTour({
        navigate,
        onFinish: completed => useOnboardingStore.getState().finish(completed),
        onError: showError,
      })
    } catch {
      showError()
    }
    return () => tour?.destroy()
  }, [enabled, phase, navigate, tourModule])

  if (!enabled || phase !== 'welcome') return null

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      aria-modal="true"
      className="m-auto max-h-[calc(100dvh-2rem)] w-[calc(100vw-2rem)] max-w-md overflow-y-auto rounded-lg border border-border-default bg-surface-raised p-0 text-txt-primary shadow-xl backdrop:bg-black/45"
      onCancel={event => { event.preventDefault(); useOnboardingStore.getState().finish() }}
    >
      <div className="px-6 pb-6 pt-5 sm:px-7 sm:pb-7">
        <div className="mb-5 flex items-center justify-between gap-3">
          <span className="inline-flex h-10 w-10 items-center justify-center rounded-lg bg-accent/10 text-accent-ink">
            <Compass aria-hidden="true" className="h-5 w-5" />
          </span>
          <button
            type="button"
            aria-label={t('Close beginner guide')}
            title={t('Close beginner guide')}
            onClick={() => useOnboardingStore.getState().finish()}
            className="touch-target flex h-11 w-11 items-center justify-center rounded-md text-txt-tertiary hover:bg-surface-overlay hover:text-txt-primary"
          >
            <X aria-hidden="true" className="h-4 w-4" />
          </button>
        </div>
        <h2 id={titleId} className="text-xl font-semibold">{t('Welcome to Pyruns')}</h2>
        <p id={descriptionId} className="mt-2 text-sm leading-6 text-txt-secondary">
          {t('Take a guided tour of your workspace, task tools, and live logs.')}
        </p>
        <p className="mt-3 text-xs leading-5 text-txt-tertiary">
          {t('Explore the interface without creating tasks, running commands, or changing your workspace.')}
        </p>
        {(dirty || panelOpen || error) && (
          <p role="status" className="mt-4 text-xs leading-5 text-amber-700 dark:text-amber-300">
            {t(dirty
              ? 'Save your changes before starting the tour. Your draft will stay here.'
              : panelOpen
                ? 'Close the open panel before starting the tour.'
                : 'The guide could not load this section. Please try again.')}
          </p>
        )}
        <div className="mt-6 flex flex-wrap items-center justify-end gap-2 border-t border-border-subtle pt-5">
          <button
            type="button"
            onClick={() => useOnboardingStore.getState().finish()}
            className="touch-target min-h-11 rounded-md px-3 py-2 text-sm font-medium text-txt-secondary hover:bg-surface-overlay"
          >
            {t('Maybe later')}
          </button>
          <button
            type="button"
            ref={startButtonRef}
            disabled={dirty || panelOpen || loading}
            aria-busy={loading}
            onClick={() => { setError(false); setLoading(true) }}
            className="touch-target inline-flex min-h-11 items-center justify-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-white hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-50"
          >
            {t(loading ? 'Loading guide...' : 'Start tour')}
            {loading
              ? <LoaderCircle aria-hidden="true" className="h-4 w-4 motion-safe:animate-spin" />
              : <ArrowRight aria-hidden="true" className="h-4 w-4" />}
          </button>
        </div>
      </div>
    </dialog>
  )
}
