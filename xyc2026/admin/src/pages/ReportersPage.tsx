import { useState, useEffect } from 'react'
import { fetchReporters, fetchReports } from '../api/admin'
import type { AdminReporterItem, AdminReportItem } from '../types'
import StatusBadge from '../components/StatusBadge'
import CategoryBadge from '../components/CategoryBadge'
import { useApp } from '../context'

export default function ReportersPage() {
  const { addToast } = useApp()
  const [items, setItems] = useState<AdminReporterItem[]>([])
  const [loading, setLoading] = useState(true)
  const [expanded, setExpanded] = useState<number | null>(null)
  const [drillReports, setDrillReports] = useState<AdminReportItem[]>([])
  const [drillLoading, setDrillLoading] = useState(false)

  useEffect(() => {
    fetchReporters(200)
      .then((r) => setItems(r.items))
      .catch(() => addToast('加载举报人列表失败', 'error'))
      .finally(() => setLoading(false))
  }, [addToast])

  const handleExpand = async (tgId: number) => {
    if (expanded === tgId) {
      setExpanded(null)
      setDrillReports([])
      return
    }
    setExpanded(tgId)
    setDrillLoading(true)
    try {
      const res = await fetchReports(undefined, 20, 0, tgId)
      setDrillReports(res.items)
    } catch {
      addToast('加载举报记录失败', 'error')
    } finally {
      setDrillLoading(false)
    }
  }

  const fmtTime = (s: string | null) => s ? new Date(s).toLocaleString() : '-'

  if (loading) return <div className="empty-state"><div className="empty-state-text">加载中...</div></div>

  return (
    <div>
      <h2 style={{ fontSize: 16, fontWeight: 700, marginBottom: 16 }}>举报人列表</h2>
      <p style={{ fontSize: 13, color: 'var(--text-secondary)', marginBottom: 16 }}>
        点击某行可展开该举报人的历史举报记录
      </p>
      {items.length === 0 ? (
        <div className="empty-state"><div className="empty-state-icon">👤</div><div className="empty-state-text">暂无举报人数据</div></div>
      ) : (
        <div className="card" style={{ overflow: 'auto' }}>
          <table className="data-table">
            <thead>
              <tr><th>举报人 TG ID</th><th>举报总数</th><th>已核实</th><th>核实率</th><th>最后举报</th><th></th></tr>
            </thead>
            <tbody>
              {items.map((r) => {
                const rate = r.report_count > 0 ? Math.round((r.verified_count / r.report_count) * 100) : 0
                const rateColor = rate >= 80 ? 'var(--green)' : rate >= 50 ? 'var(--yellow)' : 'var(--red)'
                const isExpanded = expanded === r.reporter_tg_id
                return (
                  <>
                    <tr
                      key={r.reporter_tg_id}
                      onClick={() => handleExpand(r.reporter_tg_id)}
                      style={{ cursor: 'pointer' }}
                      className={isExpanded ? 'selected' : ''}
                    >
                      <td style={{ fontWeight: 600 }}>{r.reporter_tg_id}</td>
                      <td>{r.report_count}</td>
                      <td>{r.verified_count}</td>
                      <td>
                        <span style={{ color: rateColor, fontWeight: 600 }}>{rate}%</span>
                        <span className="cred-bar" style={{ marginLeft: 8 }}>
                          <span className="cred-bar-fill" style={{ width: `${rate}%`, background: rateColor }} />
                        </span>
                      </td>
                      <td style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{r.last_at || '-'}</td>
                      <td style={{ fontSize: 12 }}>{isExpanded ? '▲' : '▼'}</td>
                    </tr>
                    {isExpanded && (
                      <tr key={`${r.reporter_tg_id}-drill`} style={{ cursor: 'default' }}>
                        <td colSpan={6} style={{ padding: 0, background: 'var(--bg)' }}>
                          <div style={{ padding: '12px 16px' }}>
                            <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8, color: 'var(--text-secondary)' }}>
                              该举报人的举报记录（最近 20 条）
                            </div>
                            {drillLoading ? (
                              <div style={{ padding: 12, fontSize: 13, color: 'var(--text-secondary)' }}>加载中...</div>
                            ) : drillReports.length === 0 ? (
                              <div style={{ padding: 12, fontSize: 13, color: 'var(--text-secondary)' }}>无举报记录</div>
                            ) : (
                              <table className="data-table" style={{ background: 'var(--bg-card)', borderRadius: 4 }}>
                                <thead>
                                  <tr><th>ID</th><th>被举报人</th><th>分类</th><th>理由</th><th>状态</th><th>时间</th></tr>
                                </thead>
                                <tbody>
                                  {drillReports.map((dr) => (
                                    <tr key={dr.id} style={{ cursor: 'default' }}>
                                      <td>{dr.id}</td>
                                      <td>{dr.target_tg_id}</td>
                                      <td><CategoryBadge category={dr.category} /></td>
                                      <td style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                        {dr.reason || '-'}
                                      </td>
                                      <td><StatusBadge status={dr.status} /></td>
                                      <td style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{fmtTime(dr.created_at)}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            )}
                          </div>
                        </td>
                      </tr>
                    )}
                  </>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
