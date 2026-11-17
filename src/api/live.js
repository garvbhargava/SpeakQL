/**
 * The live transport: one function per endpoint in Backend Plan §12.
 *
 * Every screen calls one of these. No screen builds a URL, and no screen
 * imports mock data — which is the rule that makes the mock and the API
 * interchangeable (Spec §24).
 */

import { get, post, del } from './http.js'
import * as tokens from './tokens.js'

export const name = 'live'

// ------------------------------------------------------------------ auth --

export const auth = {
  /** Step one: say who you are, and be told what happens next — before you
   *  commit to it. `path` drives the interface's one question. */
  start: (email, inviteToken = null) =>
    post('/api/auth/start', { email, invite_token: inviteToken }, { auth: false }),

  /** Step two: the six digits. `asOwner` is consulted only on the
   *  create_company path and ignored everywhere else. */
  verify: async (email, code, asOwner = false) => {
    const session = await post(
      '/api/auth/verify',
      { email, code, as_owner: asOwner },
      { auth: false },
    )
    tokens.write(session)
    return session
  },

  me: () => get('/api/auth/me'),

  logout: async () => {
    try {
      await post('/api/auth/logout', undefined)
    } finally {
      // The local session goes whatever the server said. A logout that
      // leaves a token behind because the network blinked is a bug with
      // security consequences.
      tokens.clear()
    }
  },

  session: () => tokens.read(),
}

// ---------------------------------------------------------- organisation --

export const org = {
  get: () => get('/api/org'),
  people: () => get('/api/org/people'),
  invite: (email, grants = []) =>
    post('/api/org/invite', { email, product_role: 'member', grants }),
  approve: (personId) => post(`/api/org/people/${personId}/approve`, undefined),
  grant: (personId, connectionId, dbRole) =>
    post(`/api/org/people/${personId}/grants`, {
      connection_id: connectionId,
      db_role: dbRole,
    }),
  revoke: (personId, connectionId) =>
    del(`/api/org/people/${personId}/grants/${connectionId}`),
}

// ------------------------------------------------------------- databases --

export const connections = {
  list: () => get('/api/connections'),
  /** Returns a ConnectionResult, whose four checks are reported one at a
   *  time. A refusal here is an answer, not an error. */
  register: (form) => post('/api/connections', form),
  reindex: (connectionId) => post(`/api/connections/${connectionId}/reindex`, undefined),
  schema: (connectionId) => get(`/api/schema/${connectionId}`),
}

export const datasets = {
  plan: (payload) => post('/api/datasets/plan', payload),
  load: (payload) => post('/api/datasets/load', payload),
}

// ---------------------------------------------------------------- asking --

export const ask = {
  /** One question in, one of five outcomes out. The five are different
   *  shapes, and `mode` says which. */
  send: (payload, { signal } = {}) => post('/api/ask', payload, { signal }),
  threads: () => get('/api/threads'),
  thread: (threadId) => get(`/api/threads/${threadId}`),
  feedback: (messageId, payload) => post(`/api/feedback/${messageId}`, payload),
}

// ----------------------------------------------------------- corrections --

export const corrections = {
  /** An owner's correction writes the real table; a member's raises a merge
   *  request. `landed` says which happened, because they are genuinely
   *  different things and the interface must not blur them. */
  propose: (edit) => post('/api/rows/edit', edit),
  merges: () => get('/api/merges'),
  decide: (mergeId, decision, reason = null) =>
    post(`/api/merges/${mergeId}`, { decision, reason }),
}

// ------------------------------------------------------------------ meta --

export const meta = {
  health: () => get('/health', { auth: false }),
}
