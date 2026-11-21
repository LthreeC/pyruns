import { expect, test, type Locator, type Page } from '@playwright/test'

async function openShellEditor(page: Page, kind: string, name: string) {
  await page.route('**/api/workspace', async route => {
    const response = await route.fetch()
    await route.fulfill({
      response,
      json: {
        ...await response.json(),
        workspace_kind: 'shell',
        workspace_ready: true,
        device_host: 'gpu-lab-01',
        shell_runtime: { mode: 'follow', terminal_kind: kind, display_name: name },
      },
    })
  })
  await page.goto('/generator?token=pyruns-e2e-access-token')
  const editor = page.getByRole('textbox', { name: 'Task shell editor' })
  await expect(editor).toBeVisible()
  const apple = await page.evaluate(() => /Mac|iPhone|iPad|iPod/.test(navigator.platform))
  return { editor, mod: apple ? 'Meta' : 'Control' }
}

async function expectLines(editor: Locator, lines: string[]) {
  await expect.poll(() => editor.locator('.cm-line').allTextContents()).toEqual(lines)
}

for (const [kind, name, marker] of [
  ['bash', 'Bash', '#'],
  ['powershell', 'PowerShell', '#'],
  ['cmd', 'CMD', 'REM'],
] as const) {
  test(name + ' toggles line comments for the cursor and a multi-line selection', async ({ page }) => {
    const { editor, mod } = await openShellEditor(page, kind, name)
    const original = ['  echo first', '', '  echo last']
    await editor.fill(original.join('\n'))
    await editor.press(mod + '+a')
    await editor.press('ArrowLeft')
    await editor.press(mod + '+/')
    await expectLines(editor, ['  ' + marker + ' echo first', '', original[2]])
    await editor.press(mod + '+/')
    await expectLines(editor, original)

    await editor.press(mod + '+a')
    await editor.press(mod + '+/')
    const commented = ['  ' + marker + ' echo first', '', '  ' + marker + ' echo last']
    await expectLines(editor, commented)
    await editor.press(mod + '+z')
    await expectLines(editor, original)
    await editor.press(mod === 'Meta' ? 'Meta+Shift+z' : 'Control+y')
    await expectLines(editor, commented)
    await editor.press(mod + '+a')
    await editor.press(mod + '+/')
    await expectLines(editor, original)

    if (kind === 'cmd') {
      await editor.fill('  rem echo quiet\n  @ReM\techo muted\n  REMOTE staging')
      await editor.press(mod + '+a')
      await editor.press('ArrowLeft')
      await editor.press('Shift+ArrowDown')
      await editor.press('Shift+ArrowDown')
      await editor.press(mod + '+/')
      await expectLines(editor, ['  echo quiet', '  echo muted', '  REMOTE staging'])
      await editor.press('ArrowRight')
      await editor.press(mod + '+/')
      await expectLines(editor, ['  echo quiet', '  echo muted', '  REM REMOTE staging'])
    }
  })
}

test('Shell editor shows built-in shortcuts on demand and keeps task settings compact', async ({ page }) => {
  const { editor, mod } = await openShellEditor(page, 'powershell', 'PowerShell')
  const shortcuts = page.getByLabel('Shell editor shortcuts')
  const comment = page.getByText('Toggle line comment', { exact: true })
  await expect(comment).toBeHidden()
  await shortcuts.click()
  await expect(comment).toBeVisible()
  await shortcuts.press('Escape')
  await expect(comment).toBeHidden()

  await editor.fill('echo first\necho second')
  await editor.press(mod + '+a')
  await editor.press('ArrowLeft')
  await editor.press('Alt+ArrowDown')
  await expectLines(editor, ['echo second', 'echo first'])
  await editor.press('Alt+Shift+ArrowUp')
  await expectLines(editor, ['echo second', 'echo first', 'echo first'])
  await editor.press(mod + '+Shift+k')
  await expectLines(editor, ['echo second', 'echo first'])
  await editor.press(mod + '+f')
  const search = page.locator('.cm-search').getByRole('textbox', { name: 'Find' })
  await expect(search).toBeFocused()
  await search.press('Escape')
  await expect(editor).toBeFocused()
  await editor.press('Escape')
  await page.keyboard.press('Tab')
  await expect(editor).not.toBeFocused()

  const target = page.getByRole('combobox', { name: 'Save to' })
  await expect(target.getByRole('option', { name: 'This device · gpu-lab-01' })).toHaveCount(1)
  await target.selectOption('local')
  await expect(target).toHaveAccessibleDescription('Runs only on gpu-lab-01.')
  await expect(page.getByText('Creates one shell task immediately.', { exact: true })).toHaveCount(0)
  await expect(page.getByText('config.ps1', { exact: true })).toBeHidden()
  await page.locator('summary').filter({ hasText: 'Shell Runtime' }).click()
  await expect(page.getByText('config.ps1', { exact: true })).toBeVisible()
})
