/**
 * The API contract, in the form the interface can check.
 *
 * Backend Plan §12 — and, in the repository, `backend/api/schemas.py`, which
 * is the single definition of every request and response shape. This file is
 * that definition restated as data, so it can be executed.
 *
 * It exists for one reason, stated in Frontend Specification §24: the mock
 * and the API response must share one shape. When they drift, the interface
 * gets rewritten, which is the most expensive thing that can go wrong between
 * now and the review. Previously that was defended by a sentence of warning.
 * Now `contract.test.js` runs every mock response through `check()` below and
 * fails if a field is missing, extra, or the wrong type.
 *
 * The vocabulary is deliberately small:
 *   'str' 'int' 'num' 'bool' 'any'        a scalar
 *   ['one', 'a', 'b']                     a literal union
 *   ['arr', <shape>]                      an array of
 *   ['opt', <shape>]                      may be null or absent
 *   { field: <shape> }                    an object, exact — no extra keys
 */

// ------------------------------------------------------------------ auth --

export const Session = {
  access_token: 'str',
  refresh_token: 'str',
  token_type: ['one', 'bearer'],
  expires_in: 'int',
}

export const OrganisationOut = {
  id: 'int',
  name: 'str',
  kind: ['one', 'company', 'personal'],
  domain: ['opt', 'str'],
  people_count: 'int',
  database_count: 'int',
}

export const Me = {
  person_id: 'int',
  email: 'str',
  product_role: ['one', 'owner', 'member'],
  state: ['one', 'pending', 'active', 'suspended'],
  organisation: OrganisationOut,
}

export const SignupStarted = {
  path: [
    'one',
    'sign_in',
    'invitation',
    'join_existing',
    'create_company',
    'personal_workspace',
    'invite_required',
    'refused',
  ],
  reason: 'str',
  organisation_name: ['opt', 'str'],
  domain: ['opt', 'str'],
  resend_after_seconds: 'int',
}

// ---------------------------------------------------------- organisation --

export const GrantOut = {
  connection_id: 'int',
  connection_name: 'str',
  db_role: ['one', 'analyst', 'viewer'],
}

export const PersonOut = {
  id: 'int',
  email: 'str',
  product_role: ['one', 'owner', 'member'],
  state: 'str',
  grants: ['arr', GrantOut],
}

// ------------------------------------------------------------- databases --

/** Note what is absent: credentials. Not masked — absent. */
export const ConnectionOut = {
  id: 'int',
  name: 'str',
  kind: ['one', 'internal', 'external', 'uploaded'],
  database_name: 'str',
  host: ['opt', 'str'],
  table_count: 'int',
  row_estimate: ['opt', 'int'],
  created_at: 'str',
}

/** The four checks, reported one at a time. "Connection failed" is the least
 *  useful message a form can give. */
export const ConnectionCheck = {
  check: ['one', 'host', 'tls', 'query', 'privileges'],
  passed: 'bool',
  detail: 'str',
}

export const ConnectionResult = {
  accepted: 'bool',
  checks: ['arr', ConnectionCheck],
  connection: ['opt', ConnectionOut],
  refused_because: ['opt', 'str'],
}

// ---------------------------------------------------------------- asking --

export const ChartOut = {
  chart: ['one', 'number', 'bar', 'line', 'table'],
  label_column: ['opt', 'str'],
  value_column: ['opt', 'str'],
  reason: 'str',
}

/** One of five. The five are different shapes on purpose, so the interface
 *  cannot render a refusal as though it were an answer. */
export const AnswerOut = {
  mode: ['one', 'answer'],
  message_id: 'int',
  columns: ['arr', 'str'],
  rows: ['arr', ['arr', 'any']],
  row_count: 'int',
  truncated: 'bool',
  chart: ChartOut,
  explanation: 'str',
  source_columns: ['arr', 'str'],
  sql: ['opt', 'str'], // present only for an analyst. Removed server-side.
  route: 'str',
  generator: 'str',
  confidence: ['opt', 'num'],
  latency_ms: 'int',
  notes: ['arr', 'str'],
  includes_pending_edit: 'bool',
}

export const ClarifyOut = {
  mode: ['one', 'clarify'],
  message_id: 'int',
  term: 'str',
  note: 'str',
  options: ['arr', 'any'],
}

export const BlockedOut = {
  mode: ['one', 'blocked'],
  message_id: 'int',
  refused_by: 'str',
  reason: 'str',
  layer: 'int',
  // Absent when layer 1 caught it, which is how the interface shows the
  // difference between "refused before any SQL existed" and "refused after"
  // without a word of explanation.
  statement: ['opt', 'str'],
}

export const RefusalOut = {
  mode: ['one', 'refusal'],
  message_id: 'int',
  reason: 'str',
  what_would_help: ['opt', 'str'],
}

/** A failure, not a refusal: retry strip, never a seal band (§13). */
export const FailureOut = {
  mode: ['one', 'failure'],
  reason: 'str',
  retryable: 'bool',
  retry_after_seconds: ['opt', 'int'],
}

export const AskResponse = {
  answer: AnswerOut,
  clarify: ClarifyOut,
  blocked: BlockedOut,
  refusal: RefusalOut,
  failure: FailureOut,
}

/** One event per pipeline step, so the ladder can name what is happening as
 *  it happens rather than showing an undifferentiated spinner. */
export const PipelineStep = {
  step: 'int',
  name: 'str',
  state: ['one', 'working', 'done', 'skipped', 'failed'],
  detail: ['opt', 'str'],
  elapsed_ms: ['opt', 'int'],
}

// ----------------------------------------------------------- corrections --

export const EditResult = {
  accepted: 'bool',
  landed: ['one', 'real_table', 'overlay', 'refused'],
  edit_log_id: ['opt', 'int'],
  merge_request_id: ['opt', 'int'],
  before_value: ['opt', 'str'],
  after_value: ['opt', 'str'],
  reason: ['opt', 'str'],
}

export const MergeOut = {
  id: 'int',
  state: ['one', 'open', 'merged', 'rejected', 'stale'],
  raised_by: 'str',
  table: 'str',
  pk_value: 'str',
  column_name: 'str',
  before_value: ['opt', 'str'],
  proposed_value: 'str',
  current_value: ['opt', 'str'], // populated only when the row moved
  note: ['opt', 'str'],
  created_at: 'str',
}

// --------------------------------------------------------------- threads --

export const ThreadOut = { id: 'int', title: 'str', created_at: 'str' }

export const MessageOut = { id: 'int', question: 'str', created_at: 'str' }

export const ColumnOut = {
  schema_name: 'str',
  table_name: 'str',
  column_name: 'str',
  data_type: 'str',
  description: ['opt', 'str'],
  is_public: 'bool',
}

export const HealthOut = {
  status: ['one', 'ok', 'degraded'],
  version: 'str',
  env: 'str',
  databases: 'any',
  llm: 'any',
}

/** Every 4xx and 5xx uses this. One shape, so the interface has one place to
 *  render a problem. */
export const ErrorOut = { detail: 'str', code: ['opt', 'str'] }

// ----------------------------------------------------------- the checker --

const SCALARS = {
  str: (v) => typeof v === 'string',
  int: (v) => typeof v === 'number' && Number.isInteger(v),
  num: (v) => typeof v === 'number' && Number.isFinite(v),
  bool: (v) => typeof v === 'boolean',
  any: () => true,
}

/**
 * Walk a value against a shape and collect every problem, rather than
 * throwing on the first. A report naming three wrong fields is worth three
 * runs of a report naming one.
 *
 * @returns {string[]} empty when the value matches
 */
export function check(shape, value, path = '$') {
  const problems = []

  if (typeof shape === 'string') {
    if (!SCALARS[shape]) return [`${path}: unknown shape '${shape}'`]
    if (!SCALARS[shape](value))
      problems.push(`${path}: expected ${shape}, got ${describe(value)}`)
    return problems
  }

  if (Array.isArray(shape)) {
    const [kind, ...rest] = shape

    if (kind === 'opt') {
      if (value === null || value === undefined) return []
      return check(rest[0], value, path)
    }

    if (kind === 'one') {
      if (!rest.includes(value))
        problems.push(
          `${path}: expected one of ${rest.map((s) => JSON.stringify(s)).join(', ')}, got ${describe(value)}`,
        )
      return problems
    }

    if (kind === 'arr') {
      if (!Array.isArray(value)) {
        problems.push(`${path}: expected an array, got ${describe(value)}`)
        return problems
      }
      value.forEach((item, i) => {
        problems.push(...check(rest[0], item, `${path}[${i}]`))
      })
      return problems
    }

    return [`${path}: unknown shape kind '${kind}'`]
  }

  if (typeof shape === 'object' && shape !== null) {
    if (typeof value !== 'object' || value === null || Array.isArray(value)) {
      problems.push(`${path}: expected an object, got ${describe(value)}`)
      return problems
    }
    for (const [key, sub] of Object.entries(shape)) {
      const optional = Array.isArray(sub) && sub[0] === 'opt'
      if (!(key in value)) {
        if (!optional) problems.push(`${path}.${key}: missing`)
        continue
      }
      problems.push(...check(sub, value[key], `${path}.${key}`))
    }
    // extra="forbid" on the server side; the same rule here. A field the mock
    // invents is a field a screen will read and the API will never send.
    for (const key of Object.keys(value)) {
      if (!(key in shape)) problems.push(`${path}.${key}: unexpected field`)
    }
    return problems
  }

  return [`${path}: unusable shape`]
}

function describe(value) {
  if (value === null) return 'null'
  if (value === undefined) return 'undefined'
  if (Array.isArray(value)) return 'an array'
  return typeof value
}

/** Throwing form, for the places that want to stop rather than report. */
export function assertShape(shape, value, label = 'value') {
  const problems = check(shape, value)
  if (problems.length)
    throw new Error(`${label} does not match the contract:\n  ` + problems.join('\n  '))
  return value
}
