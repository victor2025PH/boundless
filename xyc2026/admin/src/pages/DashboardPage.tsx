import { useState, useEffect } from 'react'
import { Link, useOutletContext } from 'react-router-dom'
import { fetchRiskTrend } from '../api/admin'
import type { AdminStats, AdminRiskTrendDay } from '../types'
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer } from 'recharts'

interface Ctx {
  stats: AdminStats | null
  refreshStats: () => void
}

export default function DashboardPage() {
  const { stats } = useOutletContext<Ctx>()
  const [trend, setTrend] = useState<AdminRiskTrendDay[]>([])

  useEffect(() => {
    fetchRiskTrend(7).then((r) => setTrend(r.items)).catch(() => {})
  }, [])

  if (!stats) return <div className="empty-state"><div className="empty-state-text">加载中...</div></div>

  const todoCards = [
    {
      label: '待处理举报',
      value: stats.reports_pending,
      to: '/reports?status=pending',
      urgent: stats.reports_pending > 0,
    },
    {
      label: '待处理申诉',
      value: stats.feedback_pending,
      to: '/feedback',
      urgent: stats.feedback_pending > 0,
    },
    {
      label: '待确认交易',
      value: stats.transactions_pending,
      to: '/transactions?status=pending',
      urgent: stats.transactions_pending > 0,
    },
  ]

  const metricCards = [
    { label: '今日查询', value: stats.queries_today },
    { label: '总用户数', value: stats.total_users },
    { label: '黑名单数', value: stats.blacklist_count },
    { label: '绑定地址数', value: stats.bound_wallets_count },
    { label: '开放流水用户', value: stats.users_with_flow_visible },
    { label: '流水快照', value: stats.flow_snapshots_count },
  ]

  return (
    <div>
      <h2 style={{ marginBottom: 20, fontSize: 18, fontWeight: 700 }}>工作台</h2>

      <div className="stats-grid" style={{ marginBottom: 24 }}>
        {todoCards.map((c) => (
          <div key={c.label} className={`card stat-card ${c.urgent ? 'urgent' : 'ok'}`}>
            <div className="stat-card-label">{c.label}</div>
            <div className="stat-card-value">{c.value}</div>
            {c.urgent && (
              <div className="stat-card-action">
                <Link to={c.to}>立即处理 →</Link>
              </div>
            )}
            {!c.urgent && (
              <div className="stat-card-action" style={{ color: 'var(--green)', fontSize: 12 }}>
                全部处理完毕
              </div>
            )}
          </div>
        ))}
      </div>

      <div className="stats-grid" style={{ marginBottom: 24 }}>
        {metricCards.map((c) => (
          <div key={c.label} className="card stat-card">
            <div className="stat-card-label">{c.label}</div>
            <div className="stat-card-value">{c.value.toLocaleString()}</div>
          </div>
        ))}
      </div>

      <div className="card mini-chart-section" style={{ padding: 20 }}>
        <h3>最近 7 天风险趋势</h3>
        {trend.length > 0 ? (
          <>
            <ResponsiveContainer width="100%" height={200}>
              <AreaChart data={trend}>
                <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Area type="monotone" dataKey="low" stackId="1" stroke="#22c55e" fill="#bbf7d0" />
                <Area type="monotone" dataKey="medium" stackId="1" stroke="#eab308" fill="#fef08a" />
                <Area type="monotone" dataKey="high" stackId="1" stroke="#f97316" fill="#fed7aa" />
                <Area type="monotone" dataKey="extreme" stackId="1" stroke="#ef4444" fill="#fecaca" />
              </AreaChart>
            </ResponsiveContainer>
            <Link to="/risk-trend" className="mini-chart-link">查看详细趋势 →</Link>
          </>
        ) : (
          <div style={{ padding: 40, textAlign: 'center', color: 'var(--text-secondary)' }}>
            暂无趋势数据
          </div>
        )}
      </div>
    </div>
  )
}
