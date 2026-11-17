/**
 * One route per screen, wrapped in a role guard mirroring `deps.require_role`
 * (Spec §24).
 *
 * Two route trees, one component library — not two applications to maintain.
 *
 * Worth saying plainly, because a reviewer will ask: **this guard is not a
 * security control.** It decides what is worth rendering. The server decides
 * what may happen, and a member who types an owner URL gets a 403 from the
 * API whatever this component does. What the guard buys is that they do not
 * get a screen full of empty panels and a console full of errors first.
 */

import { Navigate } from 'react-router-dom'
import { useSession } from './SessionContext.jsx'
import Loading from '../components/Loading.jsx'

export default function RoleGuard({ role, children }) {
  const { signedIn, isOwner, loading } = useSession()

  if (loading) return <Loading label="Checking what you can see" />
  if (!signedIn) return <Navigate to="/sign-in" replace />
  if (role === 'owner' && !isOwner) return <Navigate to="/ask" replace />

  return children
}
