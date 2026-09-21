import type { ComponentProps } from 'react'
import { ChevronDown, LoaderCircle, RefreshCw, Square } from 'lucide-react'
import clsx from 'clsx'
import type { TaskSearchOptions, TaskSearchScope, WorkspaceKind } from '@/types'
import SearchInput from './SearchInput'

const SEARCH_FIELDS: { value: TaskSearchScope; label: string; description: string }[] = [
  { value: 'all', label: 'All fields', description: 'Names, notes, config, task env and full log files' },
  { value: 'name', label: 'Task name', description: 'Task names only' },
  { value: 'notes', label: 'Notes', description: 'Task notes only' },
  { value: 'log', label: 'Logs', description: 'Full log files, including previous runs' },
  { value: 'env', label: 'Env', description: 'Task environment overrides: keys and values (KEY=value)' },
  { value: 'config', label: 'Config', description: 'Python task configuration keys and values' },
  { value: 'script', label: 'Shell script', description: 'Shell script content only' },
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
  ...props
}: Props) {
  const searchFields = SEARCH_FIELDS.filter(option => option.value !== (workspaceKind === 'shell' ? 'config' : 'script'))
  const searchActive = Boolean(props.value.trim())
  const refreshLabel = searching && searchActive ? 'Cancel search' : searchActive ? 'Refresh search results' : 'Refresh tasks'
  const matchOptions = (
    <div className="flex flex-none items-center gap-0.5" role="group" aria-label="Search match options">
      {MATCH_OPTIONS.map(option => <button
        key={option.key}
        type="button"
        aria-label={option.label}
        aria-pressed={searchOptions[option.key]}
        aria-keyshortcuts={option.shortcut}
        title={`${option.label} (${option.shortcut})${option.key === 'useRegex' ? ' — searches each line separately' : ''}`}
        onClick={() => onSearchOptionsChange({ ...searchOptions, [option.key]: !searchOptions[option.key] })}
        className={clsx('touch-target inline-flex h-11 w-11 items-center justify-center rounded border font-mono text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 sm:h-6 sm:w-6',
          searchOptions[option.key] ? 'border-accent/40 bg-accent/10 text-accent' : 'border-transparent text-txt-secondary hover:bg-surface-hover hover:text-txt-primary',
          option.key === 'wholeWord' && 'underline underline-offset-4')}
      >{option.symbol}</button>)}
    </div>
  )
  return (
    <div className={clsx('grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-1.5', !compact && 'sm:[@media(pointer:fine)]:grid-cols-[minmax(0,1fr)_auto_auto]')} onKeyDown={event => {
      if (!event.altKey || event.ctrlKey || event.metaKey || event.repeat) return
      const option = MATCH_OPTIONS.find(item => item.code === event.code)
      if (!option) return
      event.preventDefault()
      onSearchOptionsChange({ ...searchOptions, [option.key]: !searchOptions[option.key] })
    }}>
      <SearchInput
        {...props}
        className={clsx('col-start-1 row-start-1', compact && 'sm:[@media(pointer:fine)]:col-span-2')}
        placeholder={searchOptions.useRegex ? 'Search with regex' : searchField === 'all' ? 'Search tasks and full logs' : `Search ${SEARCH_FIELDS.find(option => option.value === searchField)!.label.toLowerCase()}`}
        trailingControls={<div className="mr-1 hidden flex-none sm:[@media(pointer:fine)]:block">{matchOptions}</div>}
      />
      <div className={clsx('col-span-2 row-start-2 flex min-w-0 flex-wrap items-center justify-between gap-1.5', compact ? 'sm:[@media(pointer:fine)]:col-span-1' : 'sm:[@media(pointer:fine)]:col-span-1 sm:[@media(pointer:fine)]:col-start-2 sm:[@media(pointer:fine)]:row-start-1')}>
        <div className="relative min-w-0">
          <select
            value={searchField}
            onChange={event => onSearchFieldChange(event.target.value as TaskSearchScope)}
            aria-label="Search field"
            title={taskSearchDescription(searchField, workspaceKind)}
            className="touch-target h-11 max-w-28 appearance-none rounded-md border border-border-subtle bg-surface-raised py-1.5 pl-2 pr-6 text-xs text-txt-secondary outline-none transition-colors hover:bg-surface-overlay focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/20 sm:h-9"
          >
            {searchFields.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
          <ChevronDown aria-hidden="true" className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
        </div>
        <div className="sm:[@media(pointer:fine)]:hidden">{matchOptions}</div>
      </div>
      <button
        type="button"
        aria-label={refreshLabel}
        title={refreshLabel}
        disabled={searching && !searchActive}
        onClick={() => searching && searchActive ? onCancel() : onRefresh()}
        className={clsx('touch-target col-start-2 row-start-1 inline-flex h-11 w-11 items-center justify-center rounded-md text-txt-secondary transition-colors hover:bg-surface-overlay hover:text-txt-primary focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 disabled:cursor-wait disabled:opacity-50 sm:h-9 sm:w-9', compact ? 'sm:[@media(pointer:fine)]:row-start-2' : 'sm:[@media(pointer:fine)]:col-start-3')}
      >
        {searching
          ? searchActive ? <Square aria-hidden="true" className="h-3.5 w-3.5" /> : <LoaderCircle aria-hidden="true" className="h-3.5 w-3.5 motion-safe:animate-spin" />
          : <RefreshCw aria-hidden="true" className="h-4 w-4" />}
      </button>
      {searchActive && <span className="sr-only" role="status">{searching ? 'Searching…' : taskSearchDescription(searchField, workspaceKind)}</span>}
    </div>
  )
}
