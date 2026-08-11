import { spawn, spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { delimiter, resolve } from 'node:path'

const frontendRoot = resolve(import.meta.dirname, '..')
const repositoryRoot = resolve(frontendRoot, '..')
const runRoot = mkdtempSync(resolve(tmpdir(), 'pyruns-e2e-'))
const python = process.env.PYRUNS_E2E_PYTHON || (process.platform === 'win32' ? 'python.exe' : 'python')
const port = process.env.PYRUNS_E2E_PORT || '8765'
const accessToken = 'pyruns-e2e-access-token'
const pythonPath = process.env.PYTHONPATH
  ? `${repositoryRoot}${delimiter}${process.env.PYTHONPATH}`
  : repositoryRoot

const child = spawn(
  python,
  [
    '-c',
    'import sys; from pyruns.web.app import main; main(reload=False, open_browser=False, port=int(sys.argv[1]), access_token=sys.argv[2])',
    port,
    accessToken,
  ],
  {
    cwd: repositoryRoot,
    env: {
      ...process.env,
      PYTHONPATH: pythonPath,
      PYTHONDONTWRITEBYTECODE: '1',
      __PYRUNS_ROOT__: runRoot,
    },
    stdio: 'inherit',
    windowsHide: true,
  },
)

let stopping = false
let finished = false
let requestedExitCode = null

function finish(exitCode) {
  if (finished) return
  finished = true
  rmSync(runRoot, { recursive: true, force: true })
  process.exitCode = exitCode
}

function terminateChild() {
  if (child.exitCode != null || !child.pid) return

  if (process.platform === 'win32') {
    const result = spawnSync(
      'taskkill.exe',
      ['/F', '/T', '/PID', String(child.pid)],
      { stdio: 'ignore', windowsHide: true },
    )
    if (result.error || result.status !== 0) {
      child.kill('SIGKILL')
    }
  } else {
    child.kill('SIGTERM')
  }

  const forceTimer = setTimeout(() => {
    if (child.exitCode != null) return
    if (process.platform === 'win32') {
      child.kill('SIGKILL')
      return
    }
    child.kill('SIGKILL')
  }, 2_000)
  forceTimer.unref()
}

function stop(exitCode = 0) {
  if (requestedExitCode == null) requestedExitCode = exitCode
  if (stopping) return
  stopping = true
  if (!child.pid) {
    finish(requestedExitCode)
    return
  }
  terminateChild()
}

process.once('SIGINT', () => stop(130))
process.once('SIGTERM', () => stop(143))
child.once('error', error => {
  console.error(error)
  stop(1)
})
child.once('close', code => finish(requestedExitCode ?? code ?? 1))
