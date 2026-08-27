import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { delimiter, resolve } from 'node:path'

const frontendRoot = resolve(import.meta.dirname, '..')
const repositoryRoot = resolve(frontendRoot, '..')
const accessToken = 'pyruns-e2e-access-token'
const startupTimeoutMs = 30_000
const shutdownTimeoutMs = 2_000

function hiddenSpawnOptions(options = {}) {
  return process.platform === 'win32'
    ? { windowsHide: true, ...options }
    : options
}

function pathCandidates(command) {
  if (process.platform !== 'win32') return []
  return String(process.env.PATH || process.env.Path || '')
    .split(delimiter)
    .map(value => value.trim().replace(/^"|"$/g, ''))
    .map(value => resolve(value || '.', command))
    .filter(candidate => existsSync(candidate))
}

function canRunPython(executable, prefixArgs = []) {
  const result = spawnSync(
    executable,
    [...prefixArgs, '-c', 'import sys; sys.exit(0)'],
    hiddenSpawnOptions({ stdio: 'ignore' }),
  )
  return !result.error && result.status === 0
}

function resolvePython() {
  const configured = String(process.env.PYRUNS_E2E_PYTHON || '').trim()
  if (configured) {
    if (!canRunPython(configured)) {
      throw new Error(`PYRUNS_E2E_PYTHON is not a usable Python interpreter: ${configured}`)
    }
    return { executable: configured, prefixArgs: [] }
  }

  if (process.platform === 'win32') {
    // ``python.exe`` may resolve to the Microsoft Store alias even when a
    // real interpreter is available later on PATH. Validate each candidate.
    const candidates = [
      ...pathCandidates('python.exe'),
      ...pathCandidates('python'),
      'python.exe',
      'python',
    ]
    const seen = new Set()
    for (const candidate of candidates) {
      const key = candidate.toLowerCase()
      if (seen.has(key) || key.includes('windowsapps')) continue
      seen.add(key)
      if (canRunPython(candidate)) return { executable: candidate, prefixArgs: [] }
    }
    if (canRunPython('py.exe', ['-3'])) return { executable: 'py.exe', prefixArgs: ['-3'] }
  } else {
    for (const candidate of ['python3', 'python']) {
      if (canRunPython(candidate)) return { executable: candidate, prefixArgs: [] }
    }
  }

  throw new Error(
    'Could not find a usable Python interpreter. Set PYRUNS_E2E_PYTHON to its full path.',
  )
}

function delay(milliseconds) {
  return new Promise(resolveDelay => setTimeout(resolveDelay, milliseconds))
}

async function serverIsAvailable(url) {
  try {
    const response = await fetch(url, { redirect: 'manual', signal: AbortSignal.timeout(2_000) })
    await response.body?.cancel()
    return true
  } catch {
    return false
  }
}

async function waitForServer(url, child, output, childError) {
  const deadline = Date.now() + startupTimeoutMs
  while (Date.now() < deadline) {
    if (childError()) {
      throw new Error(`Could not start the Pyruns E2E server: ${childError().message}`)
    }
    if (child.exitCode != null) {
      throw new Error(
        `Pyruns E2E server exited before startup (code ${child.exitCode}).\n${output()}`.trim(),
      )
    }
    if (await serverIsAvailable(url)) return
    await delay(100)
  }
  throw new Error(`Timed out waiting for the Pyruns E2E server at ${url}.\n${output()}`.trim())
}

function waitForClose(child, timeoutMs) {
  if (child.exitCode != null) return Promise.resolve(true)
  return new Promise(resolveClose => {
    let settled = false
    const finish = closed => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      child.off('close', onClose)
      resolveClose(closed)
    }
    const onClose = () => finish(true)
    const timer = setTimeout(() => {
      finish(false)
    }, timeoutMs)
    child.once('close', onClose)
    if (child.exitCode != null) finish(true)
  })
}

async function terminateServer(child) {
  if (child.exitCode != null || !child.pid) return

  if (process.platform === 'win32') {
    const result = spawnSync(
      'taskkill.exe',
      ['/F', '/T', '/PID', String(child.pid)],
      hiddenSpawnOptions({ stdio: 'ignore' }),
    )
    if (result.error || result.status !== 0) child.kill('SIGKILL')
  } else {
    child.kill('SIGTERM')
  }

  if (await waitForClose(child, shutdownTimeoutMs)) return
  child.kill('SIGKILL')
  await waitForClose(child, shutdownTimeoutMs)
}

export async function startE2EServer() {
  const port = process.env.PYRUNS_E2E_PORT || '8765'
  const url = `http://127.0.0.1:${port}/`
  if (await serverIsAvailable(url)) {
    throw new Error(`${url} is already in use; choose another PYRUNS_E2E_PORT.`)
  }

  const python = resolvePython()
  const runRoot = mkdtempSync(resolve(tmpdir(), 'pyruns-e2e-'))
  const pythonPath = process.env.PYTHONPATH
    ? `${repositoryRoot}${delimiter}${process.env.PYTHONPATH}`
    : repositoryRoot
  const child = spawn(
    python.executable,
    [
      ...python.prefixArgs,
      '-c',
      'import sys; from pyruns.web.app import main; main(reload=False, open_browser=False, port=int(sys.argv[1]), access_token=sys.argv[2])',
      port,
      accessToken,
    ],
    hiddenSpawnOptions({
      cwd: repositoryRoot,
      env: {
        ...process.env,
        PYTHONPATH: pythonPath,
        PYTHONDONTWRITEBYTECODE: '1',
        PYRUNS_UI_SESSION_STATE_DIR: resolve(runRoot, '.sessions'),
        __PYRUNS_ROOT__: runRoot,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
    }),
  )
  let serverOutput = ''
  let childError = null
  const collectOutput = chunk => {
    const text = chunk.toString()
    serverOutput = `${serverOutput}${text}`.slice(-16_384)
    process.stderr.write(`[E2E server] ${text}`)
  }
  child.once('error', error => {
    childError = error
  })
  child.stdout.on('data', collectOutput)
  child.stderr.on('data', collectOutput)

  try {
    await waitForServer(url, child, () => serverOutput, () => childError)
  } catch (error) {
    await terminateServer(child)
    rmSync(runRoot, { recursive: true, force: true })
    throw error
  }

  return async () => {
    try {
      await terminateServer(child)
    } finally {
      rmSync(runRoot, { recursive: true, force: true })
    }
  }
}
