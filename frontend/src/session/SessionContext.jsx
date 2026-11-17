/**
 * Session in context; everything else local to the screen (Spec §24).
 *
 * There is very little global state in this product — who you are, which
 * connection you are asking about, and which way the ground faces. Inventing
 * a store for three values would be the largest unnecessary decision
 * available, so this is a context and nothing more.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import api from '../api'
import { whenSessionLost } from '../api/http.js'

const SessionContext = createContext(null)

const THEME_KEY = 'speakql.theme'

export function SessionProvider({ children }) {
  const [me, setMe] = useState(null)
  const [connections, setConnections] = useState([])
  const [connectionId, setConnectionId] = useState(null)
  const [loading, setLoading] = useState(true)
  const [theme, setTheme] = useState(readTheme)

  // The ground turns over here and nowhere else: one attribute on <html>,
  // re-valuing the token block. No component knows which one is on.
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    try {
      localStorage.setItem(THEME_KEY, theme)
    } catch {
      /* a remembered theme is a convenience, not a requirement */
    }
  }, [theme])

  const signOut = useCallback(async () => {
    try {
      await api.auth.logout()
    } finally {
      setMe(null)
      setConnections([])
      setConnectionId(null)
    }
  }, [])

  // A token that died mid-conversation reaches the router once, from here,
  // rather than from each of five screens discovering it separately.
  useEffect(() => {
    whenSessionLost(() => {
      setMe(null)
      setConnections([])
      setConnectionId(null)
    })
  }, [])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const who = await api.auth.me()
      setMe(who)
      const list = await api.connections.list()
      setConnections(list)
      setConnectionId((current) => current ?? list[0]?.id ?? null)
    } catch {
      // Not an error worth showing: it means nobody is signed in, which is
      // the normal state of the sign-in screen.
      setMe(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    // No stored token means no round trip — the sign-in screen paints at once
    // instead of after a doomed request.
    if (!api.auth.session()) {
      setLoading(false)
      return
    }
    load()
  }, [load])

  const value = useMemo(() => {
    const connection = connections.find((c) => c.id === connectionId) ?? null
    return {
      me,
      loading,
      isOwner: me?.product_role === 'owner',
      organisation: me?.organisation ?? null,
      connections,
      connection,
      connectionId,
      setConnectionId,
      refreshConnections: async () => setConnections(await api.connections.list()),
      signedIn: Boolean(me),
      afterSignIn: load,
      signOut,
      theme,
      toggleTheme: () => setTheme((t) => (t === 'dark' ? 'light' : 'dark')),
    }
  }, [me, loading, connections, connectionId, theme, load, signOut])

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>
}

export function useSession() {
  const value = useContext(SessionContext)
  if (!value) throw new Error('useSession was called outside SessionProvider')
  return value
}

function readTheme() {
  try {
    const stored = localStorage.getItem(THEME_KEY)
    if (stored === 'dark' || stored === 'light') return stored
  } catch {
    /* fall through to the default */
  }
  // Dark by default, and not because of the system setting: an analyst holds
  // this screen for an hour at a stretch reading figures off it (§3.1).
  return 'dark'
}
