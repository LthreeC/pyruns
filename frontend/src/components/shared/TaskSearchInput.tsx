import type { ComponentProps } from 'react'
import { ChevronDown } from 'lucide-react'
import clsx from 'clsx'
import type { TaskSearchOptions, TaskSearchScope } from '@/types'
import SearchInput from './SearchInput'

const SEARCH_FIELDS: { value: TaskSearchScope; label: string; description: string }[] = [
  { value: 'all', label: 'All fields', description: 'Names, notes, config, scripts, task env and full log files' },
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

export function taskSearchDescription(field: TaskSearchScope) {
  return SEARCH_FIELDS.find(option => option.value === field)!.description
}

interface Props extends Omit<ComponentProps<typeof SearchInput>, 'className' | 'placeholder'> {
  searchField: TaskSearchScope
  onSearchFieldChange: (field: TaskSearchScope) => void
  searchOptions: TaskSearchOptions
  onSearchOptionsChange: (options: TaskSearchOptions) => void
}

export default function TaskSearchInput({ searchField, onSearchFieldChange, searchOptions, onSearchOptionsChange, ...props }: Props) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5" onKeyDown={event => {
      if (!event.altKey || event.ctrlKey || event.metaKey || event.repeat) return
      const option = MATCH_OPTIONS.find(item => item.code === event.code)
      if (!option) return
      event.preventDefault()
      onSearchOptionsChange({ ...searchOptions, [option.key]: !searchOptions[option.key] })
    }}>
      <SearchInput
        {...props}
        className="min-w-0 flex-[1_1_12rem]"
        placeholder={searchOptions.useRegex ? 'Search with regex' : searchField === 'all' ? 'Search tasks and full logs' : `Search ${SEARCH_FIELDS.find(option => option.value === searchField)!.label.toLowerCase()}`}
      />
      <div className="flex flex-[1_0_auto] items-center justify-between gap-2">
        <div className="relative min-w-0">
          <select
            value={searchField}
            onChange={event => onSearchFieldChange(event.target.value as TaskSearchScope)}
            aria-label="Search field"
            title={taskSearchDescription(searchField)}
            className="touch-target h-11 max-w-28 appearance-none rounded-md border border-border-subtle bg-surface-overlay py-1.5 pl-2 pr-6 text-xs text-txt-primary outline-none transition-colors focus-visible:border-accent focus-visible:ring-2 focus-visible:ring-accent/20 sm:h-[34px]"
          >
            {SEARCH_FIELDS.map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
          <ChevronDown aria-hidden="true" className="pointer-events-none absolute right-1.5 top-1/2 h-3 w-3 -translate-y-1/2 text-txt-tertiary" />
        </div>
        <div className="flex flex-none gap-1" role="group" aria-label="Search match options">
          {MATCH_OPTIONS.map(option => <button
            key={option.key}
            type="button"
            aria-label={option.label}
            aria-pressed={searchOptions[option.key]}
            aria-keyshortcuts={option.shortcut}
            title={`${option.label} (${option.shortcut})${option.key === 'useRegex' ? ' — searches each line separately' : ''}`}
            onClick={() => onSearchOptionsChange({ ...searchOptions, [option.key]: !searchOptions[option.key] })}
            className={clsx('touch-target inline-flex h-11 w-11 items-center justify-center rounded-md border font-mono text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/30 sm:h-[34px] sm:w-8',
              searchOptions[option.key] ? 'border-accent/40 bg-accent/10 text-accent' : 'border-border-subtle bg-surface-overlay text-txt-secondary hover:bg-surface-hover',
              option.key === 'wholeWord' && 'underline underline-offset-4')}
          >{option.symbol}</button>)}
        </div>
      </div>
    </div>
  )
}
