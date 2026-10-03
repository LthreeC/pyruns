import { expect, test } from '@playwright/test'

test('monitor pages beyond 10000 tasks while preserving the open log and cross-page export', async ({ page }, testInfo) => {
  test.setTimeout(90_000)
  const tasks = Array.from({ length: 10_201 }, (_, index) => ({
    name: `page-${String(index).padStart(5, '0')}`, status: 'completed', task_kind: 'shell',
    run_index: 2, pinned: false, created_at: '2026-10-04T00:00:00Z',
    config: {}, env: {}, start_times: [], finish_times: [], pids: [], records: [], tracks: [],
  }))
  const requests: { offset: number, limit: number, query: string, returned: number }[] = []
  let logReads = 0
  let exported: string[] = []
  await page.route('**/api/tasks?*', route => {
    const url = new URL(route.request().url())
    const offset = Number(url.searchParams.get('offset') || 0)
    const limit = Number(url.searchParams.get('limit') || 50)
    if (limit > 10_000) return route.fulfill({ status: 422, json: { detail: 'Page size exceeds 10000' } })
    const query = url.searchParams.get('query') || ''
    const matching = tasks.filter(task => !query || task.name.includes(query))
    const items = matching.slice(offset, offset + limit).map(task => query ? {
      ...task, search_matches: [{ field: 'name', location: 'name', snippet: task.name,
        match_start: 0, match_end: task.name.length }], search_match_count: 1,
    } : task)
    requests.push({ offset, limit, query, returned: items.length })
    return route.fulfill({ json: { items, total: matching.length, offset, limit,
      has_more: offset + items.length < matching.length } })
  })
  await page.route('**/api/tasks/page-**', route => {
    const url = new URL(route.request().url())
    const match = url.pathname.match(/\/tasks\/(page-\d+)(\/logs)?$/)!
    const task = tasks[Number(match[1].slice(5))]
    if (!match[2]) return route.fulfill({ json: task })
    logReads++
    const log = url.searchParams.get('log_file_name') || 'run2.log'
    const content = `${log}: preserved output for ${task.name}\n`
    return route.fulfill({ json: { selected_log: log, available_logs: ['run2.log', 'run1.log'],
      content, offset: content.length, log_identity: `${task.name}/${log}` } })
  })
  await page.routeWebSocket('**/api/tasks/events?*', socket => socket.send(JSON.stringify({ type: 'ready' })))
  await page.route('**/api/tasks/export/csv', route => {
    exported = route.request().postDataJSON().task_names
    return route.fulfill({ contentType: 'text/csv', body: 'task_name\n' + exported.join('\n') })
  })

  await page.goto('/monitor?token=pyruns-e2e-access-token')
  const sidebar = page.getByRole('complementary', { name: 'Task monitor sidebar' })
  const rows = sidebar.locator('button[aria-label^="View page-"]')
  const terminal = page.getByRole('region', { name: 'Read-only logs for page-00000' })
  const logs = page.getByRole('combobox', { name: 'Select task log file' })
  await expect(rows).toHaveCount(200)
  await expect(terminal).toContainText('run2.log: preserved output')
  await logs.selectOption('run1.log')
  await expect(terminal).toContainText('run1.log: preserved output')
  const historicalReads = logReads

  await sidebar.getByRole('button', { name: 'Export', exact: true }).click()
  await sidebar.getByRole('button', { name: 'Select page', exact: true }).click()
  await expect(sidebar.getByText('200 selected', { exact: true })).toBeVisible()
  const next = sidebar.getByRole('button', { name: 'Next page', exact: true })
  await next.click()
  await expect(sidebar.getByText('2 / 52', { exact: true })).toBeVisible()
  await sidebar.getByRole('button', { name: 'Select page', exact: true }).click()
  await expect(sidebar.getByText('400 selected', { exact: true })).toBeVisible()
  await sidebar.getByRole('button', { name: 'Deselect page', exact: true }).click()
  await expect(sidebar.getByText('200 selected', { exact: true })).toBeVisible()
  await sidebar.getByRole('button', { name: 'Select page-00200, completed', exact: true }).click()
  const download = page.waitForEvent('download')
  await sidebar.getByRole('button', { name: 'Export', exact: true }).click()
  await download
  expect(exported).toEqual(tasks.slice(0, 201).map(task => task.name))

  let maxRows = 0
  for (let currentPage = 3; currentPage <= 52; currentPage++) {
    await next.click()
    await expect(sidebar.getByText(`${currentPage} / 52`, { exact: true })).toBeVisible()
    const rowCount = await rows.count()
    maxRows = Math.max(maxRows, rowCount)
    expect(rowCount).toBeLessThanOrEqual(201) // One independent Current Task card is allowed.
  }
  await expect(sidebar.getByRole('button', { name: 'View page-10200, completed', exact: true })).toBeInViewport()
  await expect(next).toBeDisabled()
  await expect(logs).toHaveValue('run1.log')
  await expect(terminal).toContainText('run1.log: preserved output for page-00000')
  expect(logReads).toBe(historicalReads)
  expect(requests.some(request => request.offset === 10_000 && request.limit === 200)).toBe(true)
  expect(requests.every(request => request.limit === 200 && request.returned <= 200)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('monitor-pagination.png') })

  await sidebar.getByRole('textbox', { name: 'Search monitor tasks' }).fill('page-10200')
  await expect(sidebar.getByRole('button', { name: /View Name match in page-10200/ })).toBeVisible()
  expect(requests.at(-1)).toMatchObject({ offset: 0, limit: 200, query: 'page-10200' })
  await expect(logs).toHaveValue('run1.log')
  await expect(terminal).toContainText('run1.log: preserved output for page-00000')
  console.log(JSON.stringify({ scope: 'Mock HTTP pagination, not backend throughput',
    requests: requests.length, returnedTaskRows: requests.reduce((sum, request) => sum + request.returned, 0), maxRows }))
})
