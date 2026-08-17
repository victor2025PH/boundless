import { useState, useEffect } from 'react'
import { fetchRiskTrend } from '../api/admin'
import type { AdminRiskTrendDay } from '../types'
import {
  AreaChart, Area, LineChart, Line, XAxis, YAxis, Tooltip, Legend,
  ResponsiveContainer,
} from 'recharts'
import { useApp } from '../context'

const DAYS_OPTIONS = [7, 14, 30, 90]

export default function RiskTrendPage() {
  const { addToast } = useApp()
  const [days, setDays] = useState(7)
  const [items, setItems] = useState<AdminRiskTrendDay[]>([])
  const [loading, setLoading] = useState(true)
  const [chartType, setChartType] = useState<'area' | 'line'>('area')

  useEffect(() => {
    setLoading(true)
    fetchRiskTrend(days)
      .then((r) => setItems(r.items))
      .catch(() => addToast('加载风险趋势失败', 'error'))
      .finally(() => setLoading(false))
  }, [days, addToast])

  const totals = items.reduce(
    (acc, d) => ({
      total: acc.total + d.low + d.medium + d.high + d.extreme,
      high: acc.high + d.high + d.extreme,
    }),
    { total: 0, high: 0 }
  )
  const highRate = totals.total > 0 ? Math.round((totals.high / totals.total) * 100) : 0

  return (
    <div>
      <h2 style={{ fontSize: 16, fontWeight: 700, marginBottom: 16 }}>风险趋势</h2>

      <div style={{ display: 'flex', gap: 12, marginBottom: 16, alignItems: 'center' }}>
        <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>时间范围：</span>
        {DAYS_OPTIONS.map((d) => (
          <button key={d} className={`btn btn-sm ${days === d ? 'btn-primary' : 'btn-outline'}`} onClick={() => setDays(d)}>
            {d} 天
          </button>
        ))}
        <span style={{ marginLeft: 'auto' }} />
        <button className={`btn btn-sm ${chartType === 'area' ? 'btn-primary' : 'btn-outline'}`} onClick={() => setChartType('area')}>面积图</button>
        <button className={`btn btn-sm ${chartType === 'line' ? 'btn-primary' : 'btn-outline'}`} onClick={() => setChartType('line')}>折线图</button>
      </div>

      {loading ? (
        <div className="empty-state"><div className="empty-state-text">加载中...</div></div>
      ) : items.length === 0 ? (
        <div className="empty-state"><div className="empty-state-icon">📈</div><div className="empty-state-text">暂无趋势数据</div></div>
      ) : (
        <>
          <div className="card" style={{ padding: 20 }}>
            <ResponsiveContainer width="100%" height={360}>
              {chartType === 'area' ? (
                <AreaChart data={items}>
                  <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Legend />
                  <Area type="monotone" dataKey="low" name="低" stackId="1" stroke="#22c55e" fill="#bbf7d0" />
                  <Area type="monotone" dataKey="medium" name="中" stackId="1" stroke="#eab308" fill="#fef08a" />
                  <Area type="monotone" dataKey="high" name="高" stackId="1" stroke="#f97316" fill="#fed7aa" />
                  <Area type="monotone" dataKey="extreme" name="极高" stackId="1" stroke="#ef4444" fill="#fecaca" />
                </AreaChart>
              ) : (
                <LineChart data={items}>
                  <XAxis dataKey="date" tick={{ fontSize: 11 }} />
                  <YAxis tick={{ fontSize: 11 }} />
                  <Tooltip />
                  <Legend />
                  <Line type="monotone" dataKey="low" name="低" stroke="#22c55e" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="medium" name="中" stroke="#eab308" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="high" name="高" stroke="#f97316" strokeWidth={2} dot={false} />
                  <Line type="monotone" dataKey="extreme" name="极高" stroke="#ef4444" strokeWidth={2} dot={false} />
                </LineChart>
              )}
            </ResponsiveContainer>
          </div>

          <div className="stats-grid" style={{ marginTop: 16 }}>
            <div className="card stat-card">
              <div className="stat-card-label">总查询数（{days}天）</div>
              <div className="stat-card-value">{totals.total.toLocaleString()}</div>
            </div>
            <div className="card stat-card">
              <div className="stat-card-label">高风险占比</div>
              <div className="stat-card-value" style={{ color: highRate > 20 ? 'var(--red)' : 'var(--green)' }}>
                {highRate}%
              </div>
            </div>
          </div>
        </>
      )}
    </div>
  )
}
