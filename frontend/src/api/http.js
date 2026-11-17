/**
 * The one place a request leaves this application.
 *
 * Everything above it calls `api.something()`. Nothing above it knows about
 * headers, tokens, retries or status codes — which is what makes the mock
 * transport a swap rather than a rewrite (Spec §24).
 *
 * Two behaviours are worth naming, because they are the ones that would
 * otherwise be reimplemented per screen:
 *
 * 1. **The 401 refresh is attempted once, silently** (§13). If it works the
 *    original request is replayed and no screen ever learns it happened. If
 *    it fails the session is cleared and a SESSION_EXPIRED error is thrown,
 *    which the shell renders as a retry strip over the conversation — the
 *    conversation itself stays on screen.
 *
 * 2. **Every request is bounded.** A fetch with no timeout is a hang with
 *    good manners.
 */

import * as errors from './errors.js'
import * as tokens from './tokens.js'

const BASE = (import.meta.env?.VITE_API_BASE ?? '').replace(/\/$/, '')

// Longer than the backend's own 10-second statement timeout, so a query that
// the server stops reports the server's message rather than ours.
const DEFAULT_TIMEOUT_MS = 30_000

let refreshing = null // one refresh in flight at a time, shared by all callers
let onSessionLost = () => {}

/** The shell registers here so a dead session reaches the router once. */
export function whenSessionLost(handler) {
  onSessionLost = handler
}

export async function request(
  method,
  path,
  { body, auth = true, timeout = DEFAULT_TIMEOUT_MS, signal, retryOn401 = true } = {},
) {
  const response = await send(method, path, { body, auth, timeout, signal })

  if (response.status === 401 && auth && retryOn401) {
    const renewed = await refresh()
    if (renewed) {
      const replay = await send(method, path, { body, auth, timeout, signal })
      return unwrap(replay)
    }
    tokens.clear()
    onSessionLost()
    throw errors.fromResponse(response, null)
  }

  return unwrap(response)
}

async function send(method, path, { body, auth, timeout, signal }) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeout)

  // A caller that can cancel (the composer, when a second question is asked)
  // passes its own signal; both must be able to stop the request.
  if (signal) {
    if (signal.aborted) controller.abort()
    else signal.addEventListener('abort', () => controller.abort(), { once: true })
  }

  const headers = { Accept: 'application/json' }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (auth) {
    const session = tokens.read()
    if (session?.access_token) headers.Authorization = `Bearer ${session.access_token}`
  }

  try {
    return await fetch(BASE + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    })
  } catch (cause) {
    throw errors.fromTransport(cause)
  } finally {
    clearTimeout(timer)
  }
}

async function unwrap(response) {
  if (response.status === 204) return null

  let body = null
  const type = response.headers.get('content-type') || ''
  if (type.includes('application/json')) {
    try {
      body = await response.json()
    } catch {
      body = null
    }
  }

  if (!response.ok) throw errors.fromResponse(response, body)
  return body
}

/** One refresh at a time. Five screens hitting a dead token must not queue
 *  five refreshes, each invalidating the last. */
function refresh() {
  if (refreshing) return refreshing

  refreshing = (async () => {
    const session = tokens.read()
    if (!session?.refresh_token) return false
    try {
      const response = await send('POST', '/api/auth/refresh', {
        body: { refresh_token: session.refresh_token },
        auth: false,
        timeout: 10_000,
      })
      if (!response.ok) return false
      const renewed = await response.json()
      tokens.write(renewed)
      return true
    } catch {
      return false
    } finally {
      // Cleared on the next tick so the callers awaiting this promise all see
      // the same answer before a new attempt can start.
      setTimeout(() => {
        refreshing = null
      }, 0)
    }
  })()

  return refreshing
}

export const get = (path, options) => request('GET', path, options)
export const post = (path, body, options) => request('POST', path, { ...options, body })
export const del = (path, options) => request('DELETE', path, options)
