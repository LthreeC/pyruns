import { expect, test } from '@playwright/test'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
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
    await expect(target).not.toBeChecked()
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

test('new-task storage persists across a reload', async ({ page }) => {
  await page.goto('/manager?token=pyruns-e2e-access-token')
  const initial = await (await page.request.get('/api/runtime')).json()
  const next = initial.task_storage === 'local' ? 'shared' : 'local'
  const openPanel = async () => {
    await page.getByRole('button', { name: 'Runtime', exact: true }).click()
    return page.getByRole('dialog', { name: 'Runtime settings' })
  }
  try {
    const panel = await openPanel()
    await panel.getByRole('combobox', { name: /Save new tasks to/ }).selectOption(next)
    await panel.getByRole('button', { name: 'Save', exact: true }).click()
    await expect.poll(async () => (await (await page.request.get('/api/runtime')).json()).task_storage).toBe(next)
    await page.reload()
    await expect((await openPanel()).getByRole('combobox', { name: /Save new tasks to/ })).toHaveValue(next)
  } finally {
    await page.request.patch('/api/runtime', { data: { task_storage: initial.task_storage } })
  }
})

test('task source and uncertainty remain distinct from running and log connectivity', async ({ page, isMobile }, testInfo) => {
  const task = {
    name: 'remote-task', status: 'running', task_kind: 'shell', task_identity: 'remote-original',
    dir: '/experiments/remote-task', config_file: 'config.sh', config_text: 'echo hello',
    created_at: '2026-11-15_12-00-00', run_index: 1, runner_host: 'gpu-worker-02',
    runner_lease_expired: true, cancel_requested_at: null as number | null,
    start_times: ['2026-11-15_12-00-00'], finish_times: [], pids: [123],
    run_statuses: ['running'], records: [], tracks: [], notes: '', env: {}, progress: 0,
  }
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [task], total: 1, offset: 0, limit: 50, has_more: false,
    status_counts: { pending: 0, queued: 0, running: 1, completed: 0, failed: 0, cancelled: 0 },
  } }))
  await page.route('**/api/tasks/remote-task?*', route => route.fulfill({ json: task }))
  await page.route('**/api/tasks/remote-task/logs?*', route => route.fulfill({ json: {
    task_name: task.name, content: 'hello\n', offset: 6, selected_log: 'run1.log', available_logs: ['run1.log'], log_identity: 'log-1',
  } }))
  await page.routeWebSocket('**/api/tasks/events?*', socket => socket.send(JSON.stringify({ type: 'ready' })))
  await page.routeWebSocket('**/api/tasks/remote-task/logs/stream?*', () => {})
  await page.goto('/manager?token=pyruns-e2e-access-token')
  await expect(page.getByText('gpu-worker-02', { exact: true })).toBeVisible()
  await expect(page.getByText('Runner status awaiting confirmation', { exact: true })).toBeVisible()
  await expect(page.getByTitle('The recorded runner lease has expired. This does not confirm that the process stopped; waiting for a verified result.')).toBeVisible()
  const card = page.locator('[data-task-card="remote-task"]')
  await expect(card.getByText('Last reported running', { exact: true })).toBeVisible()
  await expect(card.getByRole('button', { name: 'Open details for remote-task' })).toHaveAccessibleDescription('Last reported running')
  await page.screenshot({ path: testInfo.outputPath('task-source.png') })
  task.cancel_requested_at = Date.now() / 1000
  await page.goto('/monitor?task=remote-task')
  const title = page.locator('.monitor-task-title')
  const execution = title.getByRole('group', { name: 'Execution source' })
  await expect(execution.getByText('gpu-worker-02', { exact: true })).toBeVisible()
  const warning = execution.getByTitle('Cancellation was requested. Waiting for the runner to confirm the result.')
  await expect(warning).toBeVisible()
  await expect(warning).toHaveText('Cancellation awaiting confirmation')
  await expect(execution).toHaveCSS('white-space', 'nowrap')
  const taskBox = (await title.getByText('remote-task', { exact: true }).boundingBox())!
  const sourceBox = (await execution.boundingBox())!
  expect(Math.abs(taskBox.y + taskBox.height / 2 - sourceBox.y - sourceBox.height / 2)).toBeLessThan(2)
  await expect(page.getByTitle('Log connection status; task execution is reported separately.')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('monitor-execution.png') })
  if (!isMobile) {
    await page.setViewportSize({ width: 1920, height: 900 })
    await expect(warning.getByText('Cancellation awaiting confirmation')).toHaveCSS('position', 'static')
    await page.screenshot({ path: testInfo.outputPath('monitor-execution-wide.png') })
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('source checkboxes filter both task views and allow an empty selection', async ({ page }, testInfo) => {
  const remote = 'tasks-gpu-production-aaaaaaaaaaaa'
  const remoteName = 'trial (gpu-production-aaaaaaaaaaaa)'
  const sources = [{ id: 'shared', host: '' }, { id: 'local', host: 'laptop' }, { id: remote, host: 'gpu-production' }]
  await page.route(`**/api/tasks/${encodeURIComponent(remoteName)}/logs?*`, route => route.fulfill({ json: {
    task_name: remoteName, selected_log: 'run1.log', available_logs: ['run1.log'], content: 'remote output\n', offset: 14,
  } }))
  await page.route('**/api/tasks?*', route => {
    const selected = new URL(route.request().url()).searchParams.get('sources')?.split(',')
    const items = sources.filter(source => !selected || selected.includes(source.id)).map(source => ({
      name: source.id === remote ? remoteName : source.host || 'common-trial',
      ...(source.id === remote ? { display_name: 'trial', task_source_host: source.host } : {}),
      task_source: source.id, dir: '/runs/' + source.id + '/trial',
      task_storage: source.id === remote ? 'remote' : source.id, status: 'completed', run_index: 1,
      config_file: 'config.sh', env: {}, notes: '', records: [], start_times: [], finish_times: [],
    }))
    return route.fulfill({ json: { items, total: items.length, offset: 0, limit: 50, has_more: false, task_sources: sources } })
  })
  await page.goto('/manager?token=pyruns-e2e-access-token')
  const trigger = page.getByRole('button', { name: 'Task sources' })
  const all = page.getByRole('checkbox', { name: 'Select all sources' })
  const remoteCard = page.getByRole('button', { name: 'Open details for trial · gpu-production' })
  await expect(remoteCard).toContainText('gpu-production · read only')
  await expect(remoteCard).not.toContainText('aaaaaaaaaaaa')
  await trigger.click()
  const width = (await all.boundingBox())!.width
  await all.click({ position: { x: width - 3, y: 14 } })
  await expect(page.locator('[data-task-card]')).toHaveCount(0)
  await page.getByRole('checkbox', { name: 'gpu-production' }).getByText('gpu-production').click()
  await expect(all).toHaveAttribute('aria-checked', 'mixed')
  await expect(page.locator('[data-task-card]')).toHaveCount(1)
  await page.keyboard.press('Escape')
  await expect(trigger).toBeFocused()
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
})
