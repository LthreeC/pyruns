import { expect, test } from '@playwright/test'
import { mkdtemp, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

test.use({ locale: 'zh-CN' })

test('the saved theme loads before React under the server content security policy', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('pyruns_theme', 'dark'))
  await page.route(/\/assets\/index-[^/]+\.js$/, route => route.abort())
  await page.goto('/?token=pyruns-e2e-access-token')
  await expect(page.locator('html')).toHaveClass('dark')
  await expect(page.locator('#root')).toBeEmpty()
})

test('language and theme switch immediately and survive reload', async ({ page }) => {
  await page.goto('/?token=pyruns-e2e-access-token')
  await expect(page.getByRole('link', { name: '首页', exact: true })).toBeVisible()
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN')
  await expect(page).toHaveTitle('概览 · Pyruns')
  await page.evaluate(() => { (window as Window & { preferencesMarker?: string }).preferencesMarker = 'same-page' })

  await page.getByTestId('language-toggle').click()
  await expect(page.getByRole('link', { name: 'Home', exact: true })).toBeVisible()
  await expect(page).toHaveTitle('Dashboard · Pyruns')
  expect(await page.evaluate(() => (window as Window & { preferencesMarker?: string }).preferencesMarker)).toBe('same-page')

  await page.getByTestId('theme-toggle').click()
  await expect(page.locator('html')).toHaveClass('dark')
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('lang', 'en')
  await expect(page.locator('html')).toHaveClass('dark')
  await expect(page.getByRole('link', { name: 'Home', exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Preferences', exact: true }).click()
  const preferences = page.getByRole('dialog', { name: 'Preferences', exact: true })
  await preferences.getByRole('combobox', { name: 'Language', exact: true }).selectOption('zh-CN')
  await expect(page.getByRole('dialog', { name: '偏好设置', exact: true })).toBeVisible()
  await page.getByRole('button', { name: '日间', exact: true }).click()
  await expect(page.locator('html')).toHaveClass('light')
  await page.getByRole('button', { name: '完成', exact: true }).click()
  await page.reload()
  await expect(page.locator('html')).toHaveAttribute('lang', 'zh-CN')
  await expect(page.locator('html')).toHaveClass('light')
})

test('switching language keeps the shell draft and task data intact', async ({ page }) => {
  const root = await mkdtemp(join(tmpdir(), 'pyruns-preferences-'))
  await page.goto('/?token=pyruns-e2e-access-token')
  const original = await (await page.request.get('/api/workspace')).json()
  try {
    const opened = await page.request.post('/api/launcher/open-shell-root', { data: { path: root } })
    expect(opened.ok(), await opened.text()).toBe(true)
    for (const name of ['Running', '$t(Save)']) {
      const generated = await page.request.post('/api/generator/create', { data: {
        name_prefix: name, mode: 'shell', shell_text: 'echo original', append_timestamp: false,
      } })
      expect(generated.ok(), await generated.text()).toBe(true)
    }
    await page.goto('/generator')
    const editor = page.getByRole('textbox', { name: '任务 Shell 编辑器' })
    await editor.press('ControlOrMeta+A')
    await editor.pressSequentially('echo keep_original_command')
    await page.getByTestId('language-toggle').click()
    await expect(page.getByRole('textbox', { name: 'Task shell editor' })).toContainText('echo keep_original_command')
    await page.getByTestId('language-toggle').click()
    await expect(page.getByRole('textbox', { name: '任务 Shell 编辑器' })).toContainText('echo keep_original_command')
    // Clear the dirty draft before navigating to another view.
    await page.reload()
    await page.getByRole('link', { name: '任务管理', exact: true }).click()
    await expect(page.getByRole('heading', { name: '任务管理', exact: true })).toBeVisible()
    await expect(page.getByText('Running', { exact: true }).first()).toBeVisible()
    const more = page.getByRole('button', { name: '$t(Save) 的更多操作', exact: true })
    await expect(more).toBeVisible()
    await more.click()
    const taskActions = page.getByRole('group', { name: '$t(Save) 的任务操作', exact: true })
    await taskActions.getByRole('button', { name: '删除任务', exact: true }).click()
    const confirmation = page.getByRole('dialog')
    await expect(confirmation).toContainText('$t(Save)')
    await confirmation.getByRole('button', { name: '取消', exact: true }).click()
    await page.getByRole('link', { name: '日志监控', exact: true }).click()
    await expect(page.getByRole('heading', { name: '日志监控', exact: true })).toBeVisible()
  } finally {
    await page.request.post('/api/workspace/run-root', { data: { path: original.run_root } })
    await rm(root, { recursive: true, force: true, maxRetries: 3 })
  }
})
