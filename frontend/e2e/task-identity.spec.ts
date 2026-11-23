import { expect, test, type Page } from '@playwright/test'

for (const field of ['notes', 'env'] as const) {
  test(`a ${field} draft cannot overwrite a replacement task with the same name`, async ({ page }) => {
    let identity = 'original-task'
    let notes = ''
    let env: Record<string, string> = {}
    const task = () => ({
      name: 'alpha', status: 'pending', task_kind: 'shell', dir: '/tmp/alpha',
      task_identity: identity, config_file: '/tmp/alpha/config.sh',
      config_text: 'echo alpha', preview_text: 'echo alpha',
      created_at: '2026-10-15_12-00-00', run_index: 0, pinned: false, notes, env,
    })
    await page.route('**/api/tasks?*', route => route.fulfill({ json: {
      items: [task()], total: 1, offset: 0, limit: 50, has_more: false,
    } }))
    await page.route('**/api/tasks/alpha?*', route => route.fulfill({ json: task() }))
    const writes: string[] = []
    await page.route(`**/api/tasks/alpha/${field}?*`, route => {
      const payload = route.request().postDataJSON()
      writes.push(payload.expected_task_identity)
      if (payload.expected_task_identity !== identity) {
        return route.fulfill({ status: 409, json: { detail: 'Task identity changed.' } })
      }
      if (field === 'notes') notes = payload.notes
      else env = payload.env
      return route.fulfill({ json: { ok: true, task: task() } })
    })

    await page.goto('/manager?token=pyruns-e2e-access-token')
    const open = page.getByRole('button', { name: 'Open details for alpha' })
    await open.click()
    await page.getByRole('tab', { name: field === 'notes' ? 'Notes' : 'Env', exact: true }).click()
    const draft = field === 'notes'
      ? page.getByRole('textbox', { name: 'Task notes' })
      : page.getByRole('textbox', { name: 'Environment variable value' })
    if (field === 'env') {
      await page.getByRole('button', { name: 'Add environment variable' }).click()
      await page.getByRole('textbox', { name: 'Environment variable key' }).fill('LOCAL')
    }
    await draft.fill('original draft')
    identity = 'replacement-task'
    const save = page.getByRole('button', { name: field === 'notes' ? 'Save Notes' : 'Save', exact: true })
    await save.click()
    await expect(page.getByText('This task was replaced. Your draft is safe. Close and reopen task details before editing the new task.')).toBeVisible()
    await expect(draft).toHaveValue('original draft')
    await expect(page.getByRole('button', { name: field === 'notes' ? 'Replace Notes' : 'Replace Env' })).toBeHidden()
    // Retrying must retain the original identity instead of rebasing onto the replacement.
    await save.click()
    await expect.poll(() => writes.length).toBe(2)
    expect(writes).toEqual(['original-task', 'original-task'])
    expect(notes).toBe('')
    expect(env).toEqual({})

    await page.getByRole('button', { name: 'Close task details' }).click()
    await page.getByRole('dialog', { name: 'Discard changes?' }).getByRole('button', { name: 'Discard', exact: true }).click()
    await open.click()
    await page.getByRole('tab', { name: field === 'notes' ? 'Notes' : 'Env', exact: true }).click()
    if (field === 'env') {
      await page.getByRole('button', { name: 'Add environment variable' }).click()
      await page.getByRole('textbox', { name: 'Environment variable key' }).fill('NEW')
    }
    await draft.fill('replacement draft')
    await save.click()
    await expect.poll(() => writes.length).toBe(3)
    expect(writes[2]).toBe('replacement-task')
    await expect(page.getByRole('button', { name: field === 'notes' ? 'Save Notes' : 'Saved', exact: true })).toBeDisabled()
    if (field === 'notes') expect(notes).toBe('replacement draft')
    else expect(env).toEqual({ NEW: 'replacement draft' })
  })
}

async function installDetailFixture(page: Page, view: 'manager' | 'monitor') {
  const now = new Date()
  await page.clock.install({ time: now })
  await page.clock.pauseAt(new Date(now.getTime() + 100))
  const state = {
    identity: 'original-task', status: 'running', run: 1, progress: 0,
    record: 'original-record', holdNextDetail: false,
    releaseDetail: null as null | (() => void),
  }
  await page.addInitScript(() => {
    const state = { detailAborts: 0 }
    Object.assign(window, { taskDetailLifecycle: state })
    const originalFetch = window.fetch.bind(window)
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), location.origin)
      if (url.pathname === '/api/tasks/alpha' && init?.signal) {
        init.signal.addEventListener('abort', () => { state.detailAborts++ }, { once: true })
      }
      return originalFetch(input, init)
    }) as typeof window.fetch
  })
  const task = (summary = false) => ({
    name: 'alpha', status: state.status, task_kind: 'shell', dir: '/tmp/alpha',
    task_identity: state.identity, config_file: '/tmp/alpha/config.sh',
    config: {}, config_text: summary ? '' : `echo ${state.identity}`, preview_text: `echo ${state.identity}`,
    created_at: '2026-10-15_12-00-00', run_index: state.run, progress: state.progress,
    pinned: false, notes: '', env: {},
    start_times: ['2026-10-15_12-00-00'],
    finish_times: state.status === 'running' ? [] : ['2026-10-15_12-00-10'],
    pids: [123], run_statuses: [state.status],
    records: summary ? [] : [{ result: state.record }], tracks: [],
  })
  let summaryReads = 0
  const detailReads: string[] = []
  await page.route('**/api/tasks?*', route => {
    summaryReads++
    return route.fulfill({ json: { items: [task(true)], total: 1, offset: 0, limit: 50, has_more: false } })
  })
  await page.route('**/api/tasks/alpha?*', async route => {
    if (new URL(route.request().url()).searchParams.get('summary') === 'true') {
      return route.fulfill({ json: task(true) })
    }
    const captured = task()
    detailReads.push(captured.task_identity)
    if (state.holdNextDetail) {
      state.holdNextDetail = false
      await new Promise<void>(resolve => { state.releaseDetail = resolve })
    }
    await route.fulfill({ json: captured })
  })
  await page.route('**/api/tasks/alpha/runs/1/input**', route => route.fulfill({ json: {
    input: { payload_text: `echo saved-${state.identity}`, workdir: '/tmp', environment: {}, shell_executable: '/bin/bash' },
  } }))
  await page.route('**/api/tasks/alpha/logs**', route => route.fulfill({ json: {
    content: '', selected_log: 'run1.log', available_logs: ['run1.log'], offset: 0, log_identity: state.identity,
  } }))
  await page.routeWebSocket('**/api/tasks/alpha/logs/stream?*', socket => {
    socket.send(JSON.stringify({ type: 'ready', task_name: 'alpha', log_file_name: 'run1.log', offset: 0 }))
  })
  await page.route('**/api/tasks/alpha/runs/1/compare**', route => {
    const run = { reference: 'alpha@1', record: {}, input_available: true, source_state: state.identity }
    return route.fulfill({ json: { left: run, right: run, input_diff: {
      available: true, equal: false, changed_fields: ['payload_text'], diff: `comparison-${state.identity}`,
    } } })
  })
  let send: undefined | ((value: string) => void)
  await page.routeWebSocket('**/api/tasks/events?*', socket => {
    send = value => socket.send(value)
    socket.send(JSON.stringify({ type: 'ready' }))
  })
  await page.goto(`/${view}?token=pyruns-e2e-access-token`)
  const openDetail = page.getByRole('button', { name: view === 'manager' ? 'Open details for alpha' : 'View Details', exact: true })
  await expect(openDetail).toBeVisible()
  await expect.poll(() => Boolean(send)).toBe(true)
  await page.clock.runFor(250)
  const invalidate = async () => {
    const before = summaryReads
    send!(JSON.stringify({ type: 'changed' }))
    await page.clock.runFor(250)
    await expect.poll(() => summaryReads).toBeGreaterThan(before)
    await page.clock.runFor(250)
  }
  return { state, detailReads, invalidate, openDetail }
}

test('monitor closes the detail panel and cancels its pending refresh', async ({ page }) => {
  const { state, openDetail } = await installDetailFixture(page, 'monitor')
  state.holdNextDetail = true
  await openDetail.click()
  await expect.poll(() => Boolean(state.releaseDetail)).toBe(true)
  const panel = page.getByRole('dialog', { name: 'Task details for alpha' })
  await panel.getByRole('button', { name: 'Close task details', exact: true }).click()
  await expect(panel).toBeHidden()
  await expect.poll(() => page.evaluate(() => (window as typeof window & {
    taskDetailLifecycle: { detailAborts: number }
  }).taskDetailLifecycle.detailAborts)).toBe(1)
  state.releaseDetail!()
  await page.clock.runFor(250)
})

for (const view of ['manager', 'monitor'] as const) {
test(`${view} same-name replacement reloads detail and discards saved input and comparison state`, async ({ page }) => {
  const { state, detailReads, invalidate, openDetail } = await installDetailFixture(page, view)
  await openDetail.click()
  const panel = page.getByRole('dialog', { name: 'Task details for alpha' })
  const history = panel.locator('details').filter({ has: page.locator('summary').filter({ hasText: 'Run #1' }) })
  await history.locator('summary').click()
  await expect(history).toContainText('original-record')
  await history.getByRole('button', { name: 'Saved input', exact: true }).click()
  await expect(history).toContainText('echo saved-original-task')
  await history.getByRole('button', { name: 'Compare runs', exact: true }).click()
  await history.getByRole('button', { name: 'Compare', exact: true }).click()
  await expect(history).toContainText('comparison-original-task')

  state.identity = 'replacement-task'
  state.record = 'replacement-record'
  await invalidate()
  await expect.poll(() => detailReads).toEqual(['original-task', 'replacement-task'])
  await expect(panel.getByText('comparison-original-task', { exact: true })).toHaveCount(0)
  await expect(history).not.toHaveAttribute('open')
  await history.locator('summary').click()
  await expect(history).toContainText('replacement-record')
  await expect(history).not.toContainText('original-record')
  await expect(history.getByLabel('Run comparison results')).toHaveCount(0)
  await history.getByRole('button', { name: 'Saved input', exact: true }).click()
  await expect(history).toContainText('echo saved-replacement-task')
  await panel.getByRole('tab', { name: 'Config', exact: true }).click()
  await expect(panel.getByRole('tabpanel')).toContainText('echo replacement-task')
  await expect(panel.getByRole('tabpanel')).not.toContainText('echo original-task')
})

test(`${view} a delayed original detail response cannot replace a newer task identity`, async ({ page }) => {
  const { state, detailReads, invalidate, openDetail } = await installDetailFixture(page, view)
  state.holdNextDetail = true
  await openDetail.click()
  await expect.poll(() => Boolean(state.releaseDetail)).toBe(true)
  const panel = page.getByRole('dialog', { name: 'Task details for alpha' })
  await panel.getByRole('tab', { name: 'Notes', exact: true }).click()
  await panel.getByRole('textbox', { name: 'Task notes' }).fill('keep my draft')
  state.identity = 'replacement-task'
  await invalidate()
  await expect.poll(() => detailReads).toEqual(['original-task', 'replacement-task'])
  if (view === 'monitor') {
    await expect.poll(() => page.evaluate(() => (window as typeof window & {
      taskDetailLifecycle: { detailAborts: number }
    }).taskDetailLifecycle.detailAborts)).toBe(1)
  }
  state.releaseDetail!()
  await page.clock.runFor(250)
  await expect(panel.getByRole('textbox', { name: 'Task notes' })).toHaveValue('keep my draft')
  await panel.getByRole('tab', { name: 'Config', exact: true }).click()
  await expect(panel.getByRole('tabpanel')).toContainText('echo replacement-task')
  await expect(panel.getByRole('tabpanel')).not.toContainText('echo original-task')
})

test(`${view} run boundaries refresh records while ordinary progress updates preserve detail and drafts`, async ({ page }) => {
  const { state, detailReads, invalidate, openDetail } = await installDetailFixture(page, view)
  await openDetail.click()
  await expect.poll(() => detailReads.length).toBe(1)
  const panel = page.getByRole('dialog', { name: 'Task details for alpha' })
  await panel.getByRole('tab', { name: 'Notes', exact: true }).click()
  await panel.getByRole('textbox', { name: 'Task notes' }).fill('keep notes')
  await panel.getByRole('tab', { name: 'Env', exact: true }).click()
  await panel.getByRole('button', { name: 'Add environment variable' }).click()
  await panel.getByRole('textbox', { name: 'Environment variable key' }).fill('LOCAL')
  await panel.getByRole('textbox', { name: 'Environment variable value' }).fill('keep env')
  state.progress = 50
  await invalidate()
  expect(detailReads).toHaveLength(1)

  state.status = 'completed'
  state.record = 'completed-record'
  await invalidate()
  await expect.poll(() => detailReads.length).toBe(2)
  await expect(panel.getByRole('textbox', { name: 'Environment variable value' })).toHaveValue('keep env')
  await panel.getByRole('tab', { name: 'Notes', exact: true }).click()
  await expect(panel.getByRole('textbox', { name: 'Task notes' })).toHaveValue('keep notes')
  await panel.getByRole('tab', { name: 'Info', exact: true }).click()
  const history = panel.locator('details').filter({ has: page.locator('summary').filter({ hasText: 'Run #1' }) })
  await history.locator('summary').click()
  await expect(history).toContainText('completed-record')
  await invalidate()
  expect(detailReads).toHaveLength(2)
  state.run = 2
  await invalidate()
  await expect.poll(() => detailReads.length).toBe(3)
})
}
