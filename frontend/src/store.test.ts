import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as api from './api'
import {
  confirmDiscardWorkspaceChanges,
  getUnsavedWorkspaceChangeLabels,
  useConfirmationStore,
  useDashboardStore,
  useGeneratorStore,
  useLauncherStore,
  useRuntimeStore,
  useTaskDetailDraftStore,
  useTaskStore,
  useThemeStore,
  useWorkspaceStore,
} from './store'

vi.mock('./api', () => ({
  getTasks: vi.fn(),
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
    expect(api.getTasks).toHaveBeenCalledWith(expect.objectContaining({ sort: 'activity_asc' }))
    expect(useTaskStore.getState().sortMode).toBe('activity_asc')
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
