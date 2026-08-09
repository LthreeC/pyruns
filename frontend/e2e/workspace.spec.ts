import { expect, test } from '@playwright/test'

test('launcher, navigation, and theme work without browser errors', async ({ page }) => {
  const browserErrors: string[] = []
  page.on('console', message => {
    if (message.type() === 'error') browserErrors.push(message.text())
  })
  page.on('pageerror', error => browserErrors.push(error.message))

  const response = await page.goto('/launcher?token=pyruns-e2e-access-token')
  expect(response?.ok()).toBe(true)
  await expect(page).toHaveTitle('Choose Workspace · Pyruns')
  await expect(page).toHaveURL(/\/launcher$/)

  const launcher = page.getByRole('dialog', { name: 'Launch Workspace' })
  await expect(launcher).toBeVisible()
  await expect(launcher.getByText('Choose a workspace type')).toBeVisible()

  await page.keyboard.press('Escape')
  await expect(launcher).toBeHidden()
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()

  const lightMode = page.getByRole('button', { name: 'Light Mode' })
  const darkMode = page.getByRole('button', { name: 'Dark Mode' })
  if (await lightMode.isVisible().catch(() => false)) {
    await lightMode.click()
    await expect(darkMode).toBeVisible()
  } else {
    await darkMode.click()
    await expect(lightMode).toBeVisible()
  }

  await page.getByRole('link', { name: 'Manager' }).click()
  await expect(page.getByRole('heading', { name: 'Task Manager' })).toBeVisible()
  expect(browserErrors).toEqual([])
})

test('opening the workspace launcher preserves unsaved runtime edits', async ({ page }) => {
  await page.goto('/manager?token=pyruns-e2e-access-token')
  await page.getByRole('button', { name: 'Runtime' }).click()
  await page.getByRole('button', { name: 'Path' }).click()

  const pythonPath = page.getByRole('textbox', { name: 'Python executable path' })
  await pythonPath.fill('D:\\tools\\python.exe')
  await expect(page.getByRole('tab', { name: /Python.*unsaved changes/ })).toBeVisible()

  await page.locator('[data-launcher-trigger="true"]').click()
  const discardDialog = page.getByRole('dialog', { name: 'Discard unsaved runtime changes?' })
  await expect(discardDialog).toBeVisible()
  await discardDialog.getByRole('button', { name: 'Cancel' }).click()

  const launcher = page.getByRole('dialog', { name: 'Launch Workspace' })
  await expect(launcher).toBeVisible()
  await launcher.getByRole('button', { name: 'Cancel' }).click()
  await expect(page.getByRole('dialog', { name: 'Runtime settings' })).toBeVisible()
  await expect(pythonPath).toHaveValue('D:\\tools\\python.exe')
})

test('launcher stays open while switching and ignores launch-history storage failures', async ({ page }) => {
  await page.addInitScript(() => {
    const originalSetItem = Storage.prototype.setItem
    Storage.prototype.setItem = function setItem(key: string, value: string) {
      if (key.startsWith('pyruns.launcher.history.')) {
        throw new DOMException('Storage is unavailable', 'QuotaExceededError')
      }
      return originalSetItem.call(this, key, value)
    }
  })

  let releaseRequest!: () => void
  const requestGate = new Promise<void>(resolve => { releaseRequest = resolve })
  await page.route('**/api/launcher/open-shell-root', async route => {
    await requestGate
    await route.continue()
  })

  await page.goto('/launcher?token=pyruns-e2e-access-token')
  const launcher = page.getByRole('dialog', { name: 'Launch Workspace' })
  await launcher.getByRole('button', { name: 'Shell' }).click()
  await launcher.getByRole('textbox').fill('.')
  const openButton = launcher.getByRole('button', { name: 'Open Folder Path' })
  await expect(openButton).toBeEnabled()
  await openButton.click()

  const cancelButton = launcher.getByRole('button', { name: 'Cancel' })
  await expect(cancelButton).toBeDisabled()
  await page.keyboard.press('Escape')
  await page.mouse.click(2, 2)
  await expect(launcher).toBeVisible()

  releaseRequest()
  await expect(page).toHaveURL(/\/generator$/)
  await expect(launcher).toBeHidden()
})

test('browser navigation cannot discard unsaved task details without confirmation', async ({ page, isMobile }) => {
  if (isMobile) {
    await page.setViewportSize({ width: 320, height: 568 })
  }
  const task = {
    name: 'alpha',
    status: 'pending',
    task_kind: 'shell',
    dir: '/tmp/alpha',
    config_file: '/tmp/alpha/task.sh',
    config_text: 'echo alpha',
    preview_text: 'echo alpha',
    created_at: '2026-08-09T00:00:00Z',
    run_index: 1,
    pinned: false,
    notes: '',
    env: {},
  }
  await page.route('**/api/tasks?*', route => route.fulfill({
    json: {
      items: [task],
      total: 1,
      offset: 0,
      limit: 50,
      has_more: false,
      status_counts: {
        pending: 1,
        queued: 0,
        running: 0,
        completed: 0,
        failed: 0,
        cancelled: 0,
      },
    },
  }))
  await page.route('**/api/tasks/alpha?*', route => route.fulfill({ json: task }))

  await page.goto('/?token=pyruns-e2e-access-token')
  await page.getByRole('link', { name: 'Manager' }).click()
  await page.getByRole('button', { name: 'Open details for alpha' }).click()
  if (isMobile) {
    const panel = page.getByRole('dialog', { name: 'Task details for alpha' })
    const box = await panel.boundingBox()
    expect(box?.width).toBeLessThanOrEqual(320)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  }
  await page.getByRole('tab', { name: 'Notes' }).click()
  const notes = page.getByPlaceholder('Add notes...')
  await notes.fill('keep this draft')

  await page.evaluate(() => window.history.back())
  const discardDialog = page.getByRole('dialog', { name: 'Discard unsaved task details?' })
  await expect(discardDialog).toBeVisible()
  await discardDialog.getByRole('button', { name: 'Cancel' }).click()

  await expect(page).toHaveURL(/\/manager$/)
  await expect(notes).toHaveValue('keep this draft')
})

test('mobile navigation and runtime controls stay touch friendly without overflow', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'Mobile viewport contract')
  await page.goto('/manager?token=pyruns-e2e-access-token')

  const sidebarTargets = [
    page.getByRole('link', { name: 'Home' }),
    page.getByRole('link', { name: 'Generator' }),
    page.getByRole('link', { name: 'Manager' }),
    page.getByRole('link', { name: 'Monitor' }),
    page.locator('[data-launcher-trigger="true"]'),
    page.getByRole('button', { name: 'Runtime' }),
    page.getByRole('button', { name: /Mode$/ }),
  ]
  for (const target of sidebarTargets) {
    const box = await target.boundingBox()
    expect(box?.width).toBeGreaterThanOrEqual(44)
    expect(box?.height).toBeGreaterThanOrEqual(44)
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)

  await page.getByRole('button', { name: 'Runtime' }).click()
  const runtimeTargets = [
    page.getByRole('tab', { name: 'Python' }),
    page.getByRole('tab', { name: 'Env' }),
    page.getByRole('tab', { name: 'GPU' }),
    page.getByRole('button', { name: 'Reload runtime' }),
    page.getByRole('button', { name: 'Close runtime panel' }),
    page.getByRole('button', { name: 'Follow' }),
    page.getByRole('button', { name: 'Conda', exact: true }),
    page.getByRole('button', { name: 'Path', exact: true }),
  ]
  for (const target of runtimeTargets) {
    const box = await target.boundingBox()
    expect(box?.width).toBeGreaterThanOrEqual(44)
    expect(box?.height).toBeGreaterThanOrEqual(44)
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})
