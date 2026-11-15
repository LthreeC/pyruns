import { expect, test } from '@playwright/test'
import { mkdtemp, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

test('created batch submits exact identities once and preserves the result when reopened', async ({ page }, testInfo) => {
  const root = await mkdtemp(join(tmpdir(), 'pyruns-created-batch-'))
  const script = join(root, 'train.py'), config = join(root, 'input.yaml')
  await writeFile(script, 'import pyruns\nconfig = pyruns.load()\n')
  await writeFile(config, 'value: original\n')
  await page.goto('/?token=pyruns-e2e-access-token')
  const original = await (await page.request.get('/api/workspace')).json()
  const tasks = ['batch-first', 'batch-second', 'unrelated-prefix'].map((name, index) => ({
    name, status: 'pending', task_identity: `created-${index}`, task_kind: 'config',
    created_at: '2026-11-15_12-00-00', run_index: 0,
  }))
  let requests = 0
  let release!: () => void
  const gate = new Promise<void>(resolve => { release = resolve })
  try {
    const opened = await page.request.post('/api/launcher/open', { data: { script_path: script, config_path: config } })
    expect(opened.ok()).toBe(true)
    await page.route('**/api/generator/create', route => route.fulfill({ json: { count: tasks.length, items: tasks, task_kind: 'config' } }))
    await page.route('**/api/tasks/batch/run?*', async route => {
      requests++
      expect(route.request().postDataJSON()).toEqual({
        task_names: tasks.map(task => task.name),
        created_task_identities: Object.fromEntries(tasks.map(task => [task.name, task.task_identity])),
      })
      await gate
      await route.fulfill({ json: { count: 2, items: tasks.slice(0, 2).map(task => ({ ...task, status: 'queued' })), skipped: [tasks[2].name] } })
    })
    await page.goto('/generator')
    await expect(page.getByRole('textbox', { name: 'value parameter value', exact: true })).toHaveValue('original')
    await page.screenshot({ path: testInfo.outputPath('generator-workspace.png') })
    await page.getByRole('button', { name: 'Generate Tasks', exact: true }).click()
    const view = page.getByRole('button', { name: 'View and run this batch (3)' })
    await view.click()
    const batch = page.getByRole('dialog', { name: 'Created batch · 3 tasks' })
    for (const task of tasks) await expect(batch.getByText(task.name, { exact: true })).toBeVisible()
    await page.screenshot({ path: testInfo.outputPath('created-batch.png') })
    // Two clicks in the same event loop must still make only one request.
    await batch.getByRole('button', { name: 'Run 3 pending tasks' }).evaluate((button: HTMLButtonElement) => { button.click(); button.click() })
    await expect.poll(() => requests).toBe(1)
    await expect(batch.getByRole('button', { name: 'Submitting…' })).toBeDisabled()
    await expect(batch.getByRole('button', { name: 'Close created batch' })).toBeDisabled()
    await page.keyboard.press('Escape')
    await expect(batch).toBeVisible()
    release()
    await expect(batch.getByRole('status')).toContainText('Submitted 2 tasks; 1 were not started.')
    await expect(batch.getByRole('button', { name: 'Batch submitted' })).toBeDisabled()
    await batch.getByRole('button', { name: 'Close', exact: true }).click()
    await view.click()
    await expect(batch.getByRole('button', { name: 'Batch submitted' })).toBeDisabled()
    await expect(batch.getByRole('status')).toContainText('Submitted 2 tasks; 1 were not started.')
    expect(requests).toBe(1)
    await batch.getByRole('button', { name: 'Close', exact: true }).click()
  } finally {
    release()
    await page.request.post('/api/workspace/run-root', { data: { path: original.run_root } })
    await rm(root, { recursive: true, force: true })
  }
})

test('task source and uncertainty remain distinct from running and log connectivity', async ({ page }, testInfo) => {
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
  await expect(page.getByRole('button', { name: 'Open details for remote-task' })).toContainText('Running')
  await page.screenshot({ path: testInfo.outputPath('task-source.png') })
  task.cancel_requested_at = Date.now() / 1000
  await page.goto('/monitor?task=remote-task')
  await expect(page.getByText('gpu-worker-02', { exact: true })).toBeVisible()
  await expect(page.getByText('Cancellation awaiting confirmation', { exact: true })).toBeVisible()
  await expect(page.getByTitle('Log connection status; task execution is reported separately.')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})
