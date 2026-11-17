/**
 * The six failure states of Frontend Specification §13, as one type.
 *
 * The distinction this file exists to keep: a **refusal** is the system
 * working correctly and declining — it comes back as a 200 with a body, and
 * gets a seal band. A **failure** is the system not working — it comes back
 * as a thrown ApiError, and gets a retry strip. Showing them the same way
 * trains people to ignore the refusals, which are the ones carrying real
 * information.
 */

export class ApiError extends Error {
  /**
   * @param {object} init
   * @param {string} init.kind     one of KINDS below
   * @param {string} init.message  what the person reads
   * @param {string} [init.action] the ONE action offered. Never two.
   * @param {number} [init.status]
   * @param {number} [init.retryAfter] seconds, from the Retry-After header
   * @param {string} [init.requestId]
   * @param {string} [init.code]
   */
  constructor({ kind, message, action, status, retryAfter, requestId, code }) {
    super(message)
    this.name = 'ApiError'
    this.kind = kind
    this.action = action ?? null
    this.status = status ?? null
    this.retryAfter = retryAfter ?? null
    this.requestId = requestId ?? null
    this.code = code ?? null
  }
}

export const KINDS = {
  OFFLINE: 'offline',
  TIMEOUT: 'timeout',
  RATE_LIMITED: 'rate_limited',
  SESSION_EXPIRED: 'session_expired',
  UNAVAILABLE: 'unavailable',
  SERVER: 'server',
  REFUSED: 'refused', // a 4xx that is the caller's fault: bad input, no grant
}

/** Build the ApiError for a response the server did send. */
export function fromResponse(response, body) {
  const detail =
    (body && (body.detail || body.reason)) || `The server returned ${response.status}.`
  const code = body && body.code ? body.code : null
  const requestId = response.headers.get('x-request-id')
  const retryAfter = seconds(response.headers.get('retry-after'))

  if (response.status === 401)
    return new ApiError({
      kind: KINDS.SESSION_EXPIRED,
      message: 'Your session ended.',
      action: 'Sign in again',
      status: 401,
      code,
    })

  if (response.status === 429)
    return new ApiError({
      kind: KINDS.RATE_LIMITED,
      // No retry button: there is nothing to retry yet.
      message: detail,
      action: null,
      status: 429,
      retryAfter,
      code,
    })

  if (response.status === 503)
    return new ApiError({
      kind: KINDS.UNAVAILABLE,
      message: detail,
      action: 'Try again',
      status: 503,
      retryAfter,
      code,
    })

  if (response.status >= 500)
    return new ApiError({
      kind: KINDS.SERVER,
      // A person reporting a bug with an id is worth ten without one.
      message: 'Something broke on our side, and it was logged.',
      action: 'Try again',
      status: response.status,
      requestId,
      code,
    })

  return new ApiError({
    kind: KINDS.REFUSED,
    message: detail,
    action: null,
    status: response.status,
    code,
  })
}

/** Build the ApiError for a request that never reached a server. */
export function fromTransport(cause) {
  if (typeof navigator !== 'undefined' && navigator.onLine === false)
    return new ApiError({
      kind: KINDS.OFFLINE,
      message: 'You are offline. Your question is saved.',
      action: 'Retry when back',
    })

  if (cause && cause.name === 'AbortError')
    return new ApiError({
      kind: KINDS.TIMEOUT,
      message: 'That request took too long and was stopped.',
      action: 'Try again',
    })

  return new ApiError({
    kind: KINDS.OFFLINE,
    message: 'The server could not be reached.',
    action: 'Try again',
  })
}

/** Retry-After is either a count of seconds or an HTTP date. Both are legal;
 *  the interface shows a time either way, never a raw number of seconds. */
function seconds(header) {
  if (!header) return null
  const n = Number(header)
  if (Number.isFinite(n)) return Math.max(0, Math.round(n))
  const when = Date.parse(header)
  if (Number.isNaN(when)) return null
  return Math.max(0, Math.round((when - Date.now()) / 1000))
}
