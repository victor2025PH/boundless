import { useState, useEffect } from 'react'
import { fetchPendingAlerts } from '../api/admin'
import type { PendingAlertItem } from '../types'
import { useApp } from '../context'

export default function AlertsPage() {
  const { addToast } = useApp()
  const [items, setItems] = useState<PendingAlertItem[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    fetchPendingAlerts()
      .then((r) => setItems(r.items))
      .catch(() => addToast('加载待发提醒失败', 'error'))
      .finally(() => setLoading(false))
  }, [addToast])

  const fmtTime = (s: string | null) => s ? new Date(s).toLocaleString() : '-'

  if (loading) return <div className="empty-state"><div className="empty-state-text">加载中...</div></div>

  return (
    <div>
      <h2 style={{ fontSize: 16, fontWeight: 700, marginBottom: 16 }}>待发提醒</h2>
      {items.length === 0 ? (
        <div className="empty-state"><div className="empty-state-icon">🔔</div><div className="empty-state-text">暂无待发提醒</div></div>
      ) : (
        <div className="card" style={{ overflow: 'auto' }}>
          <table className="data-table">
            <thead>
              <tr><th>ID</th><th>订阅者</th><th>目标</th><th>事件类型</th><th>语言</th><th>创建时间</th></tr>
            </thead>
            <tbody>
              {items.map((a) => (
                <tr key={a.id} style={{ cursor: 'default' }}>
                  <td>{a.id}</td>
                  <td>{a.subscriber_tg_id}</td>
                  <td>{a.target_tg_id}</td>
                  <td><span className="badge badge-medium">{a.event_type}</span></td>
                  <td>{a.subscriber_lang || '-'}</td>
                  <td style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{fmtTime(a.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
