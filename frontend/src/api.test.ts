import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  ApiError,
  beginAuthorizationAttempt,
  checkPyrunsUpdate,
  exportTasksCsv,
  getTasks,
  getSystemInfo,
  getWorkspace,
  recoverSession,
  restartPyruns,
  subscribeUnauthorized,
  updatePyruns,
  updateEnv,
  updateNotes,
} from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('API errors', () => {
  it('recovers a stale local UI session through the same-origin handoff', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ ok: true }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)

    await expect(recoverSession()).resolves.toBe(true)

    expect(fetchMock).toHaveBeenCalledWith('/session/recover', expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
      body: '{}',
    }))
  })

  it('shares one in-flight session recovery request', async () => {
    let resolveResponse!: (response: Response) => void
    const fetchMock = vi.fn().mockImplementation(() => new Promise<Response>(resolve => {
      resolveResponse = resolve
    }))
    vi.stubGlobal('fetch', fetchMock)

    const first = recoverSession()
    const second = recoverSession()
    expect(fetchMock).toHaveBeenCalledOnce()
    resolveResponse(new Response(JSON.stringify({ ok: true }), { status: 200 }))

    await expect(first).resolves.toBe(true)
    await expect(second).resolves.toBe(true)
  })

  it('sends the selected Manager card order to the task endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ items: [], total: 0, offset: 0, limit: 50, has_more: false }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)

    await getTasks({ sort: 'name_desc', offset: 50, limit: 50 })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/tasks?offset=50&limit=50&sort=name_desc',
      expect.any(Object),
    )
  })

  it('sends the notes version used for optimistic concurrency', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ ok: true, task: { name: 'alpha', notes: 'next' } }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)

    await updateNotes('alpha', 'next', 'previous')

    expect(fetchMock).toHaveBeenCalledOnce()
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks/alpha/notes', expect.objectContaining({
      method: 'PATCH',
      body: JSON.stringify({ notes: 'next', expected_notes: 'previous' }),
    }))
  })

  it('sends the environment version used for optimistic concurrency', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ ok: true, task: { name: 'alpha', env: { NEXT: '2' } } }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)

    await updateEnv('alpha', { NEXT: '2' }, { PREVIOUS: '1' })

    expect(fetchMock).toHaveBeenCalledOnce()
    expect(fetchMock).toHaveBeenCalledWith('/api/tasks/alpha/env', expect.objectContaining({
      method: 'PATCH',
      body: JSON.stringify({ env: { NEXT: '2' }, expected_env: { PREVIOUS: '1' } }),
    }))
  })

  it('uses dedicated system endpoints for full-process updates', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        version: '0.3.0',
        installed_version: '0.3.0',
        restart_required: false,
        instance_id: 'old-instance',
        update_supported: true,
        update_state: 'idle',
        last_update: null,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        current_version: '0.3.0',
        latest_version: '0.4.0',
        update_available: true,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ok: true,
        instance_id: 'old-instance',
        version: '0.3.0',
        state: 'restarting',
      }), { status: 202, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        ok: true,
        instance_id: 'old-instance',
        version: '0.3.0',
        state: 'restarting',
      }), { status: 202, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    await getSystemInfo()
    await checkPyrunsUpdate()
    await updatePyruns()
    await restartPyruns()

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/system/info', expect.any(Object))
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/system/update/check', expect.any(Object))
    expect(fetchMock).toHaveBeenNthCalledWith(3, '/api/system/update', expect.objectContaining({
      method: 'POST',
    }))
    expect(fetchMock).toHaveBeenNthCalledWith(4, '/api/system/restart', expect.objectContaining({
      method: 'POST',
    }))
  })

  it('preserves the HTTP status and response body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: 'Workspace is unavailable' }),
      { status: 503, headers: { 'Content-Type': 'application/json' } },
    )))

    const error = await getWorkspace().catch(value => value)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({
      status: 503,
      message: 'Workspace is unavailable',
      body: { detail: 'Workspace is unavailable' },
    })
  })

  it('notifies subscribers when a JSON request loses authorization', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: 'UI authentication required' }),
      { status: 401, headers: { 'Content-Type': 'application/json' } },
    )))
    const listener = vi.fn()
    const unsubscribe = subscribeUnauthorized(listener)

    const error = await getWorkspace().catch(value => value)
    unsubscribe()

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 401, message: 'UI authentication required' })
    expect(listener).toHaveBeenCalledOnce()
    expect(listener).toHaveBeenCalledWith(error)
  })

  it('uses the same authorization contract for file exports', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: 'UI authentication required' }),
      { status: 401, headers: { 'Content-Type': 'application/json' } },
    )))
    const listener = vi.fn()
    const unsubscribe = subscribeUnauthorized(listener)

    const error = await exportTasksCsv(['alpha']).catch(value => value)
    unsubscribe()

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 401 })
    expect(listener).toHaveBeenCalledOnce()
  })

  it('ignores a late 401 from an authorization epoch that already expired', async () => {
    let resolveFirst!: (response: Response) => void
    let resolveSecond!: (response: Response) => void
    const first = new Promise<Response>(resolve => { resolveFirst = resolve })
    const second = new Promise<Response>(resolve => { resolveSecond = resolve })
    vi.stubGlobal('fetch', vi.fn()
      .mockImplementationOnce(() => first)
      .mockImplementationOnce(() => second)
      .mockResolvedValueOnce(new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })))
    const listener = vi.fn()
    const unsubscribe = subscribeUnauthorized(listener)
    const firstRequest = getWorkspace().catch(value => value)
    const secondRequest = getWorkspace().catch(value => value)

    resolveFirst(new Response(JSON.stringify({ detail: 'UI authentication required' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    }))
    await firstRequest
    expect(listener).toHaveBeenCalledOnce()

    await getWorkspace()
    resolveSecond(new Response(JSON.stringify({ detail: 'stale authentication failure' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    }))
    await secondRequest
    unsubscribe()

    expect(listener).toHaveBeenCalledOnce()
  })

  it('ignores a request from before a successful reconnection attempt', async () => {
    let resolveStale!: (response: Response) => void
    const staleResponse = new Promise<Response>(resolve => { resolveStale = resolve })
    vi.stubGlobal('fetch', vi.fn()
      .mockImplementationOnce(() => staleResponse)
      .mockResolvedValueOnce(new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })))
    const listener = vi.fn()
    const unsubscribe = subscribeUnauthorized(listener)
    const staleRequest = getWorkspace().catch(value => value)

    beginAuthorizationAttempt()
    await getWorkspace()
    resolveStale(new Response(JSON.stringify({ detail: 'stale authentication failure' }), {
      status: 401,
      headers: { 'Content-Type': 'application/json' },
    }))
    await staleRequest
    unsubscribe()

    expect(listener).not.toHaveBeenCalled()
  })
})
