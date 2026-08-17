import { useState, useEffect } from 'react'
import { checkUser } from '../api/check'
import type { CheckResult } from '../types'
import RiskBadge from './RiskBadge'

interface Props {
  tgId: number
  title?: string
}

export default function UserProfile({ tgId, title = '用户画像' }: Props) {
  const [data, setData] = useState<CheckResult | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    setData(null)
    checkUser(tgId)
      .then(setData)
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [tgId])

  if (loading) return <div className="profile-card" style={{ opacity: 0.6 }}>加载画像...</div>
  if (!data) return <div className="profile-card">无法获取画像</div>

  return (
    <div className="profile-card">
      <div className="detail-section-title">{title} · {tgId}</div>
      <div className="risk-badge">
        <RiskBadge level={data.risk} score={data.score} />
      </div>
      <div className="detail-row">
        <span className="label">判定</span>
        <span className="value">{data.verdict}</span>
      </div>
      <div className="detail-row">
        <span className="label">举报</span>
        <span className="value">
          待核实 {data.report_pending} · 已核实 {data.report_verified}
        </span>
      </div>
      <div className="detail-row">
        <span className="label">黑名单</span>
        <span className="value">{data.blacklist ? '🔴 是' : '否'}</span>
      </div>
      <div className="detail-row">
        <span className="label">交易</span>
        <span className="value">
          成功 {data.tx_success_count}/{data.tx_total_count}
          {data.tx_total_count > 0 && ` (${Math.round(data.tx_success_rate * 100)}%)`}
        </span>
      </div>
      {data.tags.length > 0 && (
        <div className="profile-tags">
          {data.tags.map((tag) => (
            <span key={tag} className="profile-tag">{tag}</span>
          ))}
        </div>
      )}
    </div>
  )
}
