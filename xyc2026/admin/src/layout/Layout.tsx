import { useState, useEffect, useCallback, useRef } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { useApp } from '../context'
import { fetchStats } from '../api/admin'
import ToastContainer from '../components/ToastContainer'
import type { AdminStats } from '../types'

const NAV = [
  { to: '/', label: '总览', icon: '📊' },
  { to: '/reports', label: '举报管理', icon: '🚨', badgeKey: 'reports_pending' as const },
  { to: '/feedback', label: '申诉管理', icon: '📋', badgeKey: 'feedback_pending' as const },
  { to: '/blacklist', label: '黑名单', icon: '🚫' },
  { to: '/reporters', label: '举报人列表', icon: '👤' },
  { to: '/transactions', label: '交易列表', icon: '💰' },
  { to: '/risk-trend', label: '风险趋势', icon: '📈' },
  { to: '/alerts', label: '待发提醒', icon: '🔔' },
]

export default function Layout() {
  const { logout } = useApp()
  const navigate = useNavigate()
  const [stats, setStats] = useState<AdminStats | null>(null)
  const [refreshedAt, setRefreshedAt] = useState('')
  const [theme, setTheme] = useState(() =>
    localStorage.getItem('theme') || 'light'
  )
  const [searchVal, setSearchVal] = useState('')
  const searchRef = useRef<HTMLInputElement>(null)

  const loadStats = useCallback(async () => {
    try {
      const s = await fetchStats()
      setStats(s)
      setRefreshedAt(new Date().toLocaleTimeString())
    } catch {}
  }, [])

  useEffect(() => {
    loadStats()
    const t = setInterval(loadStats, 30_000)
    return () => clearInterval(t)
  }, [loadStats])

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme)
    localStorage.setItem('theme', theme)
  }, [theme])

  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return
      if (e.key === '/') {
        e.preventDefault()
        searchRef.current?.focus()
        return
      }
      if (e.key === 'g' || e.key === 'G') {
        const next = (e2: KeyboardEvent) => {
          const map: Record<string, string> = {
            d: '/', r: '/reports', f: '/feedback', b: '/blacklist',
            p: '/reporters', t: '/transactions', k: '/risk-trend', a: '/alerts',
            s: '/lookup',
          }
          const path = map[e2.key.toLowerCase()]
          if (path) navigate(path)
          window.removeEventListener('keydown', next)
        }
        window.addEventListener('keydown', next, { once: true })
        setTimeout(() => window.removeEventListener('keydown', next), 1000)
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [navigate])

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault()
    const val = searchVal.trim()
    if (!val) return
    navigate(`/lookup?q=${encodeURIComponent(val)}`)
    setSearchVal('')
  }

  const getBadge = (key?: keyof AdminStats) => {
    if (!key || !stats) return null
    const val = stats[key]
    if (typeof val === 'number' && val > 0) return val
    return null
  }

  return (
    <div className="app-layout">
      <aside className="sidebar">
        <div className="sidebar-brand">管理面板</div>
        <nav className="sidebar-nav">
          {NAV.map((n) => {
            const badge = getBadge(n.badgeKey)
            return (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.to === '/'}
                className={({ isActive }) =>
                  `sidebar-link${isActive ? ' active' : ''}`
                }
              >
                <span>{n.icon}</span>
                <span>{n.label}</span>
                {badge && <span className="sidebar-badge">{badge}</span>}
              </NavLink>
            )
          })}
          <NavLink
            to="/lookup"
            className={({ isActive }) => `sidebar-link${isActive ? ' active' : ''}`}
          >
            <span>🔍</span>
            <span>用户查找</span>
          </NavLink>
        </nav>
        <div className="sidebar-footer">
          <button onClick={logout}>退出登录</button>
        </div>
      </aside>

      <div className="main-content">
        <header className="topbar">
          <form onSubmit={handleSearch} style={{ display: 'flex', gap: 6, marginRight: 'auto' }}>
            <input
              ref={searchRef}
              className="form-input"
              style={{ width: 220, padding: '6px 10px', fontSize: 13 }}
              type="text"
              placeholder="搜索用户 TG ID / 用户名 …  ( / )"
              value={searchVal}
              onChange={(e) => setSearchVal(e.target.value)}
            />
            <button className="topbar-btn" type="submit">搜索</button>
          </form>
          {refreshedAt && (
            <span className="topbar-time">刷新于 {refreshedAt}</span>
          )}
          <button className="topbar-btn" onClick={loadStats}>↻</button>
          <button
            className="topbar-btn"
            onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
          >
            {theme === 'dark' ? '☀️' : '🌙'}
          </button>
        </header>
        <div className="page-content">
          <Outlet context={{ stats, refreshStats: loadStats }} />
        </div>
      </div>

      <ToastContainer />
    </div>
  )
}
