import { useState, useEffect, useCallback } from 'react'
import { fetchFeedback, patchFeedback } from '../api/admin'
import type { AdminFeedbackItem } from '../types'
import StatusBadge from '../components/StatusBadge'
import UserProfile from '../components/UserProfile'
import UndoAction from '../components/UndoAction'
import { exportCSV } from '../utils/export'
import { useApp } from '../context'

const TABS = [
  { value: '', label: '全部' },
  { value: 'pending', label: '待处理' },
  { value: 'processed', label: '已处理' },
  { value: 'rejected', label: '已驳回' },
]

export default function FeedbackPage() {
  const { addToast } = useApp()
  const [statusFilter, setStatusFilter] = useState('')
  const [items, setItems] = useState<AdminFeedbackItem[]>([])
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<AdminFeedbackItem | null>(null)
  const [undoAction, setUndoAction] = useState<{ id: number; status: 'processed' | 'rejected' } | null>(null)

  const load = useCallback(async (status: string) => {
    setLoading(true)
    try {
      const res = await fetchFeedback(status || undefined)
      setItems(res.items)
    } catch {
      addToast('加载申诉列表失败', 'error')
    } finally {
      setLoading(false)
    }
  }, [addToast])

  useEffect(() => { load(statusFilter) }, [statusFilter, load])

  const handleAction = (status: 'processed' | 'rejected') => {
    if (!selected) return
    setUndoAction({ id: selected.id, status })
  }

  const confirmAction = async () => {
    if (!undoAction) return
    await patchFeedback(undoAction.id, undoAction.status)
    addToast(undoAction.status === 'processed' ? '已通过' : '已驳回', 'success')
  }

  const afterAction = () => {
    setUndoAction(null)
    setSelected(null)
    load(statusFilter)
  }

  const fmtTime = (s: string | null) => s ? new Date(s).toLocaleString() : '-'

  return (
    <div className="master-detail">
      <div className="master-list">
        <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <h2 style={{ fontSize: 16, fontWeight: 700 }}>申诉管理</h2>
            {items.length > 0 && (
              <button className="btn btn-sm btn-outline" onClick={() => {
                exportCSV('feedback.csv',
                  ['ID', '申诉人', '目标', '理由', '状态', '时间'],
                  items.map((i) => [String(i.id), String(i.reporter_tg_id), String(i.target_tg_id), i.reason, i.status, i.created_at || ''])
                )
                addToast('已导出 CSV', 'success')
              }}>
                📥 导出 CSV
              </button>
            )}
          </div>
          <div className="tabs">
            {TABS.map((t) => (
              <button key={t.value} className={`tab ${statusFilter === t.value ? 'active' : ''}`} onClick={() => { setStatusFilter(t.value); setSelected(null); setUndoAction(null) }}>
                {t.label}
              </button>
            ))}
          </div>
        </div>
        {loading ? (
          <div className="empty-state"><div className="empty-state-text">加载中...</div></div>
        ) : items.length === 0 ? (
          <div className="empty-state"><div className="empty-state-icon">📭</div><div className="empty-state-text">暂无申诉数据</div></div>
        ) : (
          <table className="data-table">
            <thead>
              <tr><th>ID</th><th>申诉人</th><th>目标</th><th>状态</th><th>时间</th></tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={item.id} className={selected?.id === item.id ? 'selected' : ''} onClick={() => { setSelected(item); setUndoAction(null) }}>
                  <td>{item.id}</td>
                  <td>{item.reporter_tg_id}</td>
                  <td>{item.target_tg_id}</td>
                  <td><StatusBadge status={item.status} /></td>
                  <td style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{fmtTime(item.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className={`detail-panel ${!selected ? 'empty' : ''}`}>
        {!selected ? (
          <span>← 选择一条申诉查看详情</span>
        ) : (
          <div>
            <div className="detail-section">
              <div className="detail-section-title">申诉详情</div>
              <div className="detail-row"><span className="label">ID</span><span className="value">{selected.id}</span></div>
              <div className="detail-row"><span className="label">申诉人</span><span className="value">{selected.reporter_tg_id}</span></div>
              <div className="detail-row"><span className="label">目标</span><span className="value">{selected.target_tg_id}</span></div>
              <div className="detail-row"><span className="label">状态</span><span className="value"><StatusBadge status={selected.status} /></span></div>
              <div className="detail-row"><span className="label">时间</span><span className="value">{fmtTime(selected.created_at)}</span></div>
              <div style={{ marginTop: 8 }}>
                <div className="label" style={{ fontSize: 12, marginBottom: 4 }}>理由</div>
                <div style={{ fontSize: 13, padding: 8, background: 'var(--bg)', borderRadius: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                  {selected.reason || '-'}
                </div>
              </div>
            </div>

            <UserProfile tgId={selected.target_tg_id} title="相关用户画像" />

            {selected.status === 'pending' && !undoAction && (
              <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
                <button className="btn btn-success" onClick={() => handleAction('processed')}>✓ 通过</button>
                <button className="btn btn-danger" onClick={() => handleAction('rejected')}>✗ 驳回</button>
              </div>
            )}
            {undoAction && (
              <UndoAction
                label={undoAction.status === 'processed' ? '将通过申诉' : '将驳回申诉'}
                onConfirm={confirmAction}
                onComplete={afterAction}
              />
            )}
          </div>
        )}
      </div>
    </div>
  )
}
