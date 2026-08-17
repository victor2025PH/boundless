import { useState, useRef, type FormEvent } from 'react'
import { useApp } from '../context'
import { fetchStats } from '../api/admin'

const MAX_ATTEMPTS = 5
const LOCKOUT_MS = 60_000
const FAIL_DELAY_MS = 2000

export default function LoginPage() {
  const { login } = useApp()
  const [key, setKey] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [lockUntil, setLockUntil] = useState(0)
  const [lockRemain, setLockRemain] = useState(0)
  const attempts = useRef(0)
  const lockTimer = useRef<ReturnType<typeof setInterval>>(undefined)

  const startLock = () => {
    const until = Date.now() + LOCKOUT_MS
    setLockUntil(until)
    setLockRemain(Math.ceil(LOCKOUT_MS / 1000))
    lockTimer.current = setInterval(() => {
      const rem = Math.ceil((until - Date.now()) / 1000)
      if (rem <= 0) {
        clearInterval(lockTimer.current)
        setLockUntil(0)
        setLockRemain(0)
        attempts.current = 0
      } else {
        setLockRemain(rem)
      }
    }, 1000)
  }

  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()
    if (!key.trim() || loading) return
    if (Date.now() < lockUntil) return

    setLoading(true)
    setError('')

    sessionStorage.setItem('admin_key', key.trim())

    try {
      await fetchStats()
      login(key.trim())
    } catch {
      sessionStorage.removeItem('admin_key')
      attempts.current++
      await new Promise((r) => setTimeout(r, FAIL_DELAY_MS))
      if (attempts.current >= MAX_ATTEMPTS) {
        startLock()
        setError('')
      } else {
        setError(`密钥无效或已失效 (${attempts.current}/${MAX_ATTEMPTS})`)
      }
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <form className="login-box" onSubmit={handleSubmit}>
        <div className="login-title">管理面板</div>
        <div className="login-sub">请输入管理密钥以继续</div>

        {lockRemain > 0 && (
          <div className="login-locked">
            登录已锁定，请 {lockRemain} 秒后重试
          </div>
        )}

        {error && <div className="login-error">{error}</div>}

        <input
          className="login-input"
          type="password"
          placeholder="管理密钥"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          disabled={loading || lockRemain > 0}
          autoFocus
        />

        <button
          className="login-btn"
          type="submit"
          disabled={loading || !key.trim() || lockRemain > 0}
        >
          {loading ? '验证中...' : '登录'}
        </button>
      </form>
    </div>
  )
}
