/**
 * The mock transport. Same surface as `live.js`, same shapes, no server.
 *
 * It exists for two reasons and neither of them is convenience:
 *
 * 1. Every screen can be built, shown and reviewed before the endpoint behind
 *    it is wired, and switching back is one environment variable.
 * 2. It is the thing the contract test runs. `contract.test.js` pushes every
 *    response below through the shapes in `contract.js`, so a mock that
 *    drifts from Backend Plan §12 fails a test instead of being discovered by
 *    hand three screens later.
 *
 * The data is the demo warehouse: Northwind Group, the two connections the
 * bootstrap seeds, and the questions `make demo` asks — so what this returns
 * is what the real backend returns, down to the figures.
 *
 * **The fault-mode switch** (§23). `mock.fault('timeout')` makes the next
 * ask fail in a chosen way, which is how the six failure states of §13 are
 * built and captured without unplugging anything.
 */

import { ApiError, KINDS } from './errors.js'

export const name = 'mock'

const LATENCY = 260 // enough that the stage ladder is visible doing its job

let fault = null
/** @param {null|'timeout'|'offline'|'unavailable'|'rate_limited'|'expired'|'server'} kind */
export function setFault(kind) {
  fault = kind
}
export function currentFault() {
  return fault
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

async function reply(value, ms = LATENCY) {
  await sleep(ms)
  return structuredClone(value)
}

function raiseFault() {
  const kind = fault
  fault = null // one-shot: a fault you have seen is a fault you have handled
  switch (kind) {
    case 'timeout':
      return new ApiError({
        kind: KINDS.TIMEOUT,
        message: 'That query took too long and was stopped at ten seconds.',
        action: 'Try again',
      })
    case 'offline':
      return new ApiError({
        kind: KINDS.OFFLINE,
        message: 'You are offline. Your question is saved.',
        action: 'Retry when back',
      })
    case 'unavailable':
      return new ApiError({
        kind: KINDS.UNAVAILABLE,
        message:
          'The larger model could not be reached, and the local answer did not pass validation.',
        action: 'Try again',
        status: 503,
      })
    case 'rate_limited':
      return new ApiError({
        kind: KINDS.RATE_LIMITED,
        message: 'You have asked 60 questions this hour.',
        status: 429,
        retryAfter: 1_520,
      })
    case 'expired':
      return new ApiError({
        kind: KINDS.SESSION_EXPIRED,
        message: 'Your session ended.',
        action: 'Sign in again',
        status: 401,
      })
    case 'server':
      return new ApiError({
        kind: KINDS.SERVER,
        message: 'Something broke on our side, and it was logged.',
        action: 'Try again',
        status: 500,
        requestId: 'req_7f21c4a9',
      })
    default:
      return null
  }
}

// ------------------------------------------------------------- the world --

const ORG = {
  id: 1,
  name: 'Northwind Group',
  kind: 'company',
  domain: 'northwind.co',
  people_count: 4,
  database_count: 2,
}

const CONNECTIONS = [
  {
    id: 1,
    name: 'Sales warehouse',
    kind: 'internal',
    database_name: 'northwind_sales',
    host: null,
    table_count: 6,
    row_estimate: 1840,
    created_at: '2026-08-11T09:14:00Z',
  },
  {
    id: 2,
    name: 'Reporting replica',
    kind: 'external',
    database_name: 'northwind_dw',
    host: 'dw.northwind.co',
    table_count: 4,
    row_estimate: 12400,
    created_at: '2026-09-02T16:40:00Z',
  },
]

const PEOPLE = [
  {
    id: 1,
    email: 'garv@northwind.co',
    product_role: 'owner',
    state: 'active',
    grants: [
      { connection_id: 1, connection_name: 'Sales warehouse', db_role: 'analyst' },
      { connection_id: 2, connection_name: 'Reporting replica', db_role: 'analyst' },
    ],
  },
  {
    id: 2,
    email: 'priya@northwind.co',
    product_role: 'member',
    state: 'active',
    grants: [
      { connection_id: 1, connection_name: 'Sales warehouse', db_role: 'analyst' },
      { connection_id: 2, connection_name: 'Reporting replica', db_role: 'viewer' },
    ],
  },
  {
    id: 3,
    email: 'dhruv@northwind.co',
    product_role: 'member',
    state: 'active',
    grants: [{ connection_id: 1, connection_name: 'Sales warehouse', db_role: 'viewer' }],
  },
  {
    id: 4,
    email: 'kritant@northwind.co',
    product_role: 'member',
    state: 'pending',
    grants: [],
  },
]

const MERGES = [
  {
    id: 41,
    state: 'open',
    raised_by: 'priya@northwind.co',
    table: 'public.shipments',
    pk_value: '2291',
    column_name: 'units',
    before_value: null,
    proposed_value: '480',
    current_value: null,
    note: 'Confirmed against the delivery note.',
    created_at: '2026-09-26T11:02:00Z',
  },
  {
    id: 42,
    state: 'stale',
    raised_by: 'dhruv@northwind.co',
    table: 'public.shipments',
    pk_value: '2304',
    column_name: 'units',
    before_value: '120',
    // The row moved underneath the request. This is the third column on the
    // merge card, and the reason the card is refused rather than applied.
    current_value: '150',
    proposed_value: '140',
    note: 'Recount after the audit.',
    created_at: '2026-09-24T08:30:00Z',
  },
  {
    id: 39,
    state: 'merged',
    raised_by: 'priya@northwind.co',
    table: 'public.orders',
    pk_value: '1187',
    column_name: 'region',
    before_value: 'Wst',
    proposed_value: 'West',
    current_value: null,
    note: null,
    created_at: '2026-09-19T14:11:00Z',
  },
]

const THREADS = [
  { id: 7, title: 'Regional totals', created_at: '2026-09-27T10:05:00Z' },
  { id: 6, title: 'Shipment gaps', created_at: '2026-09-25T15:22:00Z' },
]

const SCHEMA = [
  ['public', 'orders', 'order_id', 'integer', 'One row per order', true],
  ['public', 'orders', 'region', 'text', 'Sales region', true],
  ['public', 'orders', 'amount', 'numeric', 'Order total, in dollars', true],
  ['public', 'orders', 'ordered_on', 'date', null, true],
  ['public', 'shipments', 'shipment_id', 'integer', null, true],
  ['public', 'shipments', 'units', 'integer', 'Units shipped. Nullable', true],
  ['public', 'customers', 'customer_id', 'integer', null, true],
  ['public', 'customers', 'name', 'text', null, true],
  ['public', 'customers', 'credit_note', 'text', 'Internal only', false],
].map(([s, t, c, d, desc, pub]) => ({
  schema_name: s,
  table_name: t,
  column_name: c,
  data_type: d,
  description: desc,
  is_public: pub,
}))

// ------------------------------------------------- the five answer modes --

/** Matched in order; the first whose test passes wins. Everything else falls
 *  through to the plain answer at the end. */
const ANSWERS = [
  {
    test: /password|credential|secret|connection string/i,
    build: (id) => ({
      mode: 'blocked',
      message_id: id,
      refused_by: 'layer 1: the intent gatekeeper',
      reason:
        'That asks for credentials. It was refused before anything was generated, so no SQL for it exists.',
      layer: 1,
      // Absent, deliberately: there is no statement, because none was written.
      statement: null,
    }),
  },
  {
    test: /drop|delete|truncate|update .* set/i,
    build: (id) => ({
      mode: 'blocked',
      message_id: id,
      refused_by: 'layer 2: the AST validator',
      reason: 'Only a single read-only SELECT may run. This is not one.',
      layer: 2,
      statement: 'DELETE FROM public.orders WHERE region = \'West\'',
    }),
  },
  {
    test: /\bbest\b/i,
    build: (id) => ({
      mode: 'clarify',
      message_id: id,
      term: 'best',
      note: 'Three readings of that give three different months. One round, then it commits.',
      options: [
        { value: 'highest total', label: 'Highest total', detail: 'June — 566,940.00' },
        { value: 'fastest growth', label: 'Fastest growth', detail: 'April — up 31%' },
        { value: 'most orders', label: 'Most orders', detail: 'November — 214 orders' },
      ],
    }),
  },
  {
    test: /churn|profit margin|forecast|next quarter/i,
    build: (id) => ({
      mode: 'refusal',
      message_id: id,
      reason:
        'Nothing in this warehouse records that. The tables hold orders, shipments and customers — there is no cost column, so a margin cannot be computed from them.',
      what_would_help:
        'A cost or unit-cost column on orders, or a costs table keyed by product.',
    }),
  },
  {
    test: /no units|missing|without units|not recorded/i,
    build: (id) => ({
      mode: 'answer',
      message_id: id,
      columns: ['shipment_id', 'shipped_on', 'units'],
      rows: [
        [2291, '2026-08-14', null],
        [2304, '2026-08-27', null],
        [2318, '2026-09-03', null],
      ],
      row_count: 3,
      truncated: false,
      chart: { chart: 'table', label_column: null, value_column: null, reason: 'Three rows, one of them a gap' },
      explanation:
        'Three shipments have no units recorded. Rows with no value are excluded rather than counted as zero.',
      source_columns: ['public.shipments.units'],
      sql: 'SELECT shipment_id, shipped_on, units\n  FROM public.shipments\n WHERE units IS NULL\n ORDER BY shipped_on',
      route: 'local',
      generator: 'codet5-small',
      confidence: 0.998,
      latency_ms: 1412,
      notes: ['Three rows are short of data. The table marks each gap and offers to fill it.'],
      includes_pending_edit: false,
    }),
  },
  {
    test: /month|monthly|over time|trend/i,
    build: (id) => ({
      mode: 'answer',
      message_id: id,
      columns: ['month', 'total_amount'],
      rows: [
        ['2026-04', 412300.0],
        ['2026-05', 438910.0],
        ['2026-06', 566940.0],
        ['2026-07', 501220.0],
        ['2026-08', 522480.0],
        ['2026-09', 489110.0],
      ],
      row_count: 6,
      truncated: false,
      chart: {
        chart: 'line',
        label_column: 'month',
        value_column: 'total_amount',
        reason: 'A measure over an ordered time axis',
      },
      explanation:
        'Monthly order totals run from 412,300.00 in April to 489,110.00 in September, peaking at 566,940.00 in June.',
      source_columns: ['public.orders.amount', 'public.orders.ordered_on'],
      sql: "SELECT to_char(ordered_on, 'YYYY-MM') AS month, SUM(amount) AS total_amount\n  FROM public.orders\n GROUP BY 1\n ORDER BY 1",
      route: 'local',
      generator: 'codet5-small',
      confidence: 0.91,
      latency_ms: 1380,
      notes: [],
      includes_pending_edit: false,
    }),
  },
  {
    test: /supplier|vendor|lead time/i,
    build: (id) => ({
      // Low confidence: the answer is given, the route reads `fallback`, and
      // the strip states the assumption so the reader can check it before
      // quoting the number.
      mode: 'answer',
      message_id: id,
      columns: ['supplier', 'avg_lead_days'],
      rows: [
        ['Redline Components', 6.4],
        ['Barrier Films Ltd', 9.1],
        ['Harbor Supply', 11.8],
      ],
      row_count: 3,
      truncated: false,
      chart: {
        chart: 'bar',
        label_column: 'supplier',
        value_column: 'avg_lead_days',
        reason: 'A measure across a small set of labels',
      },
      explanation:
        'Redline Components has the shortest average lead time at 6.4 days; Harbor Supply the longest at 11.8.',
      source_columns: ['public.shipments.shipped_on', 'public.orders.ordered_on'],
      sql: 'SELECT s.supplier, AVG(s.shipped_on - o.ordered_on) AS avg_lead_days\n  FROM public.shipments s\n  JOIN public.orders o ON o.order_id = s.order_id\n GROUP BY 1\n ORDER BY 2',
      route: 'fallback',
      generator: 'gemma3:4b',
      confidence: 0.41,
      latency_ms: 24_700,
      notes: [
        'Lead time was read as the days between the order date and the shipment date. Check that assumption before quoting the number.',
      ],
      includes_pending_edit: false,
    }),
  },
]

const PLAIN_ANSWER = (id) => ({
  mode: 'answer',
  message_id: id,
  columns: ['region', 'total_amount'],
  rows: [
    ['West', 482140.0],
    ['North', 391020.0],
    ['South', 298470.0],
    ['East', 264880.0],
  ],
  row_count: 4,
  truncated: false,
  chart: {
    chart: 'bar',
    label_column: 'region',
    value_column: 'total_amount',
    reason: 'A measure across a small set of labels',
  },
  explanation:
    'West had the highest total order amount at 482,140.00, ahead of North at 391,020.00.',
  source_columns: ['public.orders.region', 'public.orders.amount'],
  sql: 'SELECT region, SUM(amount) AS total_amount\n  FROM public.orders\n GROUP BY region\n ORDER BY total_amount DESC',
  route: 'local',
  generator: 'codet5-small',
  confidence: 1.0,
  latency_ms: 1189,
  notes: [],
  includes_pending_edit: false,
})

let nextMessageId = 100

/** Exported so the contract test can assert every mode, not just the one a
 *  particular question happens to hit. */
export function answerFor(question, { canSeeSql = true } = {}) {
  const id = nextMessageId++
  const match = ANSWERS.find((a) => a.test.test(question))
  const body = match ? match.build(id) : PLAIN_ANSWER(id)
  // The server removes `sql` for a viewer. The mock removes it too — if it
  // did not, the Workbench would be built against a field that never arrives.
  if (body.mode === 'answer' && !canSeeSql) body.sql = null
  return body
}

export const MODE_SAMPLES = () => [
  answerFor('Which region had the highest total order amount?'),
  answerFor('What is the database password?'),
  answerFor('Which was the best month?'),
  answerFor('What is our profit margin?'),
  answerFor('How many shipments have no units recorded?'),
  answerFor('Monthly order totals'),
  answerFor('Average supplier lead time'),
]

// ---------------------------------------------------- the transport, then --

let session = null
let people = structuredClone(PEOPLE)
let merges = structuredClone(MERGES)
let connections = structuredClone(CONNECTIONS)

export const auth = {
  start: async (email) => {
    const domain = String(email).split('@')[1] ?? ''
    const free = /^(gmail|outlook|yahoo|hotmail|proton|icloud)\./i.test(domain)
    if (free)
      return reply({
        path: 'personal_workspace',
        reason:
          'That is a personal address, so this becomes a personal workspace. It is yours alone — a personal workspace has no domain, and a domain that is empty can never be matched, so nobody else on that provider can reach it.',
        organisation_name: null,
        domain: null,
        resend_after_seconds: 60,
      })
    if (domain === 'northwind.co')
      return reply({
        path: 'join_existing',
        reason: 'Northwind Group is already here. Your request goes to its owner.',
        organisation_name: 'Northwind Group',
        domain,
        resend_after_seconds: 60,
      })
    return reply({
      path: 'create_company',
      reason: 'Nobody from that domain is here yet.',
      organisation_name: null,
      domain,
      resend_after_seconds: 60,
    })
  },

  verify: async (email, code) => {
    if (code !== '000000' && !/^\d{6}$/.test(code))
      throw new ApiError({ kind: KINDS.REFUSED, message: 'That code is not right.', status: 400 })
    session = {
      access_token: 'mock.access',
      refresh_token: 'mock.refresh',
      token_type: 'bearer',
      expires_in: 3600,
    }
    return reply(session)
  },

  me: async () =>
    reply({
      person_id: 1,
      email: 'garv@northwind.co',
      product_role: 'owner',
      state: 'active',
      organisation: ORG,
    }),

  logout: async () => {
    session = null
    return null
  },

  session: () => session,
}

export const org = {
  get: () => reply(ORG),
  people: () => reply(people),
  invite: async (email, grants = []) => {
    const person = {
      id: Math.max(...people.map((p) => p.id)) + 1,
      email,
      product_role: 'member',
      state: 'invited',
      grants: grants.map((g) => ({
        connection_id: g.connection_id,
        connection_name:
          connections.find((c) => c.id === g.connection_id)?.name ?? 'Unknown',
        db_role: g.db_role,
      })),
    }
    people = [...people, person]
    return reply(person)
  },
  approve: async (personId) => {
    people = people.map((p) => (p.id === personId ? { ...p, state: 'active' } : p))
    return reply(people.find((p) => p.id === personId))
  },
  grant: async (personId, connectionId, dbRole) => {
    people = people.map((p) => {
      if (p.id !== personId) return p
      const rest = p.grants.filter((g) => g.connection_id !== connectionId)
      return {
        ...p,
        grants: [
          ...rest,
          {
            connection_id: connectionId,
            connection_name:
              connections.find((c) => c.id === connectionId)?.name ?? 'Unknown',
            db_role: dbRole,
          },
        ],
      }
    })
    return reply(people.find((p) => p.id === personId))
  },
  revoke: async (personId, connectionId) => {
    people = people.map((p) =>
      p.id === personId
        ? { ...p, grants: p.grants.filter((g) => g.connection_id !== connectionId) }
        : p,
    )
    return null
  },
}

export const connectionsApi = {
  list: () => reply(connections),
  register: async (form) => {
    // The four checks, one at a time — and a refusal that names which one
    // failed, because "connection failed" is the least useful message a form
    // can give.
    const privileged = /^(postgres|root|admin)$/i.test(form.username || '')
    const checks = [
      { check: 'host', passed: true, detail: `${form.host} resolved to a public address` },
      { check: 'tls', passed: true, detail: 'TLS required and negotiated' },
      { check: 'query', passed: true, detail: 'SELECT 1 returned' },
      {
        check: 'privileges',
        passed: !privileged,
        detail: privileged
          ? `${form.username} can write. SpeakQL will not hold a role that can write.`
          : `${form.username} is read-only`,
      },
    ]
    const accepted = checks.every((c) => c.passed)
    if (!accepted)
      return reply(
        {
          accepted: false,
          checks,
          connection: null,
          refused_because: 'The account offered can write. Give it a read-only role.',
        },
        520,
      )
    const connection = {
      id: Math.max(...connections.map((c) => c.id)) + 1,
      name: form.name,
      kind: 'external',
      database_name: form.database_name,
      host: form.host,
      table_count: 0,
      row_estimate: null,
      created_at: new Date().toISOString(),
    }
    connections = [...connections, connection]
    return reply({ accepted: true, checks, connection, refused_because: null }, 520)
  },
  reindex: async () => reply({ indexed: true, tables: 6 }),
  schema: () => reply(SCHEMA),
}

export const datasets = {
  plan: () => reply({ tables: 1, columns: 7, rows: 1840, warnings: [] }),
  load: () => reply({ loaded: true, rows: 1840 }),
}

export const ask = {
  send: async (payload) => {
    const raised = raiseFault()
    if (raised) {
      await sleep(LATENCY)
      throw raised
    }
    // A clarification choice means the round is over: commit rather than ask
    // again. One round only.
    if (payload.clarification_choice) {
      const body = PLAIN_ANSWER(nextMessageId++)
      body.notes = [`Understood as: ${payload.clarification_choice}`]
      return reply(body, 900)
    }
    return reply(answerFor(payload.question), 900)
  },
  threads: () => reply(THREADS),
  thread: (threadId) =>
    reply([
      {
        id: 91,
        question: 'Which region had the highest total order amount?',
        created_at: '2026-09-27T10:05:00Z',
      },
      { id: 92, question: 'And by month?', created_at: '2026-09-27T10:07:00Z' },
    ]),
  feedback: async () => null,
}

export const corrections = {
  propose: async (edit) =>
    reply({
      accepted: true,
      landed: 'real_table',
      edit_log_id: 88,
      merge_request_id: null,
      before_value: null,
      after_value: edit.after_value,
      reason: null,
    }),
  merges: () => reply(merges),
  decide: async (mergeId, decision) => {
    const target = merges.find((m) => m.id === mergeId)
    // A merge whose row moved underneath it is refused rather than applied.
    // That was a real data-loss bug; the mock reproduces the refusal so the
    // stale card can be built against it.
    if (target.state === 'stale' && decision === 'approve')
      throw new ApiError({
        kind: KINDS.REFUSED,
        message:
          'The row moved since this was raised. Re-raise it against the current value.',
        status: 409,
      })
    const state =
      decision === 'approve' ? 'merged' : decision === 'reject' ? 'rejected' : 'open'
    merges = merges.map((m) => (m.id === mergeId ? { ...m, state } : m))
    return reply(merges.find((m) => m.id === mergeId))
  },
}

export const meta = {
  health: () =>
    reply({
      status: 'ok',
      version: '1.0.0',
      env: 'mock',
      databases: {
        speakql_app: 'ok',
        speakql_ro: 'ok',
        speakql_write: 'ok',
        speakql_edits_rw: 'ok',
        speakql_upload_ddl: 'ok',
      },
      llm: { generator: 'codet5-small', retriever: 'minilm', fallback: 'gemma3:4b' },
    }),
}

// `connections` is a module-level binding above, so the export is renamed
// here rather than shadowing it.
export { connectionsApi as connections }
