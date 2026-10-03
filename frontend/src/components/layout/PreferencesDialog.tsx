import { useEffect, useId, useRef, useState } from 'react'
import { Check, ChevronDown, Moon, Settings2, Sun, X } from 'lucide-react'
import clsx from 'clsx'
import {
  DEFAULT_SEARCH_SETTINGS,
  usePreferencesStore,
  useSearchHistoryStore,
  useSearchSettingsStore,
  useThemeStore,
} from '@/store'

export default function PreferencesDialog() {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const backdropPointerStarted = useRef(false)
  const titleId = useId()
  const settings = useSearchSettingsStore()
  const history = useSearchHistoryStore()
  const { theme, toggle } = useThemeStore()
  const [delayDraft, setDelayDraft] = useState(String(settings.searchOnTypeDebouncePeriod))
  const [limitDraft, setLimitDraft] = useState(String(settings.maxResults ?? ''))

  useEffect(() => {
    const dialog = dialogRef.current
    dialog?.showModal()
    return () => dialog?.close()
  }, [])
  useEffect(() => setDelayDraft(String(settings.searchOnTypeDebouncePeriod)), [settings.searchOnTypeDebouncePeriod])
  useEffect(() => setLimitDraft(String(settings.maxResults ?? '')), [settings.maxResults])

  const commitDelay = () => {
    settings.update({ searchOnTypeDebouncePeriod: delayDraft === '' ? DEFAULT_SEARCH_SETTINGS.searchOnTypeDebouncePeriod : Number(delayDraft) })
    setDelayDraft(String(useSearchSettingsStore.getState().searchOnTypeDebouncePeriod))
  }
  const commitLimit = () => {
    settings.update({ maxResults: limitDraft === '' ? null : Number(limitDraft) })
    setLimitDraft(String(useSearchSettingsStore.getState().maxResults ?? ''))
  }
  const close = () => {
    settings.update({
      searchOnTypeDebouncePeriod: delayDraft === '' ? DEFAULT_SEARCH_SETTINGS.searchOnTypeDebouncePeriod : Number(delayDraft),
      maxResults: limitDraft === '' ? null : Number(limitDraft),
    })
    usePreferencesStore.getState().close()
  }

  return (
    <dialog
      ref={dialogRef}
      aria-labelledby={titleId}
      aria-modal="true"
      className="fixed inset-0 z-50 m-auto max-h-[calc(100dvh-1.5rem)] w-[calc(100vw-1.5rem)] max-w-[34rem] overflow-hidden rounded-xl border border-border-subtle bg-surface-raised p-0 text-txt-primary shadow-xl backdrop:bg-black/40"
      onCancel={event => { event.preventDefault(); close() }}
      onMouseDown={event => { backdropPointerStarted.current = event.target === event.currentTarget }}
      onClick={event => {
        if (backdropPointerStarted.current && event.target === event.currentTarget) close()
        backdropPointerStarted.current = false
      }}
    >
      <div className="flex max-h-[calc(100dvh-1.5rem)] min-h-0 flex-col" onMouseDown={() => { backdropPointerStarted.current = false }} onClick={event => event.stopPropagation()}>
        <header className="flex flex-none items-center gap-3 border-b border-border-subtle px-5 py-4 sm:px-6">
          <span className="inline-flex h-9 w-9 flex-none items-center justify-center rounded-lg bg-accent/10 text-accent-ink"><Settings2 aria-hidden="true" className="h-4 w-4" /></span>
          <div className="min-w-0 flex-1">
            <h2 id={titleId} className="text-base font-semibold">Preferences</h2>
            <p className="mt-0.5 text-xs text-txt-tertiary">Appearance and search behavior.</p>
          </div>
          <button type="button" autoFocus aria-label="Close preferences" onClick={close} className="touch-target inline-flex h-11 w-11 flex-none items-center justify-center rounded-lg text-txt-tertiary transition-colors hover:bg-surface-overlay hover:text-txt-primary sm:h-8 sm:w-8">
            <X aria-hidden="true" className="h-4 w-4" />
          </button>
        </header>

        <div className="min-h-0 overflow-y-auto overscroll-contain px-5 py-5 sm:px-6">
          <section aria-labelledby={`${titleId}-appearance`}>
            <h3 id={`${titleId}-appearance`} className="text-xs font-semibold">Appearance</h3>
            <div className="mt-3 grid grid-cols-2 gap-2" role="group" aria-label="Color theme">
              {([{ value: 'light', label: 'Light', icon: Sun }, { value: 'dark', label: 'Dark', icon: Moon }] as const).map(({ value, label, icon: Icon }) => (
                <button key={value} type="button" aria-pressed={theme === value} onClick={() => { if (theme !== value) toggle() }}
                  className={clsx('touch-target flex min-h-11 items-center gap-2.5 rounded-lg border px-3 py-3 text-left text-sm transition-colors', theme === value ? 'border-accent/45 bg-accent/5 text-accent-ink' : 'border-border-subtle text-txt-secondary hover:bg-surface-overlay')}>
                  <Icon aria-hidden="true" className="h-4 w-4" />
                  <span className="flex-1 font-medium">{label}</span>
                  {theme === value && <Check aria-hidden="true" className="h-3.5 w-3.5" />}
                </button>
              ))}
            </div>
          </section>

          <section aria-labelledby={`${titleId}-search`} className="mt-5 border-t border-border-subtle pt-5">
            <h3 id={`${titleId}-search`} className="text-xs font-semibold">Search</h3>
            <label className="mt-3 flex min-h-11 cursor-pointer items-center gap-4">
              <span className="min-w-0 flex-1">
                <span className="block text-sm text-txt-primary">Search as you type</span>
                <span className="mt-0.5 block text-xs text-txt-tertiary">Turn off to search with Enter.</span>
              </span>
              <input type="checkbox" aria-label="Search as you type" checked={settings.searchOnType} onChange={event => settings.update({ searchOnType: event.target.checked })} className="h-4 w-4 flex-none cursor-pointer accent-accent" />
            </label>
            <details className="group mt-3 rounded-lg border border-border-subtle">
              <summary className="touch-target flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 rounded-lg px-3 py-2.5 text-xs font-medium text-txt-secondary transition-colors hover:bg-surface-overlay">
                Advanced search
                <ChevronDown aria-hidden="true" className="h-3.5 w-3.5 flex-none text-txt-tertiary transition-transform group-open:rotate-180" />
              </summary>
              <div className="space-y-4 border-t border-border-subtle px-3 py-4">
                <label className="flex flex-wrap items-center justify-between gap-2 text-xs text-txt-secondary">
                  <span>Typing delay (ms)</span>
                  <input type="number" min={0} step={50} value={delayDraft}
                    onChange={event => setDelayDraft(event.target.value)} onBlur={commitDelay}
                    onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur() }}
                    className="touch-input min-h-9 w-28 rounded-md border border-border bg-surface-overlay px-2.5 py-1.5 text-sm tabular-nums text-txt-primary" />
                </label>
                <label className="flex flex-wrap items-center justify-between gap-2 text-xs text-txt-secondary">
                  <span>Match limit (blank for unlimited)</span>
                  <input type="number" min={1} step={1} value={limitDraft}
                    onChange={event => setLimitDraft(event.target.value)} onBlur={commitLimit}
                    onKeyDown={event => { if (event.key === 'Enter') event.currentTarget.blur() }}
                    className="touch-input min-h-9 w-28 rounded-md border border-border bg-surface-overlay px-2.5 py-1.5 text-sm tabular-nums text-txt-primary" />
                </label>
              </div>
            </details>
          </section>

          <section aria-labelledby={`${titleId}-history`} className="mt-5 border-t border-border-subtle pt-5">
            <h3 id={`${titleId}-history`} className="text-xs font-semibold">History &amp; defaults</h3>
            <p className="mt-1 text-xs text-txt-tertiary">Shared by Manager and Monitor in this browser.</p>
            <div className="mt-3 flex flex-wrap gap-2">
              <button type="button" disabled={!history.items.length} onClick={history.clear} className="touch-target min-h-11 rounded-lg border border-border-subtle px-3 py-2 text-xs font-medium text-txt-secondary transition-colors hover:bg-surface-overlay disabled:cursor-not-allowed disabled:opacity-40 sm:min-h-9">Clear search history</button>
              <button type="button" onClick={() => {
                settings.update(DEFAULT_SEARCH_SETTINGS)
                setDelayDraft(String(DEFAULT_SEARCH_SETTINGS.searchOnTypeDebouncePeriod))
                setLimitDraft(String(DEFAULT_SEARCH_SETTINGS.maxResults ?? ''))
              }} className="touch-target min-h-11 rounded-lg px-3 py-2 text-xs font-medium text-txt-secondary transition-colors hover:bg-surface-overlay sm:min-h-9">Reset search settings</button>
            </div>
          </section>
        </div>

        <footer className="flex flex-none items-center justify-between gap-3 border-t border-border-subtle bg-surface-base/60 px-5 py-3 sm:px-6">
          <p className="text-xs text-txt-tertiary">Saved automatically in this browser.</p>
          <button type="button" onClick={close} className="touch-target min-h-11 flex-none rounded-lg border border-border px-4 py-2 text-xs font-medium transition-colors hover:bg-surface-overlay sm:min-h-9">Done</button>
        </footer>
      </div>
    </dialog>
  )
}
