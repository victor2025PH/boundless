import { useState, useEffect } from 'react'
import { fetchReporters } from '../api/admin'
import type { AdminReporterItem } from '../types'

const cache: { data: AdminReporterItem[]; ts: number } = { data: [], ts: 0 }
const CACHE_TTL = 5 * 60 * 1000

async function getReporters(): Promise<AdminReporterItem[]> {
  if (cache.data.length && Date.now() - cache.ts < CACHE_TTL) return cache.data
  const res = await fetchReporters(500)
  cache.data = res.items
  cache.ts = Date.now()
  return res.items
}

interface Props {
  reporterTgId: number
}

export default function ReporterBrief({ reporterTgId }: Props) {
  const [info, setInfo] = useState<AdminReporterItem | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    setLoading(true)
    getReporters()
      .then((all) => {
        const found = all.find((r) => r.reporter_tg_id === reporterTgId) || null
        setInfo(found)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
  }, [reporterTgId])

  if (loading) return <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>加载举报人信息...</div>

  const total = info?.report_count || 0
  const verified = info?.verified_count || 0
  const rate = total > 0 ? Math.round((verified / total) * 100) : 0
  const rateColor = rate >= 80 ? 'var(--green)' : rate >= 50 ? 'var(--yellow)' : 'var(--red)'

  return (
    <div className="profile-card">
      <div className="detail-section-title">举报人信誉 · {reporterTgId}</div>
      <div className="detail-row">
        <span className="label">历史举报</span>
        <span className="value">{total} 次</span>
      </div>
      <div className="detail-row">
        <span className="label">已核实</span>
        <span className="value">{verified} 次</span>
      </div>
      <div className="detail-row">
        <span className="label">核实率</span>
        <span className="value" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ color: rateColor, fontWeight: 700 }}>{rate}%</span>
          <span className="cred-bar">
            <span className="cred-bar-fill" style={{ width: `${rate}%`, background: rateColor }} />
          </span>
        </span>
      </div>
      {info?.last_at && (
        <div className="detail-row">
          <span className="label">最后举报</span>
          <span className="value">{info.last_at}</span>
        </div>
      )}
      {!info && (
        <div style={{ fontSize: 12, color: 'var(--text-secondary)', marginTop: 4 }}>无统计记录</div>
      )}
    </div>
  )
}
