import { expect, test, type Page } from '@playwright/test'

test.use({ locale: 'zh-CN', storageState: { cookies: [], origins: [] } })

const storageKey = 'pyruns.onboarding.v1'
const popover = (page: Page) => page.locator('.pyruns-tour')

async function expectStep(page: Page, target: string) {
  await expect(page.locator(`.pyruns-tour[data-tour-step="${target}"]`)).toBeVisible()
  await expect(page.locator(`[data-tour="${target}"]`)).toHaveClass(/driver-active-element/)
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))))
  const bounds = await popover(page).boundingBox()
  const targetBounds = await page.locator(`[data-tour="${target}"]`).boundingBox()
  const viewport = page.viewportSize()!
  expect(bounds).not.toBeNull()
  expect(bounds!.x).toBeGreaterThanOrEqual(0)
  expect(bounds!.y).toBeGreaterThanOrEqual(0)
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(viewport.width + 1)
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(viewport.height + 1)
  expect(targetBounds).not.toBeNull()
  const overlapWidth = Math.max(0, Math.min(bounds!.x + bounds!.width, targetBounds!.x + targetBounds!.width) - Math.max(bounds!.x, targetBounds!.x))
  const overlapHeight = Math.max(0, Math.min(bounds!.y + bounds!.height, targetBounds!.y + targetBounds!.height) - Math.max(bounds!.y, targetBounds!.y))
  expect(overlapWidth * overlapHeight).toBeLessThanOrEqual(1)
}

async function beginTour(page: Page) {
  await page.goto('/?token=pyruns-e2e-access-token')
  await page.getByRole('button', { name: '开始引导', exact: true }).click()
  await expectStep(page, 'home')
}

async function next(page: Page, target: string) {
  await popover(page).locator('.driver-popover-next-btn').click()
  await expectStep(page, target)
}

test('first visit offers a guide, dismissal persists, and Preferences can replay it', async ({ page }, testInfo) => {
  await page.goto('/?token=pyruns-e2e-access-token')
  const welcome = page.getByRole('dialog', { name: '欢迎使用 Pyruns', exact: true })
  await expect(welcome).toBeVisible()
  await expect(welcome.getByRole('button', { name: '开始引导', exact: true })).toBeFocused()
  await page.screenshot({ path: testInfo.outputPath('welcome.png') })
  await welcome.getByRole('button', { name: '稍后再说', exact: true }).click()
  await page.reload()
  await expect(page.getByRole('link', { name: '首页', exact: true })).toBeVisible()
  await expect(welcome).toHaveCount(0)
  await page.getByRole('button', { name: '偏好设置', exact: true }).click()
  await page.getByRole('dialog', { name: '偏好设置', exact: true }).getByRole('button', { name: '新手指南', exact: true }).click()
  await expect(welcome).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(welcome).toHaveCount(0)
  await expect(page.getByRole('button', { name: '偏好设置', exact: true })).toBeFocused()
})

test('a delayed guide stays cancellable and cannot start after dismissal', async ({ page }, testInfo) => {
  let releaseGuide!: () => void
  const gate = new Promise<void>(resolve => { releaseGuide = resolve })
  const guideAsset = /\/assets\/tour-[^/]+\.js(?:\?|$)/
  await page.route(guideAsset, async route => {
    await gate
    await route.continue()
  })
  try {
    await page.goto('/?token=pyruns-e2e-access-token')
    const welcome = page.getByRole('dialog', { name: '欢迎使用 Pyruns', exact: true })
    await welcome.getByRole('button', { name: '开始引导', exact: true }).click()
    await expect(welcome).toBeVisible()
    await expect(welcome.getByRole('button', { name: '正在加载引导...', exact: true })).toBeDisabled()
    await page.screenshot({ path: testInfo.outputPath('guide-loading.png') })
    await page.keyboard.press('Escape')
    await expect(welcome).toHaveCount(0)
    await page.getByRole('link', { name: '任务管理', exact: true }).click()
    const loaded = page.waitForResponse(guideAsset)
    releaseGuide()
    await (await loaded).finished()
    await expect(page).toHaveURL(/\/manager$/)
    await expect(popover(page)).toHaveCount(0)
    await page.getByRole('button', { name: '偏好设置', exact: true }).click()
    await page.getByRole('dialog', { name: '偏好设置', exact: true })
      .getByRole('button', { name: '新手指南', exact: true }).click()
    await welcome.getByRole('button', { name: '开始引导', exact: true }).click()
    await expectStep(page, 'home')
  } finally {
    releaseGuide()
  }
})

test('the interactive tour visits real pages, previews panels, and never mutates tasks or runtime', async ({ page }, testInfo) => {
  const mutations: string[] = []
  const errors: string[] = []
  page.on('pageerror', error => errors.push(error.message))
  page.on('request', request => {
    const path = new URL(request.url()).pathname
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(request.method())
      && path.startsWith('/api/') && !path.endsWith('/validate-path')) mutations.push(path)
  })
  await beginTour(page)
  await page.locator('[data-tour="home"]').click()
  await expectStep(page, 'workspace')
  await page.locator('[data-tour="workspace"]').click()
  await expectStep(page, 'workspace-picker')
  await expect(page.locator('[data-tour-panel="workspace"]')).toBeVisible()
  await page.screenshot({ path: testInfo.outputPath('workspace-preview.png') })
  await next(page, 'generator')
  await expect(page.locator('[data-tour-panel="workspace"]')).toHaveCount(0)
  await page.locator('[data-tour="generator"]').click()
  await expectStep(page, 'generator-controls')
  await expect(page).toHaveURL(/\/generator$/)
  await next(page, 'generator-create')
  await page.screenshot({ path: testInfo.outputPath('generator-create.png') })
  await popover(page).locator('.driver-popover-prev-btn').click()
  await expectStep(page, 'generator-controls')
  await next(page, 'generator-create')
  await next(page, 'manager')
  await page.locator('[data-tour="manager"]').click()
  await expectStep(page, 'manager-controls')
  await expect(page).toHaveURL(/\/manager$/)
  await next(page, 'monitor')
  await page.locator('[data-tour="monitor"]').click()
  await expectStep(page, 'monitor-controls')
  await expect(page).toHaveURL(/\/monitor$/)
  await page.screenshot({ path: testInfo.outputPath('monitor.png') })
  await next(page, 'runtime')
  await page.locator('[data-tour="runtime"]').click()
  await expectStep(page, 'runtime-controls')
  await expect(page.locator('[data-tour-panel="runtime"]')).toBeVisible()
  await page.keyboard.press('Tab')
  expect(await page.evaluate(() => Boolean(document.activeElement?.closest('.pyruns-tour')))).toBe(true)
  await next(page, 'language')
  await expect(page.locator('[data-tour-panel="runtime"]')).toHaveCount(0)
  await page.getByTestId('language-toggle').click()
  await expect(popover(page)).toContainText('Choose your language')
  await expect(page.locator('html')).toHaveAttribute('lang', 'en')
  await page.screenshot({ path: testInfo.outputPath('language-light.png') })
  await next(page, 'theme')
  await page.getByTestId('theme-toggle').click()
  await expect(page.locator('html')).toHaveClass('dark')
  await page.screenshot({ path: testInfo.outputPath('theme-dark.png') })
  await next(page, 'guide')
  await expect(popover(page)).toContainText('Replay the guide from Preferences')
  await next(page, 'preferences')
  await page.locator('[data-tour="preferences"]').click()
  await expect(popover(page)).toHaveCount(0)
  await expect(page.getByRole('dialog', { name: 'Preferences', exact: true })).toBeVisible()
  expect(await page.evaluate(key => localStorage.getItem(key), storageKey)).toBe('completed')
  expect(mutations).toEqual([])
  expect(errors).toEqual([])
  await page.getByRole('button', { name: 'Done', exact: true }).click()
  await page.reload()
  await expect(page.getByRole('link', { name: 'Home', exact: true })).toBeVisible()
  await expect(page.getByRole('dialog', { name: 'Welcome to Pyruns', exact: true })).toHaveCount(0)
})

test('back and Escape clean up preview panels and restore the Preferences focus', async ({ page }) => {
  await beginTour(page)
  await next(page, 'workspace')
  await next(page, 'workspace-picker')
  await popover(page).locator('.driver-popover-prev-btn').click()
  await expectStep(page, 'workspace')
  await expect(page.locator('[data-tour-panel="workspace"]')).toHaveCount(0)
  await next(page, 'workspace-picker')
  await page.keyboard.press('Escape')
  await expect(popover(page)).toHaveCount(0)
  await expect(page.locator('[data-tour-panel="workspace"]')).toHaveCount(0)
  await expect(page.locator('body')).not.toHaveClass(/driver-active/)
  await expect(page.getByRole('button', { name: '偏好设置', exact: true })).toBeFocused()
  await expect(page).not.toHaveURL(/launcher=/)
})

test('replaying the guide protects an unsaved generator draft', async ({ page }) => {
  await page.goto('/?token=pyruns-e2e-access-token')
  await page.getByRole('button', { name: '稍后再说', exact: true }).click()
  await page.getByRole('link', { name: '任务生成', exact: true }).click()
  const name = page.locator('#task-prefix')
  await name.fill('keep_my_draft')
  await page.getByRole('button', { name: '偏好设置', exact: true }).click()
  await page.getByRole('dialog', { name: '偏好设置', exact: true }).getByRole('button', { name: '新手指南', exact: true }).click()
  await expect(page.getByRole('button', { name: '开始引导', exact: true })).toBeDisabled()
  await expect(page.getByRole('dialog', { name: '欢迎使用 Pyruns', exact: true }).getByRole('status')).toContainText('你的草稿会保留在当前页面')
  await page.getByRole('button', { name: '关闭新手指南', exact: true }).click()
  await expect(page).toHaveURL(/\/generator$/)
  await expect(name).toHaveValue('keep_my_draft')
})

test('the guide still opens when browser storage is blocked', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'localStorage', { get() { throw new DOMException('Storage blocked', 'SecurityError') } })
  })
  await beginTour(page)
  await popover(page).getByRole('button', { name: '跳过引导', exact: true }).click()
  await expect(popover(page)).toHaveCount(0)
  await page.getByRole('button', { name: '偏好设置', exact: true }).click()
  await page.getByRole('dialog', { name: '偏好设置', exact: true }).getByRole('button', { name: '新手指南', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '欢迎使用 Pyruns', exact: true })).toBeVisible()
})

test('preferences can reopen the welcome dialog without leaving another modal underneath', async ({ page }) => {
  await page.goto('/?token=pyruns-e2e-access-token')
  await page.getByRole('button', { name: '稍后再说', exact: true }).click()
  await page.getByRole('button', { name: '偏好设置', exact: true }).click()
  await page.getByRole('dialog', { name: '偏好设置', exact: true }).getByRole('button', { name: '新手指南', exact: true }).click()
  await expect(page.getByRole('dialog', { name: '偏好设置', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '开始引导', exact: true })).toBeEnabled()
})

test('the welcome waits for a delayed initial route', async ({ page }) => {
  let releaseRoute!: () => void
  const ready = new Promise<void>(resolve => { releaseRoute = resolve })
  await page.route(/\/assets\/DashboardPage-[^/]+\.js$/, async route => {
    await ready
    await route.continue()
  })
  try {
    await page.goto('/?token=pyruns-e2e-access-token', { waitUntil: 'domcontentloaded' })
    await expect(page.getByText('正在加载工作区...', { exact: true })).toBeVisible()
    await expect(page.locator('[data-tour="home"]')).toBeVisible()
    await expect(page.getByRole('dialog', { name: '欢迎使用 Pyruns', exact: true })).toHaveCount(0)
    releaseRoute()
    await expect(page.getByRole('dialog', { name: '欢迎使用 Pyruns', exact: true })).toBeVisible()
  } finally {
    releaseRoute()
  }
})

test('session recovery takes priority over onboarding and clears the active tour', async ({ page }) => {
  let rejectWorkspace = true
  await page.route('**/api/workspace', route => rejectWorkspace
    ? route.fulfill({ status: 401, json: { detail: 'UI authentication required' } })
    : route.continue())
  await page.goto('/?token=pyruns-e2e-access-token')
  await expect(page.getByRole('heading', { name: '会话已过期', exact: true })).toBeVisible()
  await expect(page.getByRole('dialog', { name: '欢迎使用 Pyruns', exact: true })).toHaveCount(0)
  rejectWorkspace = false
  await page.getByRole('button', { name: '重新连接', exact: true }).click()
  await page.getByRole('button', { name: '开始引导', exact: true }).click()
  await expectStep(page, 'home')
  await page.route('**/api/runtime?refresh_providers=false', route => route.fulfill({ status: 401, json: { detail: 'UI authentication required' } }))
  for (const target of ['workspace', 'workspace-picker', 'generator', 'generator-controls', 'generator-create', 'manager', 'manager-controls', 'monitor', 'monitor-controls', 'runtime']) {
    await next(page, target)
  }
  await popover(page).locator('.driver-popover-next-btn').click()
  await expect(page.getByRole('alertdialog', { name: '会话已过期', exact: true })).toBeVisible()
  await expect(popover(page)).toHaveCount(0)
  await expect(page.locator('body')).not.toHaveClass(/driver-active/)
})
