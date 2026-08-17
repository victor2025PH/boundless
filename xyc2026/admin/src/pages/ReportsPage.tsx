import { useState, useEffect, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { fetchReports, patchReport, batchPatchReports } from '../api/admin'
import { invalidateCheckCache } from '../api/check'
import type { AdminReportItem } from '../types'
import StatusBadge from '../components/StatusBadge'
import CategoryBadge from '../components/CategoryBadge'
import UserProfile from '../components/UserProfile'
import ReporterBrief from '../components/ReporterBrief'
import UndoAction from '../components/UndoAction'
import { exportCSV } from '../utils/export'
import { useApp } from '../context'

const PAGE_SIZE = 50
const STATUS_TABS = [
  { value: '', label: '全部' },
  { value: 'pending', label: '待处理' },
  { value: 'verified', label: '已核实' },
  { value: 'rejected', label: '已驳回' },
]

export default function ReportsPage() {
  const { addToast } = useApp()
  const [params, setParams] = useSearchParams()
  const statusFilter = params.get('status') || ''

  const [items, setItems] = useState<AdminReportItem[]>([])
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<AdminReportItem | null>(null)
  const [undoAction, setUndoAction] = useState<{ id: number; status: 'verified' | 'rejected' } | null>(null)

  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [batchLoading, setBatchLoading] = useState(false)

  const load = useCallback(async (status: string, off: number) => {
    setLoading(true)
    try {
      const res = await fetchReports(status || undefined, PAGE_SIZE, off)
      setItems(res.items)
    } catch {
      addToast('加载举报列表失败', 'error')
    } finally {
      setLoading(false)
    }
  }, [addToast])

  useEffect(() => {
    load(statusFilter, offset)
    setChecked(new Set())
  }, [statusFilter, offset, load])

  const handleTabChange = (val: string) => {
    setParams(val ? { status: val } : {})
    setOffset(0)
    setSelected(null)
    setUndoAction(null)
    setChecked(new Set())
  }

  const toggleCheck = (id: number) => {
    setChecked((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleAll = () => {
    const pendingIds = items.filter((i) => i.status === 'pending').map((i) => i.id)
    if (pendingIds.every((id) => checked.has(id))) {
      setChecked(new Set())
    } else {
      setChecked(new Set(pendingIds))
    }
  }

  const handleBatch = async (status: 'verified' | 'rejected') => {
    if (checked.size === 0) return
    setBatchLoading(true)
    try {
      const res = await batchPatchReports(Array.from(checked), status)
      addToast(`批量操作完成：${res.updated} 条已${status === 'verified' ? '核实' : '驳回'}`, 'success')
      setChecked(new Set())
      setSelected(null)
      load(statusFilter, offset)
    } catch (err: unknown) {
      const msg = err && typeof err === 'object' && 'message' in err ? (err as { message: string }).message : '批量操作失败'
      addToast(msg, 'error')
    } finally {
      setBatchLoading(false)
    }
  }

  const handleAction = (status: 'verified' | 'rejected') => {
    if (!selected) return
    setUndoAction({ id: selected.id, status })
  }

  const confirmAction = async () => {
    if (!undoAction) return
    await patchReport(undoAction.id, undoAction.status)
    if (selected) invalidateCheckCache(selected.target_tg_id)
    addToast(
      undoAction.status === 'verified' ? '已设为核实' : '已设为驳回',
      'success'
    )
  }

  const afterAction = () => {
    setUndoAction(null)
    setSelected(null)
    load(statusFilter, offset)
  }

  const fmtTime = (s: string | null) => s ? new Date(s).toLocaleString() : '-'

  const pendingItems = items.filter((i) => i.status === 'pending')
  const allPendingChecked = pendingItems.length > 0 && pendingItems.every((i) => checked.has(i.id))

  return (
    <div className="master-detail">
      <div className="master-list">
        <div style={{ padding: '12px 16px', borderBottom: '1px solid var(--border)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
            <h2 style={{ fontSize: 16, fontWeight: 700 }}>举报管理</h2>
            {items.length > 0 && (
              <button className="btn btn-sm btn-outline" onClick={() => {
                exportCSV('reports.csv',
                  ['ID', '举报人', '被举报人', '分类', '理由', '状态', '时间'],
                  items.map((i) => [String(i.id), String(i.reporter_tg_id), String(i.target_tg_id), i.category || '', i.reason, i.status, i.created_at || ''])
                )
                addToast('已导出 CSV', 'success')
              }}>
                📥 导出
              </button>
            )}
          </div>
          <div className="tabs">
            {STATUS_TABS.map((t) => (
              <button
                key={t.value}
                className={`tab ${statusFilter === t.value ? 'active' : ''}`}
                onClick={() => handleTabChange(t.value)}
              >
                {t.label}
              </button>
            ))}
          </div>
          {checked.size > 0 && (
            <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginTop: 8, padding: '8px 0' }}>
              <span style={{ fontSize: 13, fontWeight: 600 }}>已选 {checked.size} 条</span>
              <button className="btn btn-sm btn-success" disabled={batchLoading} onClick={() => handleBatch('verified')}>
                批量核实
              </button>
              <button className="btn btn-sm btn-danger" disabled={batchLoading} onClick={() => handleBatch('rejected')}>
                批量驳回
              </button>
              <button className="btn btn-sm btn-outline" onClick={() => setChecked(new Set())}>
                取消选择
              </button>
            </div>
          )}
        </div>

        {loading ? (
          <div style={{ padding: 20 }}>
            {[1,2,3,4,5].map((i) => <div key={i} className="skeleton-row" />)}
          </div>
        ) : items.length === 0 ? (
          <div className="empty-state">
            <div className="empty-state-icon">📭</div>
            <div className="empty-state-text">暂无举报数据</div>
          </div>
        ) : (
          <>
            <table className="data-table">
              <thead>
                <tr>
                  <th style={{ width: 36 }}>
                    <input type="checkbox" checked={allPendingChecked} onChange={toggleAll} title="全选待处理" />
                  </th>
                  <th>ID</th>
                  <th>被举报人</th>
                  <th>分类</th>
                  <th>状态</th>
                  <th>时间</th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <tr
                    key={item.id}
                    className={selected?.id === item.id ? 'selected' : ''}
                    onClick={() => { setSelected(item); setUndoAction(null) }}
                  >
                    <td onClick={(e) => e.stopPropagation()}>
                      {item.status === 'pending' && (
                        <input
                          type="checkbox"
                          checked={checked.has(item.id)}
                          onChange={() => toggleCheck(item.id)}
                        />
                      )}
                    </td>
                    <td>{item.id}</td>
                    <td>{item.target_tg_id}</td>
                    <td><CategoryBadge category={item.category} /></td>
                    <td><StatusBadge status={item.status} /></td>
                    <td style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{fmtTime(item.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="pagination">
              <button
                className="btn btn-sm btn-outline"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              >
                上一页
              </button>
              <span style={{ fontSize: 13, color: 'var(--text-secondary)' }}>
                {offset + 1} - {offset + items.length}
              </span>
              <button
                className="btn btn-sm btn-outline"
                disabled={items.length < PAGE_SIZE}
                onClick={() => setOffset(offset + PAGE_SIZE)}
              >
                下一页
              </button>
            </div>
          </>
        )}
      </div>

      <div className={`detail-panel ${!selected ? 'empty' : ''}`}>
        {!selected ? (
          <span>← 选择一条举报查看详情</span>
        ) : (
          <div>
            <div className="detail-section">
              <div className="detail-section-title">举报详情</div>
              <div className="detail-row"><span className="label">ID</span><span className="value">{selected.id}</span></div>
              <div className="detail-row"><span className="label">被举报人</span><span className="value">{selected.target_tg_id}</span></div>
              <div className="detail-row"><span className="label">举报人</span><span className="value">{selected.reporter_tg_id}</span></div>
              <div className="detail-row"><span className="label">分类</span><span className="value"><CategoryBadge category={selected.category} /></span></div>
              <div className="detail-row"><span className="label">状态</span><span className="value"><StatusBadge status={selected.status} /></span></div>
              <div className="detail-row"><span className="label">时间</span><span className="value">{fmtTime(selected.created_at)}</span></div>
              <div style={{ marginTop: 8 }}>
                <div className="label" style={{ fontSize: 12, marginBottom: 4 }}>理由</div>
                <div style={{ fontSize: 13, padding: 8, background: 'var(--bg)', borderRadius: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>
                  {selected.reason || '-'}
                </div>
              </div>
              {selected.evidence_data && Object.keys(selected.evidence_data).length > 0 && (
                <div style={{ marginTop: 8 }}>
                  <div className="label" style={{ fontSize: 12, marginBottom: 4 }}>证据</div>
                  <div style={{ fontSize: 12, padding: 8, background: 'var(--bg)', borderRadius: 4, whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: 120, overflow: 'auto' }}>
                    {JSON.stringify(selected.evidence_data, null, 2)}
                  </div>
                </div>
              )}
            </div>

            <UserProfile tgId={selected.target_tg_id} title="被举报人画像" />
            <ReporterBrief reporterTgId={selected.reporter_tg_id} />

            {selected.status === 'pending' && !undoAction && (
              <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
                <button className="btn btn-success" onClick={() => handleAction('verified')}>
                  ✓ 设为已核实
                </button>
                <button className="btn btn-danger" onClick={() => handleAction('rejected')}>
                  ✗ 设为驳回
                </button>
              </div>
            )}
            {selected.status === 'pending' && !undoAction && (
              <div style={{ fontSize: 11, color: 'var(--text-secondary)', marginTop: 6 }}>
                核实将影响被举报人信用分 · 驳回将计入举报人 rejected_count
              </div>
            )}
            {undoAction && (
              <UndoAction
                label={undoAction.status === 'verified' ? '将设为已核实' : '将设为已驳回'}
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
