/**
 * Where the session tokens live.
 *
 * `sessionStorage`, not `localStorage`: closing the tab ends the session.
 * Every read and write is wrapped, because a private window, blocked site
 * data or a sandboxed iframe can make either of them throw — and an app that
 * cannot start because storage was unavailable is a worse outcome than one
 * that asks the person to sign in again.
 */

const KEY = 'speakql.session'

let memory = null // the fallback when storage is unavailable

export function read() {
  if (memory) return memory
  try {
    const raw = sessionStorage.getItem(KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export function write(session) {
  memory = session
  try {
    sessionStorage.setItem(KEY, JSON.stringify(session))
  } catch {
    /* held in memory for this page instead */
  }
}

export function clear() {
  memory = null
  try {
    sessionStorage.removeItem(KEY)
  } catch {
    /* nothing to do */
  }
}
