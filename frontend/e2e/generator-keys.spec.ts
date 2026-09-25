import { expect, test, type Page } from '@playwright/test'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { parse } from 'yaml'

async function withGeneratorWorkspace(page: Page, yaml: string, check: (runRoot: string) => Promise<void>) {
  const root = await mkdtemp(join(tmpdir(), 'pyruns-generator-keys-'))
  const script = join(root, 'train.py')
  const config = join(root, 'input.yaml')
  await writeFile(script, 'import pyruns\nconfig = pyruns.load()\nprint(config)\n')
  await writeFile(config, yaml)
  await page.goto('/?token=pyruns-e2e-access-token')
  const original = await (await page.request.get('/api/workspace')).json()
  try {
    const opened = await page.request.post('/api/launcher/open', { data: { script_path: script, config_path: config } })
    expect(opened.ok(), await opened.text()).toBe(true)
    const workspace = await opened.json()
    await page.goto('/generator')
    await expect(page.getByRole('button', { name: 'Grid', exact: true })).toBeVisible()
    await page.getByRole('button', { name: 'Grid', exact: true }).click({ timeout: 5_000 })
    await check(workspace.run_root)
  } finally {
    const restored = await page.request.post('/api/workspace/run-root', { data: { path: original.run_root } })
    expect(restored.ok(), await restored.text()).toBe(true)
    await rm(root, { recursive: true, force: true, maxRetries: 3 })
  }
}

async function expectGeneratedConfig(page: Page, runRoot: string, expected: Map<unknown, unknown>, expectedSaved = expected) {
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/generator/create'))
  await page.getByRole('button', { name: 'Generate Tasks', exact: true }).click()
  const response = await responsePromise
  expect(response.ok(), await response.text()).toBe(true)
  const payload = response.request().postDataJSON()
  expect(payload.mode).toBe('form')
  expect(parse(payload.yaml_text, { mapAsMap: true })).toEqual(expected)
  const created = await response.json()
  expect(created.count).toBe(1)
  const saved = parse(await readFile(join(runRoot, 'tasks', created.items[0].name, 'config.yaml'), 'utf8'), { mapAsMap: true })
  expect(saved).toEqual(expectedSaved)
}

test('generator edits pinned literal keys independently and restores legacy pins', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('pyruns_pinned_params', JSON.stringify(['trainer.lr'])))
  const yaml = 'a.b: 1\na:\n  b: nested\ntrainer:\n  lr: 0.2\n_meta_hint: hidden\n'
  await withGeneratorWorkspace(page, yaml, async runRoot => {
    await expect(page.getByRole('textbox', { name: '_meta_hint parameter value', exact: true })).toHaveCount(0)
    await expect(page.getByRole('textbox', { name: 'trainer.lr parameter value', exact: true })).toHaveValue('0.2')
    await page.getByRole('button', { name: /^Pin (?:a\.b|\["a\.b"\])$/ }).click()
    const literal = page.getByRole('textbox', { name: /^(?:a\.b|\["a\.b"\]) parameter value$/ })
    await expect(literal).toHaveValue('1')
    await literal.fill('27')
    await page.getByRole('button', { name: 'Pin b', exact: true }).click()
    await expect(page.getByRole('textbox', { name: 'a.b parameter value', exact: true })).toHaveValue('nested')
    const expected = parse(yaml, { mapAsMap: true })
    expected.set('a.b', 27)
    const expectedSaved = new Map(expected)
    expectedSaved.delete('_meta_hint') // Task creation strips top-level template metadata.
    await expectGeneratedConfig(page, runRoot, expected, expectedSaved)
    await page.reload()
    await expect(page.getByRole('button', { name: 'Unpin ["a.b"]', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Unpin a.b', exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Unpin trainer.lr', exact: true })).toBeVisible()
  })
})

test('generator preserves typed keys and fixed list mappings through edits and creation', async ({ page }) => {
  const yaml = '7: numeric\n"7": string\ntrue: boolean\n"true": text\n0.5: ratio\neditable: 1\nrows: [{1: numeric, "1": string}, {}, "x | y"]\n'
  await withGeneratorWorkspace(page, yaml, async runRoot => {
    const editable = page.getByRole('textbox', { name: 'editable parameter value', exact: true })
    await expect(editable).toHaveValue('1')
    await editable.fill('2')
    await page.getByRole('button', { name: 'YAML', exact: true }).click()
    const expected = parse(yaml, { mapAsMap: true })
    expected.set('editable', 2)
    expect(parse(await page.getByRole('textbox', { name: 'Task YAML editor' }).innerText(), { mapAsMap: true })).toEqual(expected)
    await page.getByRole('button', { name: 'Grid', exact: true }).click()
    const rows = page.getByRole('textbox', { name: 'rows parameter value', exact: true })
    // Focus/blur must also preserve the Map values displayed in a list field.
    await rows.focus()
    await rows.press('Tab')
    await expectGeneratedConfig(page, runRoot, expected)
  })
})

test('generator tree retains empty parent keys and edits search results at their original paths', async ({ page, isMobile }) => {
  const yaml = '"":\n  child: empty-parent\nchild: root\nsection.dot:\n  value: original\n'
  await withGeneratorWorkspace(page, yaml, async runRoot => {
    await expect(page.getByRole('textbox', { name: 'child parameter value', exact: true })).toHaveCount(2)
    await page.getByRole('button', { name: 'Tree', exact: true }).click()
    await expect(page.getByRole('textbox', { name: 'child parameter value', exact: true })).toHaveCount(2)
    await expect(page.getByTitle('[""] (1 fields)', { exact: true })).toBeVisible()
    if (isMobile) await page.getByRole('button', { name: 'Outline', exact: true }).click()
    await page.getByPlaceholder('Search path or value').fill('empty-parent')
    const found = page.getByRole('textbox', { name: 'child parameter value', exact: true })
    await expect(found).toHaveValue('empty-parent')
    await found.fill('changed')
    await page.getByPlaceholder('Search path or value').fill('section.dot')
    const dotted = page.getByRole('textbox', { name: 'value parameter value', exact: true })
    await expect(dotted).toHaveValue('original')
    await dotted.fill('updated')
    const expected = parse(yaml, { mapAsMap: true })
    expected.get('').set('child', 'changed')
    expected.get('section.dot').set('value', 'updated')
    await expectGeneratedConfig(page, runRoot, expected)
  })
})
