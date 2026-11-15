import { expect, test, type Page } from '@playwright/test'

const counts = { pending: 0, queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 }
const gpu = { id: 0, index: 0, uuid: 'GPU-HOME', name: 'Home Test GPU', util: 11, mem_used: 1024, mem_total: 24576, processes: [] }

interface HomeReads {
  stallDashboard: boolean
  stallMetrics: boolean
  dashboard: number
  metrics: number
  aborts: number
  releaseMetrics?: () => void
}

async function prepareHome(page: Page, stalled: 'dashboard' | 'metrics' | '' = '', interval = 60) {
  const now = new Date()
  await page.clock.install({ time: now })
  await page.clock.pauseAt(new Date(now.getTime() + 100))
  await page.addInitScript(endpoint => {
    const original = window.fetch.bind(window)
    const state: HomeReads = { stallDashboard: endpoint === 'dashboard', stallMetrics: endpoint === 'metrics', dashboard: 0, metrics: 0, aborts: 0 }
    Object.assign(window, { homeReads: state })
    window.fetch = ((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const kind = url.startsWith('/api/dashboard?') ? 'dashboard'
        : url.startsWith('/api/system/metrics?') ? 'metrics' : ''
      if (kind) {
        state[kind]++
        if (kind === 'dashboard' ? state.stallDashboard : state.stallMetrics) {
          return new Promise<void>((resolve, reject) => {
            const abort = () => { state.aborts++; reject(init?.signal?.reason) }
            if (init?.signal?.aborted) abort()
            else init?.signal?.addEventListener('abort', abort, { once: true })
            if (kind === 'metrics') state.releaseMetrics = () => {
              init?.signal?.removeEventListener('abort', abort)
              resolve()
            }
          }).then(() => original(input, init))
        }
      }
      return original(input, init)
    }) as typeof window.fetch
  }, stalled)
  await page.route('**/api/workspace', async route => {
    const response = await route.fetch()
    const workspace = await response.json()
    await route.fulfill({ json: { ...workspace, settings: { ...workspace.settings, header_refresh_interval: interval } } })
  })
  await page.route('**/api/dashboard?*', route => {
    expect(new URL(route.request().url()).searchParams.get('include_workspace')).toBe('false')
    return route.fulfill({ json: {
      summary: { total: 0, ...counts }, recent_tasks: [], active_task: null,
    } })
  })
  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [], total: 0, offset: 0, limit: 50, has_more: false, status_counts: counts,
  } }))
  await page.routeWebSocket('**/api/tasks/events?*', () => {})
  return () => page.evaluate(() => {
    const { dashboard, metrics, aborts } = (window as typeof window & { homeReads: HomeReads }).homeReads
    return { dashboard, metrics, aborts }
  })
}

test('Home displays and keeps refreshing GPUs while the task summary is stalled', async ({ page }) => {
  const reads = await prepareHome(page, 'dashboard', 3)
  let version = 0
  await page.route('**/api/system/metrics?*', route => route.fulfill({ json: {
    cpu_percent: 5, mem_percent: 10, gpus: [{ ...gpu, util: ++version * 11 }],
  } }))
  await page.goto('/?token=pyruns-e2e-access-token')
  const card = page.getByRole('button', { name: 'View details for GPU 0 Home Test GPU' })
  await expect(card).toBeVisible()
  await expect(card).toContainText('11%')
  await expect.poll(reads).toEqual({ dashboard: 1, metrics: 1, aborts: 0 })
  await page.clock.runFor(3_001)
  await expect(card).toContainText('22%')
  await expect.poll(reads).toEqual({ dashboard: 1, metrics: 2, aborts: 0 })

  const visibility = (state: string) => page.evaluate(value => {
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => value })
    document.dispatchEvent(new Event('visibilitychange'))
  }, state)
  await visibility('hidden')
  await page.clock.runFor(3_001)
  expect(await reads()).toMatchObject({ dashboard: 1, metrics: 2 })
  await visibility('visible')
  await expect(card).toContainText('33%')
  await expect.poll(reads).toMatchObject({ dashboard: 1, metrics: 3 })
  await page.getByRole('link', { name: 'Manager', exact: true }).click()
  await expect.poll(reads).toEqual({ dashboard: 1, metrics: 3, aborts: 1 })
  await page.clock.runFor(30_001)
  expect(await reads()).toEqual({ dashboard: 1, metrics: 3, aborts: 1 })
})

test('Home distinguishes loading and unavailable metrics from a confirmed empty GPU list', async ({ page }) => {
  await prepareHome(page, 'metrics')
  let unavailable = true
  await page.route('**/api/system/metrics?*', route => route.fulfill(unavailable
    ? { status: 503, json: { detail: 'Metrics temporarily offline' } }
    : { json: { cpu_percent: 5, mem_percent: 10, gpus: [] } }))
  await page.goto('/?token=pyruns-e2e-access-token')
  const resources = page.getByRole('region', { name: 'GPU & System', exact: true })
  await expect(resources.getByText('Loading system metrics...')).toBeVisible()
  await expect(resources.getByText('0 GPUs', { exact: true })).toBeHidden()
  await expect(resources.getByText('No NVIDIA GPU metrics detected.')).toBeHidden()
  await page.evaluate(() => {
    const state = (window as typeof window & { homeReads: HomeReads }).homeReads
    state.stallMetrics = false
    state.releaseMetrics?.()
  })
  await expect(resources.getByRole('alert')).toContainText('Metrics temporarily offline')
  await expect(resources.getByText('0 GPUs', { exact: true })).toBeHidden()
  await expect(resources.getByText('No NVIDIA GPU metrics detected.')).toBeHidden()
  unavailable = false
  await page.getByRole('button', { name: 'Refresh dashboard', exact: true }).click()
  await expect(resources.getByText('0 GPUs', { exact: true })).toBeVisible()
  await expect(resources.getByText('No NVIDIA GPU metrics detected.')).toBeVisible()
  await expect(resources.getByRole('alert')).toBeHidden()
})

test('Home retains its last GPU snapshot on return and through a refresh timeout', async ({ page }, testInfo) => {
  const reads = await prepareHome(page)
  let util = 11
  await page.route('**/api/system/metrics?*', route => route.fulfill({ json: {
    host: 'gpu-worker-01', cpu_percent: 5, mem_percent: 10, gpus: [{ ...gpu, util }],
  } }))
  await page.goto('/?token=pyruns-e2e-access-token')
  const card = page.getByRole('button', { name: 'View details for GPU 0 Home Test GPU' })
  await expect(card).toBeVisible()
  await expect(card).toContainText('11%')
  await page.getByRole('link', { name: 'Manager', exact: true }).click()
  await page.evaluate(() => { (window as typeof window & { homeReads: HomeReads }).homeReads.stallMetrics = true })
  await page.getByRole('link', { name: 'Home', exact: true }).click()
  await expect.poll(reads).toMatchObject({ dashboard: 2, metrics: 2 })
  await expect(card).toBeVisible()
  await expect(card).toContainText('11%')
  const resources = page.getByRole('region', { name: 'GPU & System', exact: true })
  await expect(resources.getByText(/Last updated/)).toContainText('Refreshing')
  await expect(resources.getByText('Loading system metrics...')).toBeHidden()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: testInfo.outputPath('home-cached-metrics.png') })
  await page.clock.runFor(10_001)
  await expect.poll(reads).toMatchObject({ aborts: 1 })
  await expect(resources.getByText('Metrics refresh failed. Showing last values.')).toBeVisible()
  await expect(card).toContainText('11%')
  await page.evaluate(() => { (window as typeof window & { homeReads: HomeReads }).homeReads.stallMetrics = false })
  util = 44
  await page.getByRole('button', { name: 'Refresh dashboard', exact: true }).click()
  await expect(card).toContainText('44%')
  await expect(resources.getByText('Metrics refresh failed. Showing last values.')).toBeHidden()
  await expect(resources.getByText(/Last updated/)).not.toContainText('Refreshing')
  await expect(page.getByText('Dashboard refreshed', { exact: true })).toBeVisible()
})
