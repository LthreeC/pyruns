import type { ComponentProps } from 'react'
import { useEffect, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, LoaderCircle, RefreshCw, Settings2, SlidersHorizontal, Square } from 'lucide-react'
import clsx from 'clsx'
import type { TaskSearchOptions, TaskSearchScope, WorkspaceKind } from '@/types'
import SearchInput from './SearchInput'
import { usePreferencesStore, useSearchHistoryStore, useSearchSettingsStore } from '@/store'

const SEARCH_FIELDS: { value: TaskSearchScope; label: string; compactLabel: string; description: string }[] = [
  { value: 'all', label: 'All fields', compactLabel: 'All', description: 'Names, notes, config, task env and full log files' },
  { value: 'name', label: 'Task name', compactLabel: 'Name', description: 'Task names only' },
  { value: 'notes', label: 'Notes', compactLabel: 'Notes', description: 'Task notes only' },
  { value: 'log', label: 'Logs', compactLabel: 'Logs', description: 'Full log files, including previous runs' },
  { value: 'env', label: 'Env', compactLabel: 'Env', description: 'Task environment overrides: keys and values (KEY=value)' },
  { value: 'config', label: 'Config', compactLabel: 'Config', description: 'Python task configuration keys and values' },
  { value: 'script', label: 'Shell script', compactLabel: 'Script', description: 'Shell script content only' },
]

const MATCH_OPTIONS: { key: keyof TaskSearchOptions; symbol: string; label: string; shortcut: string; code: string }[] = [
  { key: 'matchCase', symbol: 'Aa', label: 'Match case', shortcut: 'Alt+C', code: 'KeyC' },
  { key: 'wholeWord', symbol: 'ab', label: 'Match whole word', shortcut: 'Alt+W', code: 'KeyW' },
  { key: 'useRegex', symbol: '.*', label: 'Use regular expression', shortcut: 'Alt+R', code: 'KeyR' },
]

function taskSearchDescription(field: TaskSearchScope, workspaceKind?: WorkspaceKind) {
  if (field === 'all' && workspaceKind === 'shell') {
    return 'Names, notes, shell scripts, task env and full log files'
  }
  return SEARCH_FIELDS.find(option => option.value === field)!.description
}

interface Props extends Omit<ComponentProps<typeof SearchInput>, 'className' | 'placeholder' | 'trailingControls'> {
  workspaceKind?: WorkspaceKind
  compact?: boolean
  searching: boolean
  onRefresh: () => void
  onCancel: () => void
  searchField: TaskSearchScope
  onSearchFieldChange: (field: TaskSearchScope) => void
  searchOptions: TaskSearchOptions
  onSearchOptionsChange: (options: TaskSearchOptions) => void
  limitHit?: boolean
  onFocusSearch?: () => void
}

export default function TaskSearchInput({
  workspaceKind,
  compact = false,
  searching,
  onRefresh,
  onCancel,
  searchField,
  onSearchFieldChange,
  searchOptions,
  onSearchOptionsChange,
  limitHit = false,
  onFocusSearch,
  ...props
}: Props) {
  const [filtersOpen, setFiltersOpen] = useState(false)
  const [filterPlacement, setFilterPlacement] = useState({ above: false, maxHeight: 480 })
  const settings = useSearchSettingsStore()
  const history = useSearchHistoryStore()
  const historyTimer = useRef<ReturnType<typeof setTimeout>>()
  const searchValueRef = useRef(props.value)
  searchValueRef.current = props.value
  const searchContainer = useRef<HTMLDivElement>(null)
  const focusSearchRef = useRef(onFocusSearch)
  focusSearchRef.current = onFocusSearch
  useEffect(() => () => clearTimeout(historyTimer.current), [])
  const handleSearchChange = (query: string) => {
    props.onChange(query)
    clearTimeout(historyTimer.current)
    // Only a search interaction adds history; restoring a view must not undo a clear.
    const revision = useSearchHistoryStore.getState().revision
    if (query) historyTimer.current = setTimeout(() => {
      if (searchValueRef.current === query && useSearchHistoryStore.getState().revision === revision) history.remember(query)
    }, 2000)
  }
  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || !(event.ctrlKey || event.metaKey)
        || !event.shiftKey || event.altKey || event.key.toLowerCase() !== 'f') return
      if (document.querySelector('dialog[open], [role="dialog"][aria-modal="true"]')) return
      const input = searchContainer.current?.querySelector('textarea')
      if (!input) return
      event.preventDefault()
      focusSearchRef.current?.()
      requestAnimationFrame(() => { input.focus(); input.select() })
    }
    window.addEventListener('keydown', focusSearch)
    return () => window.removeEventListener('keydown', focusSearch)
  }, [])
  const filterMenuRef = useRef<HTMLDivElement>(null)
  const filterTriggerRef = useRef<HTMLButtonElement>(null)
  const searchFields = SEARCH_FIELDS.filter(option => option.value !== (workspaceKind === 'shell' ? 'config' : 'script'))
  const selectedField = SEARCH_FIELDS.find(option => option.value === searchField) ?? SEARCH_FIELDS[0]
  const searchActive = Boolean(props.value)
  const refreshLabel = searching && searchActive ? 'Cancel search' : searchActive ? 'Refresh search results' : 'Refresh tasks'
  const matchOptions = (
    <div
      className="flex flex-none items-center border-l border-border-subtle"
      role="group"
      aria-label="Search match options"
    >
      {MATCH_OPTIONS.map(option => <button
        key={option.key}
        type="button"
        aria-label={option.label}
        aria-pressed={searchOptions[option.key]}
        aria-keyshortcuts={option.shortcut}
        title={`${option.label} (${option.shortcut})`}
        onClick={() => onSearchOptionsChange({ ...searchOptions, [option.key]: !searchOptions[option.key] })}
        className={clsx(
          'touch-target inline-flex h-11 w-11 items-center justify-center border-l border-border-subtle font-mono text-sm transition-colors first:border-l-0 focus-visible:z-10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 focus-visible:ring-inset sm:h-full sm:w-7',
          searchOptions[option.key]
            ? 'bg-accent/10 text-accent'
            : 'text-txt-secondary hover:bg-surface-hover hover:text-txt-primary',
          option.key === 'wholeWord' && 'underline underline-offset-4',
        )}
      >{option.symbol}</button>)}
    </div>
  )
  const fieldSelector = (
    <div className="task-search-field group relative h-full min-w-0 flex-none hover:bg-surface-hover">
      <select
        value={searchField}
        onChange={event => onSearchFieldChange(event.target.value as TaskSearchScope)}
        aria-label="Search field"
        title={taskSearchDescription(searchField, workspaceKind)}
        className="touch-target h-full w-full cursor-pointer appearance-none border-0 border-l border-border-subtle bg-transparent py-1.5 pl-2 pr-6 text-xs text-txt-secondary opacity-0 outline-none transition-colors focus-visible:ring-2 focus-visible:ring-accent/20 focus-visible:ring-inset"
      >
        {searchFields.map(option => <option key={option.value} value={option.value} className="text-txt-primary">{option.label}</option>)}
      </select>
      <span aria-hidden="true" className="pointer-events-none absolute inset-y-0 left-2 right-5 flex items-center overflow-hidden whitespace-nowrap text-xs text-txt-secondary group-focus-within:text-txt-primary">
        <span className="task-search-field-label-short">{selectedField.compactLabel}</span>
        <span className="task-search-field-label-full">{selectedField.label}</span>
      </span>
      <ChevronDown aria-hidden="true" className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
    </div>
  )
  const filterControls = (
    <div
      className="task-search-filter-controls flex h-full flex-none items-stretch"
      role="toolbar"
      aria-label="Search filters"
    >
      {fieldSelector}
      {matchOptions}
    </div>
  )

  useEffect(() => {
    if (!filtersOpen) return
    const updatePlacement = () => {
      const bounds = searchContainer.current?.getBoundingClientRect()
      if (!bounds) return
      const below = window.innerHeight - bounds.bottom - 16
      const above = bounds.top - 16
      const openAbove = below < 320 && above > below
      setFilterPlacement({ above: openAbove, maxHeight: Math.max(0, openAbove ? above : below) })
    }
    updatePlacement()
    const handlePointerDown = (event: PointerEvent) => {
      if (!filterMenuRef.current?.contains(event.target as Node)) setFiltersOpen(false)
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setFiltersOpen(false)
    }
    document.addEventListener('pointerdown', handlePointerDown)
    document.addEventListener('keydown', handleKeyDown)
    window.addEventListener('resize', updatePlacement)
    document.addEventListener('scroll', updatePlacement, true)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown)
      document.removeEventListener('keydown', handleKeyDown)
      window.removeEventListener('resize', updatePlacement)
      document.removeEventListener('scroll', updatePlacement, true)
    }
  }, [filtersOpen])

  return (
    <>
    <div ref={searchContainer} className="task-search-combined flex min-w-0 items-center gap-1.5" onKeyDown={event => {
      if (event.nativeEvent.isComposing) return
      if (!event.altKey || event.ctrlKey || event.metaKey || event.repeat) return
      const option = MATCH_OPTIONS.find(item => item.code === event.code)
      if (!option) return
      event.preventDefault()
      onSearchOptionsChange({ ...searchOptions, [option.key]: !searchOptions[option.key] })
    }}>
      <div className="task-search-control-row relative flex min-w-0 flex-1 items-center gap-1.5">
        <SearchInput
          {...props}
          onChange={handleSearchChange}
          ariaKeyShortcuts="Control+Shift+F Meta+Shift+F"
          history={history.items}
          historyRevision={history.revision}
          onRemember={history.remember}
          debounceMs={settings.searchOnTypeDebouncePeriod}
          searchOnType={settings.searchOnType}
          onSubmit={onRefresh}
          onCancel={onCancel}
          className="w-full flex-1"
          placeholder={searchOptions.useRegex
            ? 'Search with regex'
            : searchField === 'all'
              ? compact ? 'Search tasks' : 'Search tasks and full logs'
              : `Search ${selectedField.label.toLowerCase()}`}
          trailingControls={<div style={{ display: filtersOpen ? 'none' : undefined }} className="task-search-inline-filters flex h-full flex-none items-stretch">{filterControls}</div>}
        />
        <div ref={filterMenuRef} className="task-search-filter-menu flex-none">
          <button
            ref={filterTriggerRef}
            type="button"
            aria-label="Search filters"
            aria-expanded={filtersOpen}
            aria-haspopup="true"
            title="Search filters"
            onClick={() => setFiltersOpen(open => !open)}
            className="task-search-filter-trigger touch-target inline-flex h-11 w-11 items-center justify-center rounded-md border border-border bg-surface-overlay text-txt-secondary transition-colors hover:bg-surface-hover hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 sm:h-9 sm:w-9"
          >
            <SlidersHorizontal aria-hidden="true" className="h-4 w-4" />
          </button>
          {filtersOpen && (
            <div
              style={{ maxHeight: filterPlacement.maxHeight, ...(filterPlacement.above ? { top: 'auto', bottom: 'calc(100% + 0.5rem)' } : {}) }}
              className="task-search-filter-popover absolute right-0 top-[calc(100%+0.5rem)] z-30 min-w-52 overflow-y-auto overscroll-contain rounded-md border border-border bg-surface-raised p-1.5 shadow-md"
              role="dialog"
              aria-label="Search filters"
            >
              {filterControls}
              <div className="mt-1.5 border-t border-border-subtle pt-1.5">
                <button type="button" onClick={() => {
                  setFiltersOpen(false)
                  usePreferencesStore.getState().open(filterTriggerRef.current)
                }} className="touch-target flex min-h-11 w-full items-center gap-2 rounded-md px-2 text-left text-xs text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary sm:min-h-9">
                  <Settings2 aria-hidden="true" className="h-3.5 w-3.5" />
                  <span className="flex-1">Search preferences…</span>
                  <ChevronRight aria-hidden="true" className="h-3.5 w-3.5 text-txt-tertiary" />
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
      {!compact && (
        <button
          type="button"
          aria-label={refreshLabel}
          title={refreshLabel}
          disabled={searching && !searchActive}
          onClick={() => searching && searchActive ? onCancel() : onRefresh()}
          className="touch-target inline-flex h-11 w-11 flex-none items-center justify-center rounded-md text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 disabled:cursor-wait disabled:opacity-50 sm:h-9 sm:w-9"
        >
          {searching
            ? searchActive ? <Square aria-hidden="true" className="h-3.5 w-3.5" /> : <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 motion-safe:animate-spin" />
            : <RefreshCw aria-hidden="true" className="h-4 w-4" />}
        </button>
      )}
      {searchActive && <span className="sr-only" role="status">{searching ? 'Searching…' : taskSearchDescription(searchField, workspaceKind)}</span>}
    </div>
    {limitHit && <p role="status" className="mt-2 text-xs text-txt-secondary">
      Match limit reached. More results may exist.{' '}
      <button type="button" className="text-accent hover:underline" onClick={() => settings.update({ maxResults: null })}>Search without limit</button>
    </p>}
    </>
  )
}
