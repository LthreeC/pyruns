import { expect, test } from '@playwright/test'
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

test('generator saves a batch to the selected device and opens it in Manager', async ({ page }, testInfo) => {
  const root = await mkdtemp(join(tmpdir(), 'pyruns-created-batch-'))
  const script = join(root, 'train.py'), config = join(root, 'input.yaml')
  await writeFile(script, 'import pyruns\nconfig = pyruns.load()\n')
  await writeFile(config, 'value: original\n')
  await page.goto('/?token=pyruns-e2e-access-token')
  const original = await (await page.request.get('/api/workspace')).json()
  try {
    const opened = await page.request.post('/api/launcher/open', { data: { script_path: script, config_path: config } })
    expect(opened.ok()).toBe(true)
    await page.goto('/generator')
    const target = page.getByRole('checkbox', { name: 'This device only', exact: true })
    await expect(target).not.toBeChecked()
    await page.getByText('This device only', { exact: true }).click()
    await expect(target).toBeChecked()
    await page.getByRole('textbox', { name: 'value parameter value', exact: true }).fill('left | right')
    await page.screenshot({ path: testInfo.outputPath('generator-workspace.png') })
    await page.getByRole('button', { name: 'Preview Batch Tasks' }).click()
    const created = page.waitForResponse(response => response.url().endsWith('/api/generator/create'))
    await page.getByRole('button', { name: 'Generate All' }).click()
    const result = await (await created).json()
    expect(result.count).toBe(2)
    for (const task of result.items) {
      expect(task.task_source).toBe('local')
      expect(task.status).toBe('pending')
    }
    await expect(target).toBeChecked()
    await expect(page.getByRole('button', { name: /run this batch/i })).toHaveCount(0)
    await page.screenshot({ path: testInfo.outputPath('created-batch.png') })
    await page.getByRole('button', { name: 'Open in Manager' }).click()
    await expect(page.getByRole('button', { name: 'Close task details' })).toBeVisible()
    await expect(page.getByText(result.items[0].name, { exact: true }).first()).toBeVisible()
  } finally {
    await page.request.post('/api/workspace/run-root', { data: { path: original.run_root } })
    await rm(root, { recursive: true, force: true })
  }
})

test('Generator remembers the storage and timestamp checkboxes per browser workspace', async ({ page }) => {
  const root = await mkdtemp(join(tmpdir(), 'pyruns-generator-preferences-'))
  const first = join(root, 'first'), second = join(root, 'second')
  await mkdir(first)
  await mkdir(second)
  await page.goto('/?token=pyruns-e2e-access-token')
  const original = await (await page.request.get('/api/workspace')).json()
  const deviceOnly = page.getByRole('checkbox', { name: 'This device only', exact: true })
  const timestamp = page.getByRole('checkbox', { name: 'Append timestamp', exact: true })
  const open = async (path: string) => {
    const response = await page.request.post('/api/launcher/open-shell-root', { data: { path } })
    expect(response.ok(), await response.text()).toBe(true)
    await page.goto('/generator')
    await expect(deviceOnly).toBeVisible()
  }
  try {
    await open(first)
    await expect(deviceOnly).not.toBeChecked()
    await expect(timestamp).toBeChecked()
    await deviceOnly.check()
    await timestamp.uncheck()
    await page.getByRole('link', { name: 'Home', exact: true }).click()
    await expect(page.getByRole('heading', { name: 'Dashboard', exact: true })).toBeVisible()
    await page.getByRole('link', { name: 'Generator', exact: true }).click()
    await page.reload()
    await expect(deviceOnly).toBeChecked()
    await expect(timestamp).not.toBeChecked()

    await open(second)
    await expect(deviceOnly).not.toBeChecked()
    await expect(timestamp).toBeChecked()
    await open(first)
    await expect(deviceOnly).toBeChecked()
    await expect(timestamp).not.toBeChecked()
    await deviceOnly.uncheck()
    await page.reload()
    await expect(deviceOnly).not.toBeChecked()
  } finally {
    await page.request.post('/api/workspace/run-root', { data: { path: original.run_root } })
    await rm(root, { recursive: true, force: true })
  }
})

test('Runtime Python settings omit the new-task storage selector', async ({ page }) => {
  await page.goto('/manager?token=pyruns-e2e-access-token')
  await page.getByRole('button', { name: 'Runtime', exact: true }).click()
  const panel = page.getByRole('dialog', { name: 'Runtime settings' })
  await panel.getByRole('button', { name: 'Path', exact: true }).click()
  await expect(panel.getByRole('textbox', { name: 'Python executable path' })).toBeVisible()
  await expect(panel.getByRole('combobox', { name: /Save new tasks to/ })).toHaveCount(0)
})

test('task source and uncertainty remain distinct from running and log connectivity', async ({ page, isMobile }, testInfo) => {
  const task = {
    name: 'remote-task', status: 'running', task_kind: 'shell', task_identity: 'remote-original',
    dir: '/experiments/remote-task', config_file: 'config.sh', config_text: 'echo hello',
    created_at: '2026-11-15_12-00-00', run_index: 1, runner_host: 'gpu-worker-02',
    task_storage: 'shared', runner_location: 'remote', runner_lease_expired: true, cancel_requested_at: null as number | null,
    start_times: ['2026-11-15_12-00-00'], finish_times: [], pids: [123],
    run_statuses: ['running'], records: [], tracks: [], notes: '', env: {}, progress: 0,
  }
  const local = {
    ...task, name: 'local-task', dir: '/experiments/local-task', task_identity: 'local-original',
    runner_host: 'local-worker', runner_location: 'unknown', local_process_verified: true,
  }
  const pending = {
    ...local, name: 'local-pending', dir: '/experiments/local-pending', task_identity: 'pending-local',
    task_storage: 'local', status: 'pending', runner_lease_expired: null, local_process_verified: false, run_index: 0,
    runner_host: null, pids: [], start_times: [], run_statuses: [],
  }
  const remotePending = {
    ...pending, name: 'remote-pending', dir: '/experiments/remote-pending', task_identity: 'pending-remote',
    task_storage: 'remote', task_source_host: 'gpu-worker-03', runner_location: 'remote',
  }
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [local, task, pending, remotePending], total: 4, offset: 0, limit: 50, has_more: false,
    status_counts: { pending: 2, queued: 0, running: 2, completed: 0, failed: 0, cancelled: 0 },
  } }))
  await page.route('**/api/tasks/remote-task?*', route => route.fulfill({ json: task }))
  const remoteStopRequests: string[] = []
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/tasks/remote-task/cancel') remoteStopRequests.push(request.url())
  })
  for (const observed of [task, local]) {
    await page.route(`**/api/tasks/${observed.name}/logs?*`, route => route.fulfill({ json: {
      task_name: observed.name, content: 'hello\n', offset: 6, selected_log: 'run1.log', available_logs: ['run1.log'], log_identity: 'log-1',
    } }))
    await page.routeWebSocket(`**/api/tasks/${observed.name}/logs/stream?*`, () => {})
  }
  await page.routeWebSocket('**/api/tasks/events?*', socket => socket.send(JSON.stringify({ type: 'ready' })))
  await page.goto('/manager?token=pyruns-e2e-access-token')
  await expect(page.getByText('Stop on gpu-worker-02', { exact: true })).toBeVisible()
  await expect(page.getByText('Runner status awaiting confirmation', { exact: true })).toBeVisible()
  await expect(page.getByTitle('The recorded runner lease has expired. This does not confirm that the process stopped; waiting for a verified result.')).toBeVisible()
  const card = page.locator('[data-task-card="remote-task"]')
  await expect(card.getByRole('button', { name: /^Stop remote-task$/ })).toHaveCount(0)
  await expect(card.getByText('Last reported running', { exact: true })).toBeVisible()
  await expect(card.getByRole('button', { name: 'Open details for remote-task' })).toHaveAccessibleDescription('Last reported running')
  const localCard = page.locator('[data-task-card="local-task"]')
  await expect(localCard.getByText('Running', { exact: true })).toBeVisible()
  await expect(localCard.getByText('Runner status awaiting confirmation')).toHaveCount(0)
  const runningColor = await localCard.locator('.task-status-dot').evaluate(dot => getComputedStyle(dot).backgroundColor)
  await expect(card.locator('.task-status-dot')).toHaveCSS('background-color', runningColor)
  await expect(card.locator('.task-status-dot')).toHaveCSS('border-top-style', 'solid')
  const pendingCard = page.locator('[data-task-card="local-pending"]')
  const pendingColor = await pendingCard.evaluate(element => getComputedStyle(element).borderTopColor)
  const reportedPending = page.locator('[data-task-card="remote-pending"]')
  await expect(reportedPending).toHaveCSS('border-top-color', pendingColor)
  await expect(reportedPending.locator('.task-status-dot')).toHaveCSS('background-color', pendingColor)
  await expect(reportedPending.locator('.task-status-dot')).toHaveCSS('border-top-style', 'dashed')
  await page.screenshot({ path: testInfo.outputPath('task-source.png') })
  await page.goto('/monitor')
  const localRow = page.getByRole('button', { name: 'View local-task, running', exact: true })
  const recordedRow = page.getByRole('button', { name: 'View remote-task, last reported running', exact: true })
  const localDot = localRow.getByRole('img', { name: 'Running', exact: true })
  const remoteDot = recordedRow.getByRole('img', { name: 'Last reported running', exact: true })
  await expect(localDot).toHaveCSS('border-top-style', 'solid')
  await expect(remoteDot).toHaveCSS('border-top-style', 'solid')
  await expect(localDot).toHaveCSS('background-color', runningColor)
  await expect(remoteDot).toHaveCSS('background-color', runningColor)
  const localPendingDot = page.getByRole('button', { name: 'View local-pending, pending', exact: true })
    .getByRole('img', { name: 'Pending', exact: true })
  const remotePendingDot = page.getByRole('button', { name: 'View remote-pending · gpu-worker-03, pending', exact: true })
    .getByRole('img', { name: 'Pending', exact: true })
  await expect(localPendingDot).toHaveCSS('border-top-style', 'solid')
  await expect(remotePendingDot).toHaveCSS('border-top-style', 'dashed')
  await expect(remotePendingDot).toHaveCSS('border-top-width', '2px')
  await expect(localPendingDot).toHaveCSS('background-color', pendingColor)
  await expect(remotePendingDot).toHaveCSS('background-color', pendingColor)
  await page.screenshot({ path: testInfo.outputPath('monitor-status-origins.png') })
  const header = page.locator('.monitor-header')
  const title = page.locator('.monitor-task-title')
  await expect(header.getByText('Running', { exact: true })).toBeVisible()
  await recordedRow.click()
  await expect(header.getByText('Last reported running', { exact: true })).toBeVisible()
  await expect(header.getByRole('button', { name: 'Stop', exact: true })).toHaveCount(0)
  const execution = title.getByRole('group', { name: 'Execution source' })
  await expect(execution.getByText('Stop on gpu-worker-02', { exact: true })).toBeVisible()
  const warning = execution.getByText('Runner status awaiting confirmation', { exact: true })
  await expect(execution).toHaveCSS('white-space', 'nowrap')
  const taskBox = (await title.getByText('remote-task', { exact: true }).boundingBox())!
  const sourceBox = (await execution.boundingBox())!
  expect(Math.abs(taskBox.y + taskBox.height / 2 - sourceBox.y - sourceBox.height / 2)).toBeLessThan(2)
  await expect(page.getByTitle('Log connection status; task execution is reported separately.')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('monitor-execution.png') })
  if (!isMobile) {
    await page.setViewportSize({ width: 1920, height: 900 })
    await expect(warning).toHaveCSS('position', 'static')
    await page.screenshot({ path: testInfo.outputPath('monitor-execution-wide.png') })
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  task.status = 'cancelled'
  await page.getByRole('button', { name: 'Refresh tasks', exact: true }).click()
  const cancelledRow = page.getByRole('button', { name: 'View remote-task, cancelled', exact: true })
  const cancelledDot = cancelledRow.getByRole('img', { name: 'Cancelled', exact: true })
  await expect(cancelledDot).toHaveCSS('border-top-style', 'solid')
  await expect(cancelledDot).toHaveAttribute('data-task-tone', 'cancelled')
  await expect(header.getByText('Cancelled', { exact: true })).toBeVisible()
  expect(remoteStopRequests).toEqual([])
})

test('Manager and Monitor allow retrying a pending local stop', async ({ page }) => {
  const task = {
    name: 'local-run', status: 'running', task_kind: 'shell', task_identity: 'local-stop',
    dir: '/runs/local-run', config_file: 'config.sh', task_storage: 'shared', runner_location: 'local',
    run_index: 1, pids: [123], local_process_verified: true, cancel_requested_at: Date.now() / 1000,
    start_times: ['2026-11-24_10-00-00'], finish_times: [], run_statuses: ['running'],
    config: {}, env: {}, records: [], tracks: [], _load_error: 'Temporary read error',
  }
  let requests = 0
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [task], total: 1, offset: 0, limit: 50, has_more: false,
    status_counts: { [task.status]: 1 },
  } }))
  await page.route('**/api/tasks/local-run?*', route => route.fulfill({ json: task }))
  await page.route('**/api/tasks/local-run/cancel?*', route => {
    expect(new URL(route.request().url()).searchParams.get('local_only')).toBe('true')
    requests += 1
    if (requests === 2) {
      task.status = 'cancelled'
      task.run_statuses = ['cancelled']
    }
    return route.fulfill({ json: { ok: true, task } })
  })
  await page.route('**/api/tasks/local-run/logs?*', route => route.fulfill({ json: {
    task_name: task.name, content: 'hello\n', offset: 6, selected_log: 'run1.log',
    available_logs: ['run1.log'], log_identity: 'log-1',
  } }))
  await page.routeWebSocket('**/api/tasks/events?*', socket => socket.send(JSON.stringify({ type: 'ready' })))
  await page.routeWebSocket('**/api/tasks/local-run/logs/stream?*', () => {})

  await page.goto('/manager?token=pyruns-e2e-access-token')
  const card = page.locator('[data-task-card="local-run"]')
  const retryCard = card.getByRole('button', { name: 'Stop local-run', exact: true })
  await expect(card.getByText('Running', { exact: true })).toBeVisible()
  await expect(card).toHaveAttribute('data-task-tone', 'running')
  await retryCard.click()
  await page.getByRole('button', { name: 'Stop Task', exact: true }).click()
  await expect.poll(() => requests).toBe(1)
  await expect(page.getByRole('dialog')).toHaveCount(0)
  await expect(retryCard).toBeEnabled()

  await page.goto('/monitor')
  const header = page.locator('.monitor-header')
  await expect(header.getByText('Running', { exact: true })).toBeVisible()
  await header.getByRole('button', { name: 'Stop', exact: true }).click()
  await page.getByRole('button', { name: 'Stop Task', exact: true }).click()
  await expect.poll(() => requests).toBe(2)
  await expect(header.getByText('Cancelled', { exact: true })).toBeVisible()
  await expect(header.getByRole('button', { name: 'Run', exact: true })).toBeEnabled()
})

test('source checkboxes filter both task views and allow an empty selection', async ({ page }, testInfo) => {
  const remote = 'tasks-gpu-production-aaaaaaaaaaaa'
  const remoteName = 'trial (gpu-production-aaaaaaaaaaaa)'
  const sources = [{ id: 'shared', host: '' }, { id: 'local', host: 'laptop' }, { id: remote, host: 'gpu-production' }]
  const taskForSource = (source: (typeof sources)[number]) => ({
    name: source.id === remote ? remoteName : source.host || 'common-trial',
    ...(source.id === remote ? { display_name: 'trial', task_source_host: source.host } : {}),
    task_source: source.id, dir: '/runs/' + source.id + '/trial',
    task_storage: source.id === remote ? 'remote' : source.id, status: 'completed', run_index: 1,
    config_file: 'config.sh', env: {}, notes: '', records: [], start_times: [], finish_times: [],
  })
  await page.route(`**/api/tasks/${encodeURIComponent(remoteName)}?*`, route => route.fulfill({ json: taskForSource(sources[2]) }))
  await page.route(`**/api/tasks/${encodeURIComponent(remoteName)}/logs?*`, route => route.fulfill({ json: {
    task_name: remoteName, selected_log: 'run1.log', available_logs: ['run1.log'], content: 'remote output\n', offset: 14,
  } }))
  await page.route('**/api/tasks?*', route => {
    const selected = new URL(route.request().url()).searchParams.get('sources')?.split(',')
    const items = sources.filter(source => !selected || selected.includes(source.id)).map(taskForSource)
    return route.fulfill({ json: { items, total: items.length, offset: 0, limit: 50, has_more: false, task_sources: sources } })
  })
  await page.goto('/manager?token=pyruns-e2e-access-token')
  const trigger = page.getByRole('button', { name: 'Task sources' })
  const all = page.getByRole('checkbox', { name: 'Select all sources' })
  const remoteCard = page.getByRole('button', { name: 'Open details for trial · gpu-production' })
  await expect(remoteCard).toContainText('Other device · gpu-production')
  await expect(remoteCard).not.toContainText('aaaaaaaaaaaa')
  await expect(page.getByRole('button', { name: 'Pin trial · gpu-production' })).toBeEnabled()
  await expect(page.getByRole('button', { name: 'Reorder trial · gpu-production' })).toBeEnabled()
  await expect(page.getByRole('button', { name: 'Delete trial · gpu-production' })).toBeEnabled()
  await remoteCard.click()
  const details = page.getByRole('dialog', { name: 'Task details for trial' })
  await details.getByRole('button', { name: 'Rename task' }).click()
  await expect(details.getByTitle('New task name')).toHaveValue('trial')
  await details.getByRole('button', { name: 'Cancel task rename' }).click()
  await details.getByRole('tab', { name: 'Notes', exact: true }).click()
  await expect(details.getByRole('textbox', { name: 'Task notes' })).toBeEditable()
  await details.getByRole('tab', { name: 'Env', exact: true }).click()
  await expect(details.getByRole('button', { name: 'Add environment variable' })).toBeEnabled()
  await details.getByRole('button', { name: 'Close task details' }).click()
  await trigger.click()
  const width = (await all.boundingBox())!.width
  await all.click({ position: { x: width - 3, y: 14 } })
  await expect(page.locator('[data-task-card]')).toHaveCount(0)
  await page.getByRole('checkbox', { name: 'gpu-production' }).getByText('gpu-production').click()
  await expect(all).toHaveAttribute('aria-checked', 'mixed')
  await expect(page.locator('[data-task-card]')).toHaveCount(1)
  await page.keyboard.press('Escape')
  await expect(trigger).toBeFocused()
  await page.reload()
  await expect(page.locator('[data-task-card]')).toHaveCount(1)
  await expect(remoteCard).toBeVisible()
  await page.getByRole('button', { name: 'Select tasks', exact: true }).click()
  await page.getByRole('button', { name: 'Select trial · gpu-production', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Run (0)', exact: true })).toBeDisabled()
  await expect(page.getByRole('button', { name: 'Delete', exact: true })).toBeEnabled()
  await page.getByRole('button', { name: 'Cancel', exact: true }).click()
  await page.getByRole('link', { name: 'Monitor', exact: true }).first().click()
  const terminal = page.getByRole('region', { name: 'Read-only logs for trial' })
  await expect(terminal).toContainText('remote output')
  await expect(page.getByRole('button', { name: 'View trial · gpu-production, completed', exact: true })).toBeVisible()
  await trigger.click()
  await expect(page.getByRole('checkbox', { name: 'gpu-production' })).toBeChecked()
  await expect(page.getByRole('checkbox', { name: 'Common tasks' })).not.toBeChecked()
  await page.getByRole('checkbox', { name: /This device/ }).click({ position: { x: width - 3, y: 14 } })
  await expect(page.getByRole('checkbox', { name: /This device/ })).toBeChecked()
  await page.screenshot({ path: testInfo.outputPath('monitor-sources.png') })
  await all.focus()
  await page.keyboard.press('Space')
  await expect(page.getByRole('checkbox', { name: 'Common tasks' })).toBeChecked()
  await all.uncheck()
  await expect(terminal).toHaveCount(0)
  await expect(page.getByText('No task sources selected')).toBeVisible()
  await page.reload()
  await expect(page.getByText('No task sources selected')).toBeVisible()
})

test('device search navigates matching hosts without changing hidden selections', async ({ page }) => {
  const sources = [
    { id: 'shared', host: '' }, { id: 'local', host: 'desk' },
    ...Array.from({ length: 12 }, (_, i) => ({ id: `tasks-lab-${i}`, host: `lab-${i}` })),
  ]
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [], total: 0, offset: 0, limit: 50, has_more: false, task_sources: sources,
  } }))
  await page.goto('/manager?token=pyruns-e2e-access-token')
  const trigger = page.getByRole('button', { name: 'Task sources' })
  await trigger.click()
  const search = page.getByRole('searchbox', { name: 'Find a source' })
  await search.fill('LAB-11')
  const device = page.getByRole('checkbox', { name: 'lab-11', exact: true })
  await search.press('ArrowDown')
  await expect(device).toBeFocused()
  await expect(device).toBeChecked()
  await device.press('Space')
  await expect(device).not.toBeChecked()
  await search.fill('absent')
  await expect(page.getByText('No matching sources')).toBeVisible()
  await search.press('Escape')
  await expect(trigger).toBeFocused()
  await trigger.click()
  await expect(search).toHaveValue('')
  await expect(page.getByRole('checkbox', { name: 'lab-0', exact: true })).toBeChecked()
  await expect(device).not.toBeChecked()
  await search.fill('lab-11')
  await page.getByRole('checkbox', { name: 'Select all sources' }).click()
  await expect(device).toBeChecked()
})
