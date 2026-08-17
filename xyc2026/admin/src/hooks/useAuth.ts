import { useState, useCallback, useEffect } from 'react'
import { setOnUnauthorized } from '../api/client'

const KEY = 'admin_key'
const LAST_ACTIVE = 'admin_active'
const SESSION_TIMEOUT = 30 * 60 * 1000

export function useAuth() {
  const [authed, setAuthed] = useState(() => !!sessionStorage.getItem(KEY))

  const login = useCallback((key: string) => {
    sessionStorage.setItem(KEY, key)
    sessionStorage.setItem(LAST_ACTIVE, String(Date.now()))
    setAuthed(true)
  }, [])

  const logout = useCallback(() => {
    sessionStorage.removeItem(KEY)
    sessionStorage.removeItem(LAST_ACTIVE)
    setAuthed(false)
  }, [])

  useEffect(() => {
    setOnUnauthorized(logout)
  }, [logout])

  useEffect(() => {
    if (!authed) return
    const check = () => {
      const last = parseInt(sessionStorage.getItem(LAST_ACTIVE) || '0', 10)
      if (Date.now() - last > SESSION_TIMEOUT) logout()
    }
    const tick = setInterval(check, 60_000)
    const touch = () => sessionStorage.setItem(LAST_ACTIVE, String(Date.now()))
    window.addEventListener('click', touch)
    window.addEventListener('keydown', touch)
    return () => {
      clearInterval(tick)
      window.removeEventListener('click', touch)
      window.removeEventListener('keydown', touch)
    }
  }, [authed, logout])

  return { authed, login, logout }
}
