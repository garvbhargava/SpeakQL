/**
 * The one import every screen makes.
 *
 *     import api from '../api'
 *     const people = await api.org.people()
 *
 * No screen imports `live.js` or `mock.js` directly, and no screen imports
 * mock data at all. That is what makes switching between them a flag and a
 * base URL rather than a rewrite (Spec §24), and it is why the mock is worth
 * having: the seam is exercised on every screen, every day, instead of being
 * discovered on the day the backend arrives.
 */

import * as live from './live.js'
import * as mock from './mock.js'

const TRANSPORT = import.meta.env?.VITE_TRANSPORT ?? 'live'

const chosen = TRANSPORT === 'mock' ? mock : live

const api = {
  name: chosen.name,
  auth: chosen.auth,
  org: chosen.org,
  connections: chosen.connections,
  datasets: chosen.datasets,
  ask: chosen.ask,
  corrections: chosen.corrections,
  meta: chosen.meta,
  /** Only the mock has one. Undefined on live, which is the honest answer:
   *  you cannot ask a real server to fail on demand. */
  fault: TRANSPORT === 'mock' ? mock.setFault : undefined,
}

export default api
export { live, mock }
