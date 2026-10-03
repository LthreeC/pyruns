import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as api from './api'
import {
  appendMonitorLogContent,
  confirmDiscardWorkspaceChanges,
  getUnsavedWorkspaceChangeLabels,
  monitorLogAppendDelta,
  replaceMonitorLogContent,
  trimMonitorLogContent,
  useConfirmationStore,
  useDashboardStore,
  useGeneratorStore,
  useLauncherStore,
  useMonitorStore,
  useRuntimeStore,
  useSearchHistoryStore,
  useTaskDetailDraftStore,
  useTaskStore,
  useThemeStore,
  useWorkspaceStore,
} from './store'

vi.mock('./api', () => ({
  setWorkspaceContext: vi.fn(),
  getTasks: vi.fn(),
  getTaskLogs: vi.fn(),
  getDashboard: vi.fn(),
  getTemplates: vi.fn(),
  getTemplateContent: vi.fn(),
  openLauncherWorkspace: vi.fn(),
}))

function workspace(runRoot: string) {
  return {
    run_root: runRoot,
    working_root: runRoot,
    workspace_kind: 'shell' as const,
    workspace_ready: true,
  } as any
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(next => { resolve = next })
  return { promise, resolve }
}

describe('monitor log text coordinates', () => {
  const empty = { logContent: '', logTextVersion: 0, logTextEnd: 0 }

  it('appends repeated text even when trimming leaves an identical full window', () => {
    const row = 'same line '.padEnd(127, 'x') + '\n'
    const previous = replaceMonitorLogContent(empty, trimMonitorLogContent(row.repeat(40_000)))
    expect(previous.logContent).toHaveLength(4 * 1024 * 1024)
    const next = appendMonitorLogContent(previous, row)
    expect(next.logContent).toBe(previous.logContent)
    expect(monitorLogAppendDelta(previous, next)).toBe(row)
  })

  it('keeps exact ANSI and UTF-16 fragments across a full character window', () => {
    const previous = replaceMonitorLogContent(empty, 'x'.repeat(4 * 1024 * 1024))
    const chunks = ['\x1b[', '32m中文\ud83d', '\ude80\x1b[0m\n', 'x'.repeat(4096)]
    const next = appendMonitorLogContent(previous, chunks)
    expect(next.logContent).toHaveLength(4 * 1024 * 1024)
    expect(monitorLogAppendDelta(previous, next)).toBe(chunks.join(''))
    expect(next.logTextEnd - previous.logTextEnd).toBe(chunks.join('').length)
  })

  it('deduplicates lifecycle frames within a batch while retaining ordinary repeated lines', () => {
    const start = '[PYRUNS] START task\r\n'
    const next = appendMonitorLogContent(empty, [start, start.replace(/\r\n/g, '\n'), 'repeat\n', 'repeat\n'])
    expect(next.logContent).toBe(`${start}repeat\nrepeat\n`)
    expect(monitorLogAppendDelta(empty, next)).toBe(next.logContent)
    const unchanged = appendMonitorLogContent(next, [])
    expect(monitorLogAppendDelta(next, unchanged)).toBe('')
  })

  it('replaces the terminal after a reset or after an unseen append exceeds the retained window', () => {
    const previous = replaceMonitorLogContent(empty, 'old log\n')
    const reset = appendMonitorLogContent(replaceMonitorLogContent(previous, 'new log\n'), 'more\n')
    expect(monitorLogAppendDelta(previous, reset)).toBeNull()
    const overflow = appendMonitorLogContent(previous, 'z'.repeat(4 * 1024 * 1024 + 1))
    expect(overflow.logContent).toHaveLength(4 * 1024 * 1024)
    expect(monitorLogAppendDelta(previous, overflow)).toBeNull()
  })
})

it('bounds and deduplicates search history while preserving literal whitespace', () => {
  const setItem = vi.fn()
  vi.stubGlobal('window', { localStorage: { getItem: vi.fn(), setItem } })
  try {
    useSearchHistoryStore.setState({ items: [] })
    for (let index = 0; index < 102; index++) useSearchHistoryStore.getState().remember(String(index))
    useSearchHistoryStore.getState().remember('100')
    useSearchHistoryStore.getState().remember('  \n ')
    useSearchHistoryStore.getState().remember('')
    const items = useSearchHistoryStore.getState().items
    expect(items).toHaveLength(100)
    expect(items.slice(0, 3)).toEqual(['  \n ', '100', '101'])
    expect(new Set(items).size).toBe(100)
    expect(JSON.parse(setItem.mock.calls.at(-1)![1])).toEqual(items)
    useSearchHistoryStore.getState().clear()
    expect(useSearchHistoryStore.getState().items).toEqual([])
    expect(setItem).toHaveBeenLastCalledWith('pyruns_search_history', '[]')
  } finally { vi.unstubAllGlobals() }
})

describe('workspace-scoped stores', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    useWorkspaceStore.getState().setWorkspace(null)
    useRuntimeStore.setState({ dirty: false })
    useTaskStore.setState({ sortMode: 'priority' })
    useGeneratorStore.setState({
      selectedTemplate: '',
      templateContent: null,
      yamlText: '',
      shellText: '',
      namePrefix: 'task',
      appendTimestamp: true,
      dirty: false,
    })
    useTaskDetailDraftStore.getState().clear()
    useConfirmationStore.getState().respond(false)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('opens bounded search context and returns to the latest task log', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const match = { field: 'log' as const, log_file: 'run1.log', offset: 5_000_000, log_identity: 'old', snippet: 'needle', match_start: 0, match_end: 6, location: 'run1.log:42' }
    vi.mocked(api.getTaskLogs).mockResolvedValueOnce({ content: 'needle\n', offset: 5_000_007, selected_log: 'run1.log', available_logs: ['run1.log', 'run2.log'], log_identity: 'old' } as any)
    await useMonitorStore.getState().selectTask('alpha', match)
    expect(api.getTaskLogs).toHaveBeenLastCalledWith('alpha', {
      logFileName: 'run1.log', logIdentity: 'old', offset: 5_000_000, chunkSize: 32768,
    }, expect.any(AbortSignal))
    expect(useMonitorStore.getState()).toMatchObject({ selectedTaskName: 'alpha', selectedLog: 'run1.log', logMatch: match, logContent: 'needle\n', loading: false })
    vi.mocked(api.getTaskLogs).mockResolvedValueOnce({ content: 'latest\n', offset: 7, selected_log: 'run2.log', available_logs: ['run1.log', 'run2.log'] } as any)
    await useMonitorStore.getState().selectTask('alpha')
    expect(useMonitorStore.getState()).toMatchObject({ selectedLog: 'run2.log', logMatch: null, logContent: 'latest\n' })
  })

  it.each(['selection', 'workspace'])('discards obsolete log context after changing %s', async change => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const stale = deferred<any>()
    vi.mocked(api.getTaskLogs).mockReturnValueOnce(stale.promise)
    const pending = useMonitorStore.getState().selectTask('alpha', { field: 'log', log_file: 'run1.log', snippet: 'needle', match_start: 0, match_end: 6, location: 'run1.log:42' })
    const signal = vi.mocked(api.getTaskLogs).mock.calls[0][2]!
    if (change === 'workspace') useWorkspaceStore.getState().setWorkspace(workspace('B'))
    else {
      vi.mocked(api.getTaskLogs).mockResolvedValueOnce({ content: 'beta', offset: 4, selected_log: 'run2.log', available_logs: ['run2.log'] } as any)
      await useMonitorStore.getState().selectTask('beta')
    }
    expect(signal.aborted).toBe(true)
    stale.resolve({ content: 'obsolete', offset: 8, selected_log: 'run1.log', available_logs: ['run1.log'] })
    await pending
    expect(useMonitorStore.getState()).toMatchObject({ logContent: change === 'workspace' ? '' : 'beta', logMatch: null, loading: false, logError: '' })
  })

  it.each([{ reset: true }, { available_logs: [] }, { selected_log: 'run2.log' }])('rejects stale search locations: %j', async changed => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    vi.mocked(api.getTaskLogs).mockResolvedValueOnce({ content: 'wrong context', selected_log: 'run1.log', available_logs: ['run1.log'], ...changed } as any)
    await expect(useMonitorStore.getState().selectTask('alpha', { field: 'log', log_file: 'run1.log', snippet: 'needle', match_start: 0, match_end: 6, location: 'run1.log:42' })).rejects.toThrow('This log changed')
    expect(useMonitorStore.getState()).toMatchObject({ logContent: '', loading: false, logError: expect.stringContaining('This log changed') })
  })

  it('cancels obsolete historical log reads when the log or workspace changes', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    useMonitorStore.setState({ selectedTaskName: 'alpha' })
    vi.mocked(api.getTaskLogs).mockImplementation((_name, _options, signal) => new Promise((_resolve, reject) => {
      signal?.addEventListener('abort', () => reject(signal.reason), { once: true })
    }))

    const first = useMonitorStore.getState().selectLogFile('run1.log')
    const firstSignal = vi.mocked(api.getTaskLogs).mock.calls[0][2]!
    const second = useMonitorStore.getState().selectLogFile('run2.log')
    const secondSignal = vi.mocked(api.getTaskLogs).mock.calls[1][2]!
    expect(firstSignal.aborted).toBe(true)
    await first
    expect(useMonitorStore.getState()).toMatchObject({ selectedLog: 'run2.log', loading: true, logError: '' })

    useWorkspaceStore.getState().setWorkspace(workspace('B'))
    expect(secondSignal.aborted).toBe(true)
    await second
    expect(useMonitorStore.getState()).toMatchObject({ selectedTaskName: null, logContent: '', loading: false, logError: '' })
  })

  it.each(['timeout', 'failure'])('shows historical log %s and recovers on retry', async failure => {
    vi.useFakeTimers()
    try {
      useWorkspaceStore.getState().setWorkspace(workspace('A'))
      useMonitorStore.setState({ selectedTaskName: 'alpha' })
      vi.mocked(api.getTaskLogs).mockImplementationOnce((_name, _options, signal) => new Promise((_resolve, reject) => {
        if (failure === 'failure') reject(new Error('Log is unavailable'))
        else signal?.addEventListener('abort', () => reject(signal.reason), { once: true })
      }))
      const pending = useMonitorStore.getState().selectLogFile('run1.log')
      const message = failure === 'timeout' ? 'Log loading timed out. Please retry.' : 'Log is unavailable'
      const rejection = expect(pending).rejects.toThrow(message)
      if (failure === 'timeout') await vi.advanceTimersByTimeAsync(10_000)
      await rejection
      expect(useMonitorStore.getState()).toMatchObject({ logContent: '', loading: false, logError: message })

      vi.mocked(api.getTaskLogs).mockResolvedValueOnce({ content: 'recovered', offset: 9, selected_log: 'run1.log', available_logs: ['run1.log'] } as any)
      await useMonitorStore.getState().selectLogFile('run1.log')
      expect(useMonitorStore.getState()).toMatchObject({ logContent: 'recovered', loading: false, logError: '' })
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('ignores a task response from the workspace that was replaced', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const request = deferred<any>()
    vi.mocked(api.getTasks).mockReturnValueOnce(request.promise)

    const pending = useTaskStore.getState().fetchTasks()
    useWorkspaceStore.getState().setWorkspace(workspace('B'))
    request.resolve({
      items: [{ name: 'from-a', status: 'completed' }],
      total: 1,
      has_more: false,
      status_counts: null,
    })
    await pending

    expect(useTaskStore.getState().tasks).toEqual([])
    expect(useTaskStore.getState().monitorWorkspaceKey).toBe('B')
  })

  it('preserves dashboard data and exposes a visible error after refresh failure', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const snapshot = { summary: { total: 3 }, recent_tasks: [] } as any
    useDashboardStore.setState({ data: snapshot })
    vi.mocked(api.getDashboard).mockRejectedValueOnce(new Error('offline'))

    await expect(useDashboardStore.getState().fetch()).rejects.toThrow('offline')

    expect(useDashboardStore.getState().data).toBe(snapshot)
    expect(useDashboardStore.getState().error).toBe('offline')
  })

  it('preserves manager results and selection after refresh failure', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const task = { name: 'kept', status: 'completed' } as any
    useTaskStore.setState({
      tasks: [task],
      total: 1,
      statusCounts: { completed: 1 } as any,
      selectedIds: new Set(['kept']),
    })
    vi.mocked(api.getTasks).mockRejectedValueOnce(new Error('offline'))

    await useTaskStore.getState().fetchTasks()

    expect(useTaskStore.getState().tasks).toEqual([task])
    expect(useTaskStore.getState().total).toBe(1)
    expect(useTaskStore.getState().selectedIds).toEqual(new Set(['kept']))
    expect(useTaskStore.getState().error).toBe('offline')
  })

  it('uses and persists the selected Manager card order', async () => {
    const setItem = vi.fn()
    vi.stubGlobal('window', { localStorage: { getItem: vi.fn(), setItem } })
    vi.mocked(api.getTasks).mockResolvedValueOnce({
      items: [],
      total: 0,
      offset: 0,
      limit: 50,
      has_more: false,
    })

    useTaskStore.getState().setSortMode('activity_asc')
    await useTaskStore.getState().fetchTasks()

    expect(setItem).toHaveBeenCalledWith('pyruns_manager_sort', 'activity_asc')
    expect(api.getTasks).toHaveBeenCalledWith(expect.objectContaining({ sort: 'activity_asc' }), undefined)
    expect(useTaskStore.getState().sortMode).toBe('activity_asc')
  })

  it.each(['manager', 'monitor'] as const)('retains unchanged %s rows across refreshes, reordering, and deletion', async view => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const items = [
      { name: 'alpha', status: 'completed', config: { model: 'small', layers: [1, 2] }, notes: 'old' },
      { name: 'beta', status: 'running', config: {}, notes: '' },
    ]
    const fetch = () => view === 'manager'
      ? useTaskStore.getState().fetchTasks({ background: true })
      : useTaskStore.getState().fetchMonitorTasks({ background: true })
    const rows = () => view === 'manager' ? useTaskStore.getState().tasks : useTaskStore.getState().monitorTasks
    const respond = (next: unknown[]) => vi.mocked(api.getTasks).mockResolvedValueOnce({ items: next, total: next.length, has_more: false } as any)
    respond(items)
    await fetch()
    const original = rows()
    respond(JSON.parse(JSON.stringify(items)))
    await fetch()
    expect(rows()).toBe(original)

    // Reordering JSON keys does not change the data. Nested edits and removed
    // fields must reach consumers even when the displayed name/status match.
    const changed = { name: 'alpha', status: 'completed', config: { layers: [1, 3], model: 'small' } }
    respond([{ config: {}, notes: '', status: 'running', name: 'beta' }, changed, { name: 'gamma' }])
    await fetch()
    expect(rows().map(task => task.name)).toEqual(['beta', 'alpha', 'gamma'])
    expect(rows()[0]).toBe(original[1])
    expect(rows()[1]).toEqual(changed)
    expect(rows()[1]).not.toBe(original[0])
    expect(rows()[1]).not.toHaveProperty('notes')
    const reordered = rows()
    respond([{ ...changed }])
    await fetch()
    expect(rows()).toHaveLength(1)
    expect(rows()[0]).toBe(reordered[1])
  })

  it.each(['manager', 'monitor'] as const)('retains unchanged %s search rows while updating nested match locations', async view => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const response = deferred<any>()
    vi.mocked(api.getTasks).mockReturnValueOnce(response.promise)
    if (view === 'manager') useTaskStore.getState().setQuery('needle')
    const pending = view === 'manager'
      ? useTaskStore.getState().fetchTasks()
      : useTaskStore.getState().fetchMonitorTasks({ query: 'needle' })
    const rows = () => view === 'manager' ? useTaskStore.getState().tasks : useTaskStore.getState().monitorTasks
    const progress = vi.mocked(api.getTasks).mock.calls[0][0]?.onProgress!
    const page = { items: [{ name: 'alpha', search_matches: [{ field: 'log', offset: 10, snippet: 'needle' }] }], total: 1, has_more: false } as any
    progress(page)
    const initial = rows()
    progress(JSON.parse(JSON.stringify(page)))
    expect(rows()).toBe(initial)
    const updated = JSON.parse(JSON.stringify(page))
    updated.items[0].search_matches[0].offset = 20
    progress(updated)
    expect(rows()[0]).not.toBe(initial[0])
    expect(rows()[0].search_matches![0].offset).toBe(20)
    const latest = rows()
    response.resolve(JSON.parse(JSON.stringify(updated)))
    await pending
    expect(rows()).toBe(latest)
  })

  it.each(['manager', 'monitor'] as const)('cancels superseded %s log searches and ignores their results', async view => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const old = deferred<any>()
    const latest = deferred<any>()
    vi.mocked(api.getTasks).mockReturnValueOnce(old.promise).mockReturnValueOnce(latest.promise)
    const fetch = (query: string) => {
      if (view === 'monitor') return useTaskStore.getState().fetchMonitorTasks({ query })
      useTaskStore.getState().setQuery(query)
      return useTaskStore.getState().fetchTasks()
    }
    const pendingOld = fetch('old')
    const oldSignal = vi.mocked(api.getTasks).mock.calls[0][1]!
    expect(oldSignal.aborted).toBe(false)
    const pendingLatest = fetch('latest')
    expect(oldSignal.aborted).toBe(true)
    const progress = vi.mocked(api.getTasks).mock.calls[1][0]?.onProgress!
    progress({ items: [{ name: 'partial' }], total: 1, has_more: false } as any)
    expect(view === 'monitor' ? useTaskStore.getState().monitorTasks : useTaskStore.getState().tasks).toEqual([{ name: 'partial' }])
    vi.mocked(api.getTasks).mock.calls[0][0]?.onProgress?.({ items: [{ name: 'stale' }], total: 1 } as any)
    expect(view === 'monitor' ? useTaskStore.getState().monitorTasks : useTaskStore.getState().tasks).toEqual([{ name: 'partial' }])
    latest.resolve({ items: [{ name: 'latest' }], total: 1, has_more: false, search_limit_hit: true })
    await pendingLatest
    old.resolve({ items: [{ name: 'old' }], total: 1, has_more: false })
    await pendingOld
    expect(view === 'monitor' ? useTaskStore.getState().monitorTasks : useTaskStore.getState().tasks).toEqual([{ name: 'latest' }])
    expect(view === 'monitor' ? useTaskStore.getState().monitorSearchLimitHit : useTaskStore.getState().searchLimitHit).toBe(true)
  })

  it.each(['manager', 'monitor'] as const)('does not let %s polling cancel a foreground search', async view => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const foreground = deferred<any>()
    vi.mocked(api.getTasks).mockReturnValueOnce(foreground.promise)

    if (view === 'manager') useTaskStore.getState().setQuery('needle')
    const pendingForeground = view === 'monitor'
      ? useTaskStore.getState().fetchMonitorTasks({ query: 'needle', forceRefresh: true })
      : useTaskStore.getState().fetchTasks({ forceRefresh: true })
    expect(view === 'monitor' ? useTaskStore.getState().monitorLoading : useTaskStore.getState().loading).toBe(true)
    const signal = vi.mocked(api.getTasks).mock.calls[0][1]!
    const pendingBackground = view === 'monitor'
      ? useTaskStore.getState().fetchMonitorTasks({ query: 'needle', background: true })
      : useTaskStore.getState().fetchTasks({ background: true })
    await pendingBackground
    expect(signal.aborted).toBe(false)
    expect(api.getTasks).toHaveBeenCalledTimes(1)

    foreground.resolve({ items: [{ name: 'fresh' }], total: 1, has_more: false })
    await pendingForeground
    expect(view === 'monitor' ? useTaskStore.getState().monitorTasks : useTaskStore.getState().tasks).toEqual([{ name: 'fresh' }])
    expect(view === 'monitor' ? useTaskStore.getState().monitorLoading : useTaskStore.getState().loading).toBe(false)
    if (view === 'monitor') await useTaskStore.getState().fetchMonitorTasks({ background: true })
    else await useTaskStore.getState().fetchTasks({ background: true })
    expect(api.getTasks).toHaveBeenCalledTimes(1)
  })

  it('keeps Manager controls available while a background refresh is pending', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const pendingResponse = deferred<any>()
    vi.mocked(api.getTasks).mockReturnValueOnce(pendingResponse.promise)

    const pending = useTaskStore.getState().fetchTasks({ background: true })
    expect(useTaskStore.getState().loading).toBe(false)

    pendingResponse.resolve({ items: [{ name: 'fresh' }], total: 1, has_more: false })
    await pending
    expect(useTaskStore.getState().tasks).toEqual([{ name: 'fresh' }])
    expect(useTaskStore.getState().loading).toBe(false)
  })

  it.each([
    ['manager', 'field'], ['monitor', 'field'], ['manager', 'options'], ['monitor', 'options'],
  ])('changing %s search %s aborts stale results and resets pagination', async (view, change) => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const old = deferred<any>()
    vi.mocked(api.getTasks).mockReturnValueOnce(old.promise)
    useTaskStore.setState({ query: 'needle', monitorQuery: 'needle', offset: 50, monitorOffset: 600 })
    const fetch = () => view === 'manager' ? useTaskStore.getState().fetchTasks() : useTaskStore.getState().fetchMonitorTasks()
    const pending = fetch()
    const signal = vi.mocked(api.getTasks).mock.calls[0][1]!
    expect(vi.mocked(api.getTasks).mock.calls[0][0]?.searchField).toBe('all')
    const options = { matchCase: true, wholeWord: true, useRegex: true }
    if (change === 'field') {
      if (view === 'manager') useTaskStore.getState().setSearchField('notes')
      else useTaskStore.getState().setMonitorSearchField('notes')
    } else {
      if (view === 'manager') useTaskStore.getState().setSearchOptions(options)
      else useTaskStore.getState().setMonitorSearchOptions(options)
    }
    expect(signal.aborted).toBe(true)
    vi.mocked(api.getTasks).mockResolvedValue({ items: [{ name: 'notes-result' }], total: 1, has_more: false } as any)
    await fetch()
    const expected = change === 'field' ? { searchField: 'notes' } : { searchOptions: options }
    expect(api.getTasks).toHaveBeenLastCalledWith(expect.objectContaining({ ...expected, offset: 0, ...(view === 'monitor' ? { limit: 200 } : {}) }), expect.any(AbortSignal))
    old.resolve({ items: [{ name: 'stale-log-result' }], total: 1, has_more: false })
    await pending
    expect(view === 'manager' ? useTaskStore.getState().tasks : useTaskStore.getState().monitorTasks).toEqual([{ name: 'notes-result' }])
    if (view === 'monitor') {
      await useTaskStore.getState().fetchMonitorTasks({ offset: 200 })
      expect(api.getTasks).toHaveBeenLastCalledWith(expect.objectContaining({ ...expected, limit: 200, offset: 200 }), expect.any(AbortSignal))
    }
    useWorkspaceStore.getState().setWorkspace(workspace('B'))
    expect(useTaskStore.getState()).toMatchObject({ searchField: 'all', monitorSearchField: 'all' })
    expect(useTaskStore.getState().searchOptions).toEqual({ matchCase: false, wholeWord: false, useRegex: false })
    expect(useTaskStore.getState().monitorSearchOptions).toEqual({ matchCase: false, wholeWord: false, useRegex: false })
  })

  it('browses monitor tasks beyond 10000 with bounded pages and resets a changed query', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    vi.mocked(api.getTasks).mockImplementation(async params => {
      if ((params?.limit ?? 0) > 10_000) throw Object.assign(new Error('Page too large'), { status: 422 })
      return { items: [{ name: params?.query ? 'match' : 'task-10000' }], total: params?.query ? 1 : 10_001 } as any
    })
    await useTaskStore.getState().fetchMonitorTasks({ offset: 10_000 })
    expect(useTaskStore.getState()).toMatchObject({ monitorOffset: 10_000, monitorTasks: [{ name: 'task-10000' }] })
    expect(api.getTasks).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 10_000, limit: 200 }), expect.any(AbortSignal))
    await useTaskStore.getState().fetchMonitorTasks({ query: 'needle' })
    expect(useTaskStore.getState()).toMatchObject({ monitorOffset: 0, monitorTasks: [{ name: 'match' }] })
    expect(api.getTasks).toHaveBeenLastCalledWith(expect.objectContaining({ offset: 0, limit: 200 }), expect.any(AbortSignal))
  })

  it.each([0, 401])('returns an emptied last monitor page to the remaining %i tasks', async total => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    useTaskStore.setState({ monitorOffset: 600 })
    vi.mocked(api.getTasks).mockResolvedValueOnce({ items: [], total } as any)
      .mockResolvedValueOnce({ items: [{ name: 'last-remaining' }], total } as any)
    await useTaskStore.getState().fetchMonitorTasks()
    expect(useTaskStore.getState()).toMatchObject({ monitorOffset: total ? 400 : 0,
      monitorTasks: total ? [{ name: 'last-remaining' }] : [] })
    expect(api.getTasks).toHaveBeenCalledTimes(total ? 2 : 1)
  })

  it('keeps the latest monitor page when an obsolete request or off-page task update arrives', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const old = deferred<any>()
    vi.mocked(api.getTasks).mockReturnValueOnce(old.promise)
    const pending = useTaskStore.getState().fetchMonitorTasks({ offset: 10_000 })
    const signal = vi.mocked(api.getTasks).mock.calls[0][1]!
    vi.mocked(api.getTasks).mockResolvedValueOnce({ items: [{ name: 'second-page' }], total: 10_001 } as any)
    await useTaskStore.getState().fetchMonitorTasks({ offset: 200 })
    expect(signal.aborted).toBe(true)
    old.resolve({ items: [{ name: 'obsolete-page' }], total: 10_001 })
    await pending
    useTaskStore.getState().updateMonitorTask({ name: 'off-page', status: 'running' } as any)
    expect(useTaskStore.getState()).toMatchObject({ monitorOffset: 200, monitorTasks: [{ name: 'second-page' }] })
  })

  it('preserves the current generator draft after a template load failure', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    useGeneratorStore.setState({
      selectedTemplate: 'old.yaml',
      templateContent: { content: 'old: true' } as any,
      yamlText: 'old: true',
      shellText: 'echo old',
    })
    vi.mocked(api.getTemplateContent).mockRejectedValueOnce(new Error('unreadable'))

    await expect(useGeneratorStore.getState().loadTemplate('new.yaml')).rejects.toThrow('unreadable')

    expect(useGeneratorStore.getState().selectedTemplate).toBe('old.yaml')
    expect(useGeneratorStore.getState().templateContent).toEqual({ content: 'old: true' })
    expect(useGeneratorStore.getState().yamlText).toBe('old: true')
    expect(useGeneratorStore.getState().shellText).toBe('echo old')
  })

  it('tracks generator edits and clears dirty state after loading a template', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))

    useGeneratorStore.getState().setYamlText('value: 2')
    expect(useGeneratorStore.getState().dirty).toBe(true)

    vi.mocked(api.getTemplateContent).mockResolvedValueOnce({
      value: 'fresh.yaml',
      label: 'fresh.yaml',
      path: '/fresh.yaml',
      content: 'value: 1',
      parsed_config: { value: 1 },
      read_only: false,
      mode_hint: 'yaml',
    })
    await useGeneratorStore.getState().loadTemplate('fresh.yaml')

    expect(useGeneratorStore.getState().yamlText).toBe('value: 1')
    expect(useGeneratorStore.getState().dirty).toBe(false)
  })

  it('does not overwrite generator edits made while a template is loading', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    useGeneratorStore.setState({
      selectedTemplate: 'old.yaml',
      templateContent: { content: 'old: true' } as any,
      yamlText: 'old: true',
      namePrefix: 'old-name',
      dirty: false,
    })
    const request = deferred<any>()
    vi.mocked(api.getTemplateContent).mockReturnValueOnce(request.promise)

    const pending = useGeneratorStore.getState().loadTemplate('fresh.yaml')
    useGeneratorStore.getState().setNamePrefix('edited-while-loading')
    request.resolve({
      value: 'fresh.yaml',
      label: 'fresh.yaml',
      path: '/fresh.yaml',
      content: 'fresh: true',
      parsed_config: { fresh: true },
      read_only: false,
      mode_hint: 'yaml',
    })
    await pending

    expect(useGeneratorStore.getState().selectedTemplate).toBe('old.yaml')
    expect(useGeneratorStore.getState().yamlText).toBe('old: true')
    expect(useGeneratorStore.getState().namePrefix).toBe('edited-while-loading')
    expect(useGeneratorStore.getState().dirty).toBe(true)
    expect(useGeneratorStore.getState().loading).toBe(false)
  })

  it('ignores a template response from the workspace that was replaced', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    const request = deferred<any>()
    vi.mocked(api.getTemplateContent).mockReturnValueOnce(request.promise)

    const pending = useGeneratorStore.getState().loadTemplate('from-a.yaml')
    useWorkspaceStore.getState().setWorkspace(workspace('B'))
    request.resolve({
      value: 'from-a.yaml',
      label: 'from-a.yaml',
      path: '/from-a.yaml',
      content: 'workspace: A',
      parsed_config: { workspace: 'A' },
      read_only: false,
      mode_hint: 'yaml',
    })
    await pending

    expect(useGeneratorStore.getState().selectedTemplate).toBe('')
    expect(useGeneratorStore.getState().templateContent).toBeNull()
    expect(useGeneratorStore.getState().yamlText).toBe('')
    expect(useGeneratorStore.getState().loading).toBe(false)
  })

  it('combines all workspace draft sources into one switch confirmation', async () => {
    useRuntimeStore.setState({ dirty: true })
    useGeneratorStore.setState({ dirty: true })
    useTaskDetailDraftStore.getState().setDirty('train', true)

    expect(getUnsavedWorkspaceChangeLabels()).toEqual([
      'runtime settings',
      'generator draft',
      'task details',
    ])
    const pending = confirmDiscardWorkspaceChanges()
    expect(useConfirmationStore.getState().request).toMatchObject({
      title: 'Discard unsaved changes?',
      description: 'Discard unsaved runtime settings, generator draft and task details before switching workspaces?',
      confirmLabel: 'Discard and Switch',
      confirmVariant: 'danger',
    })
    useConfirmationStore.getState().respond(false)
    await expect(pending).resolves.toBe(false)
  })

  it('keeps session preferences usable when localStorage writes fail', () => {
    vi.stubGlobal('window', {
      localStorage: {
        getItem: vi.fn(() => null),
        setItem: vi.fn(() => { throw new Error('quota exceeded') }),
      },
    })

    expect(() => useTaskStore.getState().setColumns(3)).not.toThrow()
    expect(() => useGeneratorStore.getState().setColumns(4)).not.toThrow()
    expect(() => useGeneratorStore.getState().togglePin('trainer.lr')).not.toThrow()
    expect(() => useThemeStore.getState().toggle()).not.toThrow()
    expect(useTaskStore.getState().columns).toBe(3)
    expect(useGeneratorStore.getState().columns).toBe(4)
    expect(useGeneratorStore.getState().pinnedParams).toContain('trainer.lr')
  })

  it('does not switch workspaces after a launcher request is cancelled', async () => {
    useWorkspaceStore.getState().setWorkspace(workspace('A'))
    useLauncherStore.setState({ selectedScript: '/project/train.py', selectedConfig: '' })
    const request = deferred<any>()
    vi.mocked(api.openLauncherWorkspace).mockReturnValueOnce(request.promise)

    const pending = useLauncherStore.getState().openWorkspace()
    useLauncherStore.getState().reset()
    request.resolve(workspace('B'))

    await expect(pending).resolves.toBe(false)
    expect(useWorkspaceStore.getState().workspace?.run_root).toBe('A')
  })
})
