// The API client (R-UI-01): where requests go, and what a failure looks like.
// Every surface talks to the daemon through here, so the base URL, the JSON
// contract and the status check are tested once rather than trusted per caller.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  createTodo,
  deleteTodo,
  fetchArrayBuffer,
  fetchBlob,
  fetchText,
  getAsks,
  getBase,
  initBase,
  respondAsk,
  visualUrl,
} from './client'

let calls
function stubFetch(respond) {
  calls = []
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url, opts = {}) => {
      calls.push({ url, method: opts.method || 'GET', body: opts.body && JSON.parse(opts.body) })
      return respond(url, opts)
    }),
  )
}
const ok = (body, status = 200) => ({
  ok: status < 400,
  status,
  json: async () => body,
  text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
  blob: async () => ({ type: 'image/png', body }),
  arrayBuffer: async () => new Uint8Array([1, 2]).buffer,
})

beforeEach(() => stubFetch(() => ok({})))
afterEach(() => vi.unstubAllGlobals())

describe('the base URL', () => {
  it('is the daemon port the main process reports', async () => {
    vi.stubGlobal('window', { electronAPI: { getDaemonPort: async () => 7699 } })
    await initBase()
    expect(getBase()).toBe('http://127.0.0.1:7699')
    expect(visualUrl('/visuals/v1')).toBe('http://127.0.0.1:7699/visuals/v1')
  })

  it('stays on the default outside Electron', async () => {
    vi.stubGlobal('window', {})
    const before = getBase()
    await initBase()
    expect(getBase()).toBe(before)
  })
})

describe('JSON calls', () => {
  it('send the method, path and body', async () => {
    await createTodo('Write tests', { status: 'active' })
    await respondAsk('a1', 'yes')
    expect(calls.map((c) => [c.method, c.url.replace(getBase(), ''), c.body])).toEqual([
      [
        'POST',
        '/todos',
        {
          title: 'Write tests',
          status: 'active',
          tags: undefined,
          pinned: undefined,
          note: undefined,
        },
      ],
      ['POST', '/ask/a1/respond', { response: 'yes' }],
    ])
  })

  it('return the parsed body, or null for a 204', async () => {
    stubFetch((url) =>
      url.endsWith('/asks?status=pending') ? ok([{ ask_id: 'a' }]) : ok(null, 204),
    )
    expect(await getAsks()).toEqual([{ ask_id: 'a' }])
    expect(await deleteTodo('tdo_1')).toBe(null)
  })

  it('raise with the method, path and status on a failure', async () => {
    stubFetch(() => ok({ detail: 'nope' }, 404))
    await expect(getAsks()).rejects.toThrow('GET /asks?status=pending → 404')
  })
})

describe('raw bodies', () => {
  it('come back as a blob, text or bytes', async () => {
    expect((await fetchBlob('http://d/x.png')).type).toBe('image/png')
    stubFetch(() => ok('# notes'))
    expect(await fetchText('http://d/x.md')).toBe('# notes')
    expect(new Uint8Array(await fetchArrayBuffer('http://d/x.wav'))).toEqual(new Uint8Array([1, 2]))
  })

  it('raise on a failure instead of reading the error body as content', async () => {
    stubFetch(() => ok({ code: 'http_error' }, 404))
    await expect(fetchBlob('http://d/gone.png')).rejects.toThrow('GET http://d/gone.png → 404')
    await expect(fetchText('http://d/gone.md')).rejects.toThrow('→ 404')
    await expect(fetchArrayBuffer('http://d/gone.wav')).rejects.toThrow('→ 404')
  })
})
