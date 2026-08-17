import { useState, useEffect, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { checkUser, invalidateCheckCache } from '../api/check'
import { request } from '../api/client'
import type { CheckResult } from '../types'
import RiskBadge from '../components/RiskBadge'
import { useApp } from '../context'

export default function LookupPage() {
  const { addToast } = useApp()
  const [params] = useSearchParams()
  const initialQ = params.get('q') || ''
  const [query, setQuery] = useState(initialQ)
  const [data, setData] = useState<CheckResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [searched, setSearched] = useState(false)

  const doSearch = useCallback(async (q: string) => {
    if (!q.trim()) return
    setLoading(true)
    setSearched(true)
    setData(null)

    try {
      const isNum = /^\d+$/.test(q.trim())
      if (isNum) {
        const tgId = parseInt(q.trim(), 10)
        invalidateCheckCache(tgId)
        const res = await checkUser(tgId)
        setData(res)
      } else {
        const res = await request<CheckResult>(`/v1/check?username=${encodeURIComponent(q.trim())}`)
        setData(res)
      }
    } catch {
      addToast('未找到该用户', 'error')
    } finally {
      setLoading(false)
    }
  }, [addToast])

  useEffect(() => {
    if (initialQ) doSearch(initialQ)
  }, [initialQ, doSearch])

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    doSearch(query)
  }

  return (
    <div>
      <h2 style={{ fontSize: 16, fontWeight: 700, marginBottom: 16 }}>用户查找</h2>

      <form onSubmit={handleSubmit} style={{ display: 'flex', gap: 8, marginBottom: 24, maxWidth: 500 }}>
        <input
          className="form-input"
          type="text"
          placeholder="输入 Telegram ID 或用户名"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoFocus
        />
        <button className="btn btn-primary" type="submit" disabled={loading || !query.trim()}>
          {loading ? '查询中...' : '查询'}
        </button>
      </form>

      {loading && (
        <div className="card" style={{ padding: 40, textAlign: 'center' }}>
          <div className="skeleton-shimmer" style={{ height: 200 }} />
        </div>
      )}

      {!loading && searched && !data && (
        <div className="empty-state">
          <div className="empty-state-icon">🔍</div>
          <div className="empty-state-text">未找到该用户</div>
        </div>
      )}

      {data && (
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, maxWidth: 800 }}>
          <div className="card" style={{ padding: 20 }}>
            <div className="detail-section-title">基本信息</div>
            <div style={{ margin: '12px 0' }}>
              <RiskBadge level={data.risk} score={data.score} />
            </div>
            <div className="detail-row"><span className="label">TG ID</span><span className="value">{data.tg_id}</span></div>
            <div className="detail-row"><span className="label">用户名</span><span className="value">{data.username || '-'}</span></div>
            <div className="detail-row"><span className="label">判定</span><span className="value">{data.verdict}</span></div>
            <div className="detail-row"><span className="label">Premium</span><span className="value">{data.is_premium ? '是' : '否'}</span></div>
            <div className="detail-row"><span className="label">黑名单</span><span className="value" style={{ color: data.blacklist ? 'var(--red)' : undefined }}>{data.blacklist ? '🔴 是' : '否'}</span></div>
          </div>

          <div className="card" style={{ padding: 20 }}>
            <div className="detail-section-title">举报与交易</div>
            <div className="detail-row"><span className="label">举报总数</span><span className="value">{data.report_count}</span></div>
            <div className="detail-row"><span className="label">待核实</span><span className="value">{data.report_pending}</span></div>
            <div className="detail-row"><span className="label">已核实</span><span className="value">{data.report_verified}</span></div>
            <div className="detail-row"><span className="label">交易成功</span><span className="value">{data.tx_success_count}/{data.tx_total_count}</span></div>
            <div className="detail-row">
              <span className="label">成功率</span>
              <span className="value">
                {data.tx_total_count > 0 ? `${Math.round(data.tx_success_rate * 100)}%` : '-'}
              </span>
            </div>
          </div>

          <div className="card" style={{ padding: 20, gridColumn: '1 / -1' }}>
            <div className="detail-section-title">标签与提示</div>
            {data.tags.length > 0 ? (
              <div className="profile-tags" style={{ marginBottom: 12 }}>
                {data.tags.map((tag) => (
                  <span key={tag} className="profile-tag">{tag}</span>
                ))}
              </div>
            ) : (
              <div style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 12 }}>无标签</div>
            )}
            {data.tips.length > 0 && (
              <ul style={{ fontSize: 13, paddingLeft: 20, margin: 0, color: 'var(--text-secondary)' }}>
                {data.tips.map((tip, i) => <li key={i}>{tip}</li>)}
              </ul>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
