import { expect, test } from '@playwright/test'

test('idle UI update confirms, waits for a new instance, and reloads', async ({ page, isMobile }) => {
  let restarted = false
  let updateChecks = 0
  let updateRequests = 0

  await page.route('**/api/system/info', route => route.fulfill({
    json: {
      version: restarted ? '0.4.0' : '0.3.0',
      installed_version: restarted ? '0.4.0' : '0.3.0',
      restart_required: false,
      instance_id: restarted ? 'new-instance' : 'old-instance',
      update_supported: true,
      update_state: 'idle',
      last_update: restarted
        ? {
            ok: true,
            previous_version: '0.3.0',
            installed_version: '0.4.0',
            exit_code: 0,
          }
        : null,
    },
  }))
  await page.route('**/api/system/update/check', route => {
    updateChecks += 1
    return route.fulfill({
      json: {
        current_version: '0.3.0',
        latest_version: '0.4.0',
        update_available: true,
      },
    })
  })
  await page.route('**/api/system/update', route => {
    updateRequests += 1
    restarted = true
    return route.fulfill({
      status: 202,
      json: {
        ok: true,
        instance_id: 'old-instance',
        version: '0.3.0',
        state: 'restarting',
      },
    })
  })

  await page.goto('/?token=pyruns-e2e-access-token')
  const updateButton = page.getByRole('button', { name: /Check for Pyruns updates.*0\.3\.0/ })
  const homeLink = page.getByRole('link', { name: 'Home' })
  await expect(updateButton).toBeVisible()
  const updateBox = await updateButton.boundingBox()
  const homeBox = await homeLink.boundingBox()
  const sidebarBox = await page.locator('aside').boundingBox()
  if (!updateBox || !homeBox || !sidebarBox) throw new Error('Sidebar controls must have stable layout boxes')
  expect(updateBox.width).toBeLessThanOrEqual(isMobile ? 44 : 80)
  expect(updateBox.height).toBeGreaterThanOrEqual(40)
  expect(updateBox.height).toBeLessThanOrEqual(44)
  expect(updateBox.x + updateBox.width).toBeLessThanOrEqual(sidebarBox.x + sidebarBox.width + 0.5)
  expect(updateBox.y).toBeLessThan(homeBox.y)
  await updateButton.click()
  const confirmation = page.getByRole('dialog', { name: 'Update Pyruns to v0.4.0?' })
  await expect(confirmation).toBeVisible()
  await confirmation.getByRole('button', { name: 'Update and Restart' }).click()

  await expect(page.getByRole('dialog', { name: 'Updating Pyruns' })).toBeVisible()
  await expect(page.getByText('Pyruns updated')).toBeVisible()
  await expect(page.getByRole('button', { name: /Check for Pyruns updates.*0\.4\.0/ })).toBeVisible()
  expect(updateChecks).toBe(1)
  expect(updateRequests).toBe(1)
})

test('update control reports the current PyPI release without restarting', async ({ page }) => {
  let updateRequests = 0
  let releaseVersionCheck!: () => void
  const versionCheckGate = new Promise<void>(resolve => {
    releaseVersionCheck = resolve
  })
  await page.route('**/api/system/info', route => route.fulfill({
    json: {
      version: '0.3.0',
      installed_version: '0.3.0',
      restart_required: false,
      instance_id: 'current-instance',
      update_supported: true,
      update_state: 'idle',
      last_update: null,
    },
  }))
  await page.route('**/api/system/update/check', async route => {
    await versionCheckGate
    return route.fulfill({
      json: {
        current_version: '0.3.0',
        latest_version: '0.3.0',
        update_available: false,
      },
    })
  })
  await page.route('**/api/system/update', route => {
    updateRequests += 1
    return route.fulfill({ status: 500, json: { detail: 'Update should not start' } })
  })

  await page.goto('/?token=pyruns-e2e-access-token')
  await page.getByRole('button', { name: /Check for Pyruns updates.*0\.3\.0/ }).click()
  await expect(page.getByRole('button', { name: 'Checking for Pyruns updates' })).toBeDisabled()
  releaseVersionCheck()

  await expect(page.getByText('Pyruns is up to date')).toBeVisible()
  await expect(page.getByText('v0.3.0 is the latest version on PyPI.')).toBeVisible()
  expect(updateRequests).toBe(0)
})

test('external package change waits for a manual shared restart', async ({ page }) => {
  let restarted = false
  let restartRequests = 0
  let updateChecks = 0

  await page.route('**/api/system/info', route => route.fulfill({
    json: {
      version: restarted ? '0.4.0' : '0.3.0',
      installed_version: '0.4.0',
      restart_required: !restarted,
      instance_id: restarted ? 'restarted-instance' : 'stale-instance',
      update_supported: true,
      update_state: restarted ? 'idle' : 'restart_required',
      last_update: null,
    },
  }))
  await page.route('**/api/system/update/check', route => {
    updateChecks += 1
    return route.fulfill({ status: 500, json: { detail: 'Version check should not run' } })
  })
  await page.route('**/api/system/restart', route => {
    restartRequests += 1
    restarted = true
    return route.fulfill({
      status: 202,
      json: {
        ok: true,
        instance_id: 'stale-instance',
        version: '0.3.0',
        state: 'restarting',
      },
    })
  })

  await page.goto('/?token=pyruns-e2e-access-token')
  const restartButton = page.getByRole('button', {
    name: /Restart Pyruns to load installed version 0\.4\.0/,
  })
  await expect(restartButton).toBeVisible()
  await restartButton.click()

  const confirmation = page.getByRole('dialog', { name: 'Restart Pyruns to load v0.4.0?' })
  await expect(confirmation).toBeVisible()
  await confirmation.getByRole('button', { name: 'Restart Interfaces' }).click()

  await expect(page.getByRole('dialog', { name: 'Restarting Pyruns' })).toBeVisible()
  await expect(page.getByRole('button', { name: /Check for Pyruns updates.*0\.4\.0/ })).toBeVisible()
  expect(restartRequests).toBe(1)
  expect(updateChecks).toBe(0)
})

test('a shared backend restart reloads a non-initiating interface', async ({ page }) => {
  let restarted = false
  await page.route('**/api/system/info', route => route.fulfill({
    json: {
      version: restarted ? '0.4.0' : '0.3.0',
      installed_version: restarted ? '0.4.0' : '0.3.0',
      restart_required: false,
      instance_id: restarted ? 'shared-new-instance' : 'shared-old-instance',
      update_supported: true,
      update_state: 'idle',
      last_update: null,
    },
  }))

  await page.goto('/?token=pyruns-e2e-access-token')
  await expect(page.getByRole('button', { name: /Check for Pyruns updates.*0\.3\.0/ })).toBeVisible()

  restarted = true
  const reloaded = page.waitForEvent('load')
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await reloaded

  await expect(page.getByRole('button', { name: /Check for Pyruns updates.*0\.4\.0/ })).toBeVisible()
})

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
  const managerSearch = page.getByRole('textbox', { name: 'Search tasks' })
  await expect(managerSearch).toHaveJSProperty('tagName', 'INPUT')

  await page.getByRole('link', { name: 'Monitor' }).click()
  const monitorSearch = page.getByRole('textbox', { name: 'Search monitor tasks' })
  await expect(monitorSearch).toHaveJSProperty('tagName', 'INPUT')
  expect(await monitorSearch.evaluate(element => element.scrollHeight <= element.clientHeight)).toBe(true)
  expect(browserErrors).toEqual([])
})

test('monitor terminal search stays responsive with a large scrollback buffer', async ({ page }) => {
  const lineCount = 12_050
  const logContent = Array.from(
    { length: lineCount },
    (_, index) => `terminal line ${String(index).padStart(5, '0')}${index === lineCount - 1 ? ' terminal-target' : ''}`,
  ).join('\n')
  const task = {
    name: 'large-log',
    status: 'completed',
    task_kind: 'shell',
    dir: '/tmp/large-log',
    config_file: '/tmp/large-log/task.sh',
    config: {},
    config_text: '',
    created_at: '2026-08-23T00:00:00Z',
    run_index: 1,
    pinned: false,
    env: {},
    start_times: [],
    finish_times: [],
    pids: [],
    durations: [],
    exit_codes: [],
    source_states: [],
    records: [],
    tracks: [],
    notes: '',
    preview_text: '',
    search_text: '',
  }

  await page.route('**/api/tasks?*', route => route.fulfill({ json: {
    items: [task],
    total: 1,
    offset: 0,
    limit: 200,
    has_more: false,
    status_counts: {
      pending: 0,
      queued: 0,
      running: 0,
      completed: 1,
      failed: 0,
      cancelled: 0,
    },
  } }))
  await page.route('**/api/tasks/large-log/logs?*', route => route.fulfill({ json: {
    task_name: 'large-log',
    selected_log: 'run1.log',
    available_logs: ['run1.log'],
    content: logContent,
    offset: logContent.length,
    log_identity: 'large-log-v1',
    tail_truncated: false,
    tail_limit_bytes: 0,
  } }))

  await page.goto('/monitor?token=pyruns-e2e-access-token')
  const terminal = page.getByRole('region', { name: 'Read-only logs for large-log' })
  await expect(terminal.locator('.xterm-rows')).toContainText('terminal-target', { timeout: 10_000 })
  await terminal.click()
  await page.keyboard.press('Control+f')

  const search = page.getByRole('textbox', { name: 'Search terminal logs' })
  await expect(search).toBeFocused()
  const typingStarted = Date.now()
  await search.pressSequentially('terminal-target')
  expect(Date.now() - typingStarted).toBeLessThan(1_000)
  await expect(search).toHaveValue('terminal-target')

  const searchForm = page.getByRole('search')
  await expect(searchForm.getByRole('status')).toHaveText('Match', { timeout: 3_000 })
  await page.keyboard.press('F3')
  await expect(searchForm.getByRole('status')).toHaveText('Match')
  await page.keyboard.press('Escape')
  await expect(search).toBeHidden()
  await expect(page.getByRole('textbox', { name: 'Read-only task log output' })).toBeFocused()
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
  await page.keyboard.press('Escape')

  const launcher = page.getByRole('dialog', { name: 'Launch Workspace' })
  await expect(launcher).toBeVisible()
  await launcher.getByRole('button', { name: 'Cancel' }).click()
  await expect(page.getByRole('dialog', { name: 'Runtime settings' })).toBeVisible()
  await expect(pythonPath).toHaveValue('D:\\tools\\python.exe')
})

test('recovering an expired session preserves unsaved runtime edits', async ({ page }) => {
  await page.goto('/manager?token=pyruns-e2e-access-token')
  await page.getByRole('button', { name: 'Runtime' }).click()
  const runtimePanel = page.getByRole('dialog', { name: 'Runtime settings' })
  await runtimePanel.getByRole('button', { name: 'Path' }).click()

  const pythonPath = runtimePanel.getByRole('textbox', { name: 'Python executable path' })
  await pythonPath.fill('D:\\drafts\\python.exe')
  await page.route('**/api/runtime?*', async route => {
    if (route.request().method() === 'PATCH') {
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Unauthorized' }),
      })
      return
    }
    await route.continue()
  })

  await runtimePanel.getByRole('button', { name: 'Save', exact: true }).click()
  const recovery = page.getByRole('alertdialog', { name: 'Session expired' })
  await expect(recovery).toBeVisible()
  const retryConnection = recovery.getByRole('button', { name: 'Retry connection' })
  await expect(retryConnection).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(retryConnection).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(retryConnection).toBeFocused()
  await retryConnection.click()

  await expect(recovery).toBeHidden()
  await expect(runtimePanel).toBeVisible()
  await expect(pythonPath).toHaveValue('D:\\drafts\\python.exe')
  expect(await runtimePanel.evaluate(panel => panel.contains(document.activeElement))).toBe(true)
})

test('reconnect requires explicit discard when the server workspace changed', async ({ page }) => {
  let returnChangedWorkspace = false
  let changedWorkspace: Record<string, unknown> | null = null
  await page.route('**/api/workspace', async route => {
    if (!returnChangedWorkspace || !changedWorkspace) {
      await route.continue()
      return
    }
    await route.fulfill({ json: changedWorkspace })
  })

  const initialWorkspaceResponse = page.waitForResponse(response => (
    new URL(response.url()).pathname === '/api/workspace'
      && response.request().method() === 'GET'
  ))
  await page.goto('/manager?token=pyruns-e2e-access-token')
  const workspace = await (await initialWorkspaceResponse).json()
  changedWorkspace = {
    ...workspace,
    run_root: `${workspace.run_root}-changed`,
  }
  await page.getByRole('button', { name: 'Runtime' }).click()
  const runtimePanel = page.getByRole('dialog', { name: 'Runtime settings' })
  await runtimePanel.getByRole('button', { name: 'Path' }).click()
  const pythonPath = runtimePanel.getByRole('textbox', { name: 'Python executable path' })
  await pythonPath.fill('D:\\drafts\\workspace-change.exe')

  await page.route('**/api/runtime?*', async route => {
    if (route.request().method() === 'PATCH') {
      await route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Unauthorized' }),
      })
      return
    }
    await route.continue()
  })

  await runtimePanel.getByRole('button', { name: 'Save', exact: true }).click()
  const expired = page.getByRole('alertdialog', { name: 'Session expired' })
  await expect(expired).toBeVisible()
  returnChangedWorkspace = true
  await expired.getByRole('button', { name: 'Retry connection' }).click()

  const changed = page.getByRole('alertdialog', { name: 'Workspace changed' })
  await expect(changed).toBeVisible()
  await expect(page.locator('input[aria-label="Python executable path"]'))
    .toHaveValue('D:\\drafts\\workspace-change.exe')
  await expect(page.locator('dialog[open]')).toHaveCount(0)
  const discard = changed.getByRole('button', { name: 'Discard drafts and reconnect' })
  await expect(discard).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(discard).toBeFocused()
  await discard.click()
  await expect(changed).toBeHidden()
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
  const browserErrors: string[] = []
  page.on('console', message => {
    if (message.type() === 'error') browserErrors.push(message.text())
  })
  page.on('pageerror', error => browserErrors.push(error.message))

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
  const notes = page.getByRole('textbox', { name: 'Task notes' })
  await notes.fill('keep this draft')

  await page.evaluate(() => window.history.back())
  const discardDialog = page.getByRole('dialog', { name: 'Discard unsaved task details?' })
  await expect(discardDialog).toBeVisible()
  await discardDialog.getByRole('button', { name: 'Cancel' }).click()

  await expect(page).toHaveURL(/\/manager$/)
  await expect(notes).toHaveValue('keep this draft')
  expect(browserErrors).toEqual([])
})

test('monitor task details stay stable after the full task loads', async ({ page }) => {
  const browserErrors: string[] = []
  page.on('console', message => {
    if (message.type() === 'error') browserErrors.push(message.text())
  })
  page.on('pageerror', error => browserErrors.push(error.message))

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
    notes: 'keep monitor notes',
    env: { MODE: 'review' },
    start_times: ['2026-08-09T00:01:00Z'],
    finish_times: [],
    pids: [1234],
    durations: [],
    exit_codes: [],
    source_states: ['clean'],
    records: [{ loss: 0.5 }],
    tracks: [{ step: 1 }],
  }
  const compactTask = {
    ...task,
    config: {},
    config_text: '',
    env: {},
    start_times: [],
    finish_times: [],
    pids: [],
    durations: [],
    exit_codes: [],
    source_states: [],
    records: [],
    tracks: [],
    notes: '',
    preview_text: '',
    search_text: '',
  }
  await page.route('**/api/tasks?*', route => {
    const query = new URL(route.request().url()).searchParams.get('query') || ''
    const visibleTasks = query === 'hidden' ? [] : [{
      ...compactTask,
      search_matches: query ? [
        {
          field: 'name',
          location: '',
          snippet: 'alpha',
          match_start: 0,
          match_end: 5,
        },
        {
          field: 'notes',
          location: 'Line 2',
          snippet: 'Review alpha before launch',
          match_start: 7,
          match_end: 12,
        },
      ] : undefined,
      search_match_count: query ? 5 : undefined,
    }]
    return route.fulfill({ json: {
      items: visibleTasks,
      total: visibleTasks.length,
      offset: 0,
      limit: 200,
      has_more: false,
      status_counts: {
        pending: 1,
        queued: 0,
        running: 0,
        completed: 0,
        failed: 0,
        cancelled: 0,
      },
    } })
  })
  await page.route('**/api/tasks/alpha/logs?*', route => route.fulfill({
    json: {
      task_name: 'alpha',
      selected_log: '',
      available_logs: [],
      content: '',
      offset: 0,
      log_identity: '',
      tail_truncated: false,
      tail_limit_bytes: 0,
    },
  }))
  await page.route('**/api/tasks/alpha?*', route => route.fulfill({ json: task }))

  await page.goto('/monitor?token=pyruns-e2e-access-token')
  await page.getByRole('button', { name: 'View alpha, pending' }).click()
  await page.keyboard.press('Control+Shift+F')
  await expect(page.getByRole('textbox', { name: 'Search monitor tasks' })).toBeFocused()
  await page.getByRole('button', { name: 'View Details' }).click()
  await expect(page.getByRole('dialog', { name: 'Task details for alpha' })).toBeVisible()
  await page.getByRole('tab', { name: 'Notes' }).click()
  const notes = page.getByRole('textbox', { name: 'Task notes' })
  await expect(notes).toHaveValue('keep monitor notes')
  await notes.fill('local monitor draft')

  const hiddenTasks = page.waitForResponse(response => {
    const url = new URL(response.url())
    return url.pathname === '/api/tasks' && url.searchParams.get('query') === 'hidden'
  })
  await page.getByRole('textbox', { name: 'Search monitor tasks' }).fill('hidden')
  await hiddenTasks
  await expect(page.getByRole('dialog', { name: 'Task details for alpha' })).toBeVisible()
  await expect(notes).toHaveValue('local monitor draft')
  const monitorSidebar = page.getByRole('complementary', { name: 'Task monitor sidebar' })
  await expect(monitorSidebar.getByText('Current Task', { exact: true })).toHaveCount(0)
  await expect(monitorSidebar.getByText('Search Results', { exact: true })).toBeVisible()

  const refreshedTasks = page.waitForResponse(response => {
    const url = new URL(response.url())
    return url.pathname === '/api/tasks' && url.searchParams.get('query') === 'alpha'
  })
  await page.getByRole('textbox', { name: 'Search monitor tasks' }).fill('alpha')
  await refreshedTasks
  await expect(notes).toHaveValue('local monitor draft')
  await expect(monitorSidebar.getByText('5 matches in 1 task', { exact: true })).toBeVisible()
  const nameMatch = monitorSidebar.getByRole('button', { name: /View Name match in alpha:/ })
  const notesMatch = monitorSidebar.getByRole('button', { name: /View Notes match in alpha at Line 2:/ })
  await expect(nameMatch.getByText('Name', { exact: true })).toBeVisible()
  await expect(nameMatch.locator('mark')).toHaveText('alpha')
  await expect(notesMatch.getByText('Notes: Line 2', { exact: true })).toBeVisible()
  await expect(notesMatch.locator('mark')).toHaveText('alpha')
  await expect(monitorSidebar.getByText('+3 more matches in this task', { exact: true })).toBeVisible()

  await page.getByRole('tab', { name: 'Env' }).click()
  await expect(page.getByRole('textbox', { name: 'Environment variable key' })).toHaveValue('MODE')
  await expect(page.getByRole('textbox', { name: 'Environment variable value' })).toHaveValue('review')
  await page.getByRole('tab', { name: 'Info' }).click()
  await expect(page.getByText('2026-08-09T00:01:00Z')).toBeVisible()
  await expect(page.getByText('1234')).toBeVisible()

  expect(browserErrors).toEqual([])
})

test('task detail conflicts and session recovery preserve local drafts', async ({ page }) => {
  let latestNotes = ''
  let latestEnv: Record<string, string> = {}
  let rejectEnvWith401 = false
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
  }
  const taskPayload = () => ({ ...task, notes: latestNotes, env: latestEnv })
  await page.route('**/api/tasks?*', route => route.fulfill({
    json: {
      items: [taskPayload()],
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
  await page.route('**/api/tasks/alpha?*', route => route.fulfill({ json: taskPayload() }))
  const writes: { notes: string; expected_notes: string }[] = []
  await page.route('**/api/tasks/alpha/notes', async route => {
    const payload = route.request().postDataJSON() as { notes: string; expected_notes: string }
    writes.push(payload)
    if (writes.length === 1) {
      latestNotes = 'newer remote notes'
      return route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Task notes changed since they were loaded.' }),
      })
    }
    latestNotes = payload.notes
    const savedTask = taskPayload()
    latestNotes = 'newer remote notes'
    return route.fulfill({ json: { ok: true, task: savedTask } })
  })
  const envWrites: { env: Record<string, string>; expected_env: Record<string, string> }[] = []
  await page.route('**/api/tasks/alpha/env', async route => {
    if (rejectEnvWith401) {
      return route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'UI authentication required' }),
      })
    }
    const payload = route.request().postDataJSON() as {
      env: Record<string, string>
      expected_env: Record<string, string>
    }
    envWrites.push(payload)
    if (envWrites.length === 1) {
      latestEnv = { REMOTE: 'newer' }
      return route.fulfill({
        status: 409,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Task environment changed since it was loaded.' }),
      })
    }
    latestEnv = payload.env
    const savedTask = taskPayload()
    latestEnv = { REMOTE: 'newer' }
    return route.fulfill({ json: { ok: true, task: savedTask } })
  })

  await page.goto('/manager?token=pyruns-e2e-access-token')
  await page.getByRole('button', { name: 'Open details for alpha' }).click()
  await page.getByRole('tab', { name: 'Notes' }).click()
  const notes = page.getByRole('textbox', { name: 'Task notes' })
  await notes.fill('local draft')
  await page.getByRole('button', { name: 'Save Notes' }).click()

  const conflictAlert = page.getByText('Another editor saved newer notes. Your draft is unchanged.')
  await expect(conflictAlert).toBeVisible()
  await expect(notes).toHaveValue('local draft')
  await expect(page.getByRole('button', { name: 'Replace Notes' })).toBeVisible()
  expect(writes).toEqual([{ notes: 'local draft', expected_notes: '' }])

  await page.getByRole('button', { name: 'Replace Notes' }).click()
  await expect(page.getByRole('button', { name: 'Save Notes' })).toBeVisible()
  await expect(conflictAlert).toBeHidden()
  await expect(notes).toHaveValue('newer remote notes')
  expect(writes).toEqual([
    { notes: 'local draft', expected_notes: '' },
    { notes: 'local draft', expected_notes: 'newer remote notes' },
  ])

  await page.getByRole('tab', { name: 'Env' }).click()
  await page.getByRole('button', { name: 'Add environment variable' }).click()
  const envKey = page.getByRole('textbox', { name: 'Environment variable key' })
  const envValue = page.getByRole('textbox', { name: 'Environment variable value' })
  await envKey.fill('LOCAL')
  await envValue.fill('draft')
  await page.getByRole('button', { name: 'Save', exact: true }).click()

  const envConflict = page.getByText('Another editor saved newer environment variables. Your draft is unchanged.')
  await expect(envConflict).toBeVisible()
  await expect(envKey).toHaveValue('LOCAL')
  await expect(envValue).toHaveValue('draft')
  expect(envWrites).toEqual([{ env: { LOCAL: 'draft' }, expected_env: {} }])

  await page.getByRole('button', { name: 'Replace Env' }).click()
  await expect(envConflict).toBeHidden()
  await expect(envKey).toHaveValue('REMOTE')
  await expect(envValue).toHaveValue('newer')
  expect(envWrites).toEqual([
    { env: { LOCAL: 'draft' }, expected_env: {} },
    { env: { LOCAL: 'draft' }, expected_env: { REMOTE: 'newer' } },
  ])

  await envKey.fill('LOCAL')
  await envValue.fill('draft after expiry')
  rejectEnvWith401 = true
  await page.getByRole('button', { name: 'Save', exact: true }).click()
  const recovery = page.getByRole('alertdialog', { name: 'Session expired' })
  await expect(recovery).toBeVisible()
  await recovery.getByRole('button', { name: 'Retry connection' }).click()

  await expect(recovery).toBeHidden()
  await expect(page.getByRole('dialog', { name: 'Task details for alpha' })).toBeVisible()
  await expect(envKey).toHaveValue('LOCAL')
  await expect(envValue).toHaveValue('draft after expiry')
  expect(await page.getByRole('dialog', { name: 'Task details for alpha' })
    .evaluate(panel => panel.contains(document.activeElement))).toBe(true)
})

test('task detail only offers replacement after loading the newer value', async ({ page }) => {
  let latestNotes = ''
  let latestEnv: Record<string, string> = {}
  let failNextDetailRead = false
  let notesWrites = 0
  let envWrites = 0
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
  }
  const taskPayload = () => ({ ...task, notes: latestNotes, env: latestEnv })

  await page.route('**/api/tasks?*', route => route.fulfill({
    json: {
      items: [taskPayload()],
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
  await page.route('**/api/tasks/alpha?*', route => {
    if (failNextDetailRead) {
      failNextDetailRead = false
      return route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Task detail temporarily unavailable.' }),
      })
    }
    return route.fulfill({ json: taskPayload() })
  })
  await page.route('**/api/tasks/alpha/notes', route => {
    notesWrites += 1
    latestNotes = 'newer remote notes'
    failNextDetailRead = notesWrites === 1
    return route.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Task notes changed since they were loaded.' }),
    })
  })
  await page.route('**/api/tasks/alpha/env', route => {
    envWrites += 1
    latestEnv = { REMOTE: 'newer' }
    failNextDetailRead = envWrites === 1
    return route.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'Task environment changed since it was loaded.' }),
    })
  })

  await page.goto('/manager?token=pyruns-e2e-access-token')
  await page.getByRole('button', { name: 'Open details for alpha' }).click()
  await page.getByRole('tab', { name: 'Notes' }).click()
  const notes = page.getByRole('textbox', { name: 'Task notes' })
  await notes.fill('local draft')
  await page.getByRole('button', { name: 'Save Notes' }).click()

  await expect(page.getByText(
    'Newer notes exist, but their latest version could not be loaded. Your draft is safe. Retry Save Notes before replacing anything.',
  )).toBeVisible()
  await expect(notes).toHaveValue('local draft')
  await expect(page.getByRole('button', { name: 'Replace Notes' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Save Notes' })).toBeVisible()

  await page.getByRole('button', { name: 'Save Notes' }).click()
  await expect(page.getByText('Another editor saved newer notes. Your draft is unchanged.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Replace Notes' })).toBeVisible()

  await page.getByRole('tab', { name: 'Env' }).click()
  await page.getByRole('button', { name: 'Add environment variable' }).click()
  const envKey = page.getByRole('textbox', { name: 'Environment variable key' })
  const envValue = page.getByRole('textbox', { name: 'Environment variable value' })
  await envKey.fill('LOCAL')
  await envValue.fill('draft')
  await page.getByRole('button', { name: 'Save', exact: true }).click()

  await expect(page.getByText(
    'Newer environment variables exist, but their latest version could not be loaded. Your draft is safe. Retry Save before replacing anything.',
  )).toBeVisible()
  await expect(envKey).toHaveValue('LOCAL')
  await expect(envValue).toHaveValue('draft')
  await expect(page.getByRole('button', { name: 'Replace Env' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Save', exact: true }).click()
  await expect(page.getByText('Another editor saved newer environment variables. Your draft is unchanged.')).toBeVisible()
  await expect(page.getByRole('button', { name: 'Replace Env' })).toBeVisible()
})

test('mobile navigation and runtime controls stay touch friendly without overflow', async ({ page, isMobile }) => {
  test.skip(!isMobile, 'Mobile viewport contract')
  await page.setViewportSize({ width: 375, height: 667 })
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

  await page.locator('[data-launcher-trigger="true"]').click()
  const launcher = page.getByRole('dialog', { name: 'Launch Workspace' })
  await launcher.getByRole('button', { name: 'Python', exact: true }).click()
  const launcherTargets = [
    launcher.getByRole('button', { name: 'Python', exact: true }),
    launcher.getByRole('button', { name: 'Shell', exact: true }),
    launcher.getByRole('textbox', { name: 'Python script path' }),
    launcher.getByRole('button', { name: /Browse/ }),
    launcher.getByRole('button', { name: 'Select Script Path' }),
    launcher.getByRole('button', { name: 'Cancel' }),
  ]
  for (const target of launcherTargets) {
    const box = await target.boundingBox()
    expect(box?.width).toBeGreaterThanOrEqual(44)
    expect(box?.height).toBeGreaterThanOrEqual(44)
  }
  const launcherInputFontSize = await launcher
    .getByRole('textbox', { name: 'Python script path' })
    .evaluate(element => Number.parseFloat(window.getComputedStyle(element).fontSize))
  expect(launcherInputFontSize).toBeGreaterThanOrEqual(16)
  await launcher.getByRole('button', { name: 'Cancel' }).click()

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
  await page.getByRole('tab', { name: 'GPU' }).click()
  const gpuScheduler = page.getByRole('switch', { name: 'GPU scheduling' })
  const gpuSchedulerBox = await gpuScheduler.boundingBox()
  expect(gpuSchedulerBox?.width).toBeGreaterThanOrEqual(44)
  expect(gpuSchedulerBox?.height).toBeGreaterThanOrEqual(44)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)

  await page.getByRole('button', { name: 'Close runtime panel' }).click()
  await page.getByRole('link', { name: 'Generator' }).click()
  const taskPrefix = page.getByRole('textbox', { name: 'Task Prefix' })
  const editorViewButtons = await page.locator('[aria-label="Editor view"] button').all()
  const generatorTargets = [
    page.locator('button[aria-haspopup="listbox"]').first(),
    ...editorViewButtons,
    taskPrefix,
    page.locator('label').filter({ hasText: 'Append timestamp' }),
    page.getByRole('button', { name: /^(Create Shell Task|Generate Tasks|Preview Batch Tasks)$/ }),
  ]
  for (const target of generatorTargets) {
    const box = await target.boundingBox()
    expect(box?.width).toBeGreaterThanOrEqual(44)
    expect(box?.height).toBeGreaterThanOrEqual(44)
  }
  const taskPrefixFontSize = await taskPrefix.evaluate(
    element => Number.parseFloat(window.getComputedStyle(element).fontSize),
  )
  expect(taskPrefixFontSize).toBeGreaterThanOrEqual(16)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)

  await page.getByRole('link', { name: 'Monitor' }).click()
  const monitorSearch = page.getByRole('textbox', { name: 'Search monitor tasks' })
  const monitorSearchBox = await monitorSearch.boundingBox()
  expect(monitorSearchBox?.height).toBeGreaterThanOrEqual(44)
  const monitorSearchFontSize = await monitorSearch.evaluate(
    element => Number.parseFloat(window.getComputedStyle(element).fontSize),
  )
  expect(monitorSearchFontSize).toBeGreaterThanOrEqual(16)
  const exportBox = await page.getByRole('button', { name: 'Export' }).boundingBox()
  expect(exportBox?.height).toBeGreaterThanOrEqual(44)
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)

  await page.setViewportSize({ width: 667, height: 375 })
  await page.getByRole('link', { name: 'Manager' }).click()
  const landscapeSearch = page.getByRole('textbox', { name: 'Search tasks' })
  const landscapeSearchBox = await landscapeSearch.boundingBox()
  expect(landscapeSearchBox?.height).toBeGreaterThanOrEqual(44)
  expect(await landscapeSearch.evaluate(
    element => Number.parseFloat(window.getComputedStyle(element).fontSize),
  )).toBeGreaterThanOrEqual(16)
  await page.getByRole('button', { name: 'Runtime' }).click()
  for (const target of [
    page.getByRole('tab', { name: 'Python' }),
    page.getByRole('tab', { name: 'Env' }),
    page.getByRole('tab', { name: 'GPU' }),
    page.getByRole('button', { name: 'Reload runtime' }),
    page.getByRole('button', { name: 'Close runtime panel' }),
  ]) {
    const box = await target.boundingBox()
    expect(box?.width).toBeGreaterThanOrEqual(44)
    expect(box?.height).toBeGreaterThanOrEqual(44)
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
})

test('authentication failures show a recoverable session screen', async ({ page }) => {
  let rejectWorkspace = true
  await page.route('**/api/workspace', route => {
    if (rejectWorkspace) {
      return route.fulfill({
        status: 401,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'UI authentication required' }),
      })
    }
    return route.continue()
  })

  await page.goto('/?token=pyruns-e2e-access-token')
  await expect(page.getByRole('heading', { name: 'Session expired' })).toBeVisible()
  await expect(page).toHaveTitle('Session expired · Pyruns')
  await expect(page.getByText('This browser no longer has access')).toBeVisible()

  rejectWorkspace = false
  await page.getByRole('button', { name: 'Retry connection' }).click()
  await expect(page.getByRole('heading', { name: 'Dashboard' })).toBeVisible()

  await page.route('**/api/runtime', route => route.fulfill({
    status: 401,
    contentType: 'application/json',
    body: JSON.stringify({ detail: 'UI authentication required' }),
  }))
  await page.getByRole('button', { name: 'Runtime' }).click()
  await expect(page.getByRole('heading', { name: 'Session expired' })).toBeVisible()
})

test('template picker keeps the current option active when reopened', async ({ page }) => {
  await page.addInitScript(() => {
    const testWindow = window as typeof window & { __templateScrollTargets?: string[] }
    testWindow.__templateScrollTargets = []
    Element.prototype.scrollIntoView = function scrollIntoView() {
      testWindow.__templateScrollTargets?.push((this as HTMLElement).id)
    }
  })
  const templates = [
    { value: 'task-a', label: 'Task A' },
    { value: 'task-b', label: 'Task B' },
  ]
  await page.route('**/api/templates', route => route.fulfill({ json: { items: templates } }))
  await page.route('**/api/templates/content?*', route => {
    const value = new URL(route.request().url()).searchParams.get('value') || ''
    const label = templates.find(option => option.value === value)?.label || value
    return route.fulfill({
      json: {
        value,
        label,
        path: `/tmp/${value}.sh`,
        content: `echo ${value}`,
        read_only: false,
        mode_hint: 'shell',
        parsed_config: null,
      },
    })
  })

  await page.goto('/generator?token=pyruns-e2e-access-token')
  await expect(page.getByRole('textbox', { name: 'Task Prefix' })).toBeVisible()
  await page.getByRole('button', { name: /^(Load task|Select template|Task A)$/ }).click()
  await page.getByRole('option', { name: 'Task B' }).click()
  await expect(page.getByRole('button', { name: 'Task B' })).toBeVisible()

  await page.getByRole('button', { name: 'Task B' }).click()
  const templateSearch = page.getByRole('combobox', { name: /^Search (tasks|templates)$/ })
  const options = page.getByRole('option')
  await expect(options).toHaveCount(3)
  expect(await options.evaluateAll(elements => elements.every(element => element.tabIndex === -1))).toBe(true)

  await templateSearch.press('Home')
  await expect(templateSearch).toHaveAttribute('aria-activedescendant', /option-0$/)
  await expect.poll(() => page.evaluate(() => (
    window as typeof window & { __templateScrollTargets?: string[] }
  ).__templateScrollTargets?.at(-1))).toMatch(/option-0$/)

  await templateSearch.press('End')
  await expect(templateSearch).toHaveAttribute('aria-activedescendant', /option-2$/)
  await expect.poll(() => page.evaluate(() => (
    window as typeof window & { __templateScrollTargets?: string[] }
  ).__templateScrollTargets?.at(-1))).toMatch(/option-2$/)

  await templateSearch.press('Enter')
  await expect(page.getByRole('button', { name: 'Task B' })).toBeVisible()

  await page.getByRole('button', { name: 'Task B' }).click()
  await page.getByRole('combobox', { name: /^Search (tasks|templates)$/ }).press('Tab')
  await expect(page.getByRole('listbox')).toBeHidden()
})

test('template list load failure has a retry that clears the error', async ({ page }) => {
  let attempts = 0
  const templates = [{ value: 'task-a', label: 'Task A' }]
  await page.route('**/api/templates', route => {
    attempts += 1
    if (attempts === 1) {
      return route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Template service temporarily unavailable.' }),
      })
    }
    return route.fulfill({ json: { items: templates } })
  })
  await page.route('**/api/templates/content?*', route => route.fulfill({
    json: {
      value: 'task-a',
      label: 'Task A',
      path: '/tmp/task-a.yaml',
      content: 'name: task-a',
      read_only: false,
      mode_hint: 'form',
      parsed_config: { name: 'task-a' },
    },
  }))

  await page.goto('/generator?token=pyruns-e2e-access-token')
  const templateError = page.getByRole('alert').filter({ hasText: 'Templates could not be loaded.' })
  await expect(templateError).toBeVisible()
  await templateError.getByRole('button', { name: 'Retry' }).click()

  await expect(templateError).toBeHidden()
  await page.getByRole('button', { name: 'Load task' }).click()
  await expect(page.getByRole('option', { name: 'Task A' })).toBeVisible()
  expect(attempts).toBe(2)
})

test('generator preserves shell and uncommitted form drafts across navigation', async ({ page }) => {
  let activeWorkspace: Record<string, unknown> = {
    run_root: 'C:/pyruns-e2e/shell-workspace',
    working_root: 'C:/pyruns-e2e',
    tasks_dir: 'C:/pyruns-e2e/shell-workspace/tasks',
    workspace_kind: 'shell',
    workspace_ready: true,
    script_path: '',
    script_name: 'Shell',
    native_file_picker: false,
    settings: {},
    templates: [],
    shell_runtime: {
      mode: 'follow',
      source: 'test',
      terminal_kind: 'powershell',
      display_name: 'PowerShell',
      executable: 'powershell.exe',
    },
  }
  await page.route('**/api/workspace', route => route.fulfill({ json: activeWorkspace }))
  await page.route('**/api/templates', route => route.fulfill({
    json: { items: [{ value: 'default.yaml', label: 'Default' }] },
  }))
  await page.route('**/api/templates/content?*', route => route.fulfill({
    json: {
      value: 'default.yaml',
      label: 'Default',
      path: 'C:/pyruns-e2e/default.yaml',
      content: 'epochs: 10\n',
      parsed_config: { epochs: 10 },
      read_only: false,
      mode_hint: 'form',
    },
  }))

  await page.goto('/generator?token=pyruns-e2e-access-token')
  const shellEditor = page.getByRole('textbox', { name: 'Task shell editor' })
  await expect(shellEditor).toBeVisible()
  await shellEditor.fill('echo shell-draft-kept')

  await page.getByRole('link', { name: 'Manager' }).click()
  await page.getByRole('link', { name: 'Generator' }).click()
  await expect(page.getByRole('textbox', { name: 'Task shell editor' })).toContainText('echo shell-draft-kept')

  activeWorkspace = {
    run_root: 'C:/pyruns-e2e/script-workspace',
    working_root: 'C:/pyruns-e2e',
    tasks_dir: 'C:/pyruns-e2e/script-workspace/tasks',
    workspace_kind: 'script',
    workspace_ready: true,
    script_path: 'C:/pyruns-e2e/train.py',
    script_name: 'train.py',
    native_file_picker: false,
    settings: {},
    templates: [],
  }

  await page.reload()
  const epochs = page.getByRole('textbox', { name: 'epochs parameter value' })
  await expect(epochs).toHaveValue('10')
  await epochs.fill('27')
  await page.getByRole('link', { name: 'Manager' }).evaluate((link: HTMLElement) => link.click())
  await page.getByRole('link', { name: 'Generator' }).click()

  const restoredEpochs = page.getByRole('textbox', { name: 'epochs parameter value' })
  await expect(restoredEpochs).toHaveValue('27')
  await restoredEpochs.focus()
  await page.getByRole('button', { name: 'YAML' }).click()
  await expect(page.getByRole('textbox', { name: 'Task YAML editor' })).toContainText('epochs: 27')
})

test('manager applies and remembers the selected card order', async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 })
  const baseTask = {
    status: 'pending',
    dir: '',
    config: {},
    config_text: '',
    config_file: '',
    task_kind: 'shell',
    pinned: false,
    notes: '',
    env: {},
    created_at: '2026-08-23_10-00-00',
    start_times: [],
    finish_times: [],
    pids: [],
    progress: 0,
    run_index: 0,
    preview_text: '',
    search_text: '',
    records: [],
    tracks: [],
  }
  const tasks = ['alpha', 'beta', 'gamma'].map(name => ({ ...baseTask, name }))
  await page.route('**/api/tasks?*', route => {
    const sort = new URL(route.request().url()).searchParams.get('sort')
    const items = sort === 'name_desc' ? [...tasks].reverse() : tasks
    return route.fulfill({
      json: {
        items,
        total: items.length,
        offset: 0,
        limit: 50,
        has_more: false,
        status_counts: { pending: 3, queued: 0, running: 0, completed: 0, failed: 0, cancelled: 0 },
      },
    })
  })

  await page.goto('/manager?token=pyruns-e2e-access-token')
  const sortSelect = page.getByRole('combobox', { name: 'Sort task cards' })
  await expect.poll(() => sortSelect.evaluate(element => {
    const select = element as HTMLSelectElement
    const style = window.getComputedStyle(select)
    const context = document.createElement('canvas').getContext('2d')
    if (!context) return false
    context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`
    const widestOption = Math.max(...Array.from(select.options, option => context.measureText(option.text).width))
    const availableWidth = select.clientWidth
      - Number.parseFloat(style.paddingLeft)
      - Number.parseFloat(style.paddingRight)
    return widestOption <= availableWidth
  })).toBe(true)
  await sortSelect.selectOption('name_desc')
  await expect(page.locator('[data-task-card]')).toHaveCount(3)
  await expect.poll(() => page.locator('[data-task-card]').evaluateAll(cards => (
    cards.map(card => card.getAttribute('data-task-card'))
  ))).toEqual(['gamma', 'beta', 'alpha'])

  await page.getByRole('link', { name: 'Generator' }).click()
  await page.getByRole('link', { name: 'Manager' }).click()
  await expect(page.getByRole('combobox', { name: 'Sort task cards' })).toHaveValue('name_desc')
})
