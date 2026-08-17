import { useState, useEffect, useCallback } from 'react'
import { fetchTransactions } from '../api/admin'
import type { AdminTransactionItem } from '../types'
import StatusBadge from '../components/StatusBadge'
import { exportCSV } from '../utils/export'
import { useApp } from '../context'

const TABS = [
  { value: '', label: '全部' },
  { value: 'pending', label: '待确认' },
  { value: 'confirmed', label: '已确认' },
  { value: 'rejected', label: '已拒绝' },
]

const BIG_AMOUNT = 1000

export default function TransactionsPage() {
  const { addToast } = useApp()
  const [statusFilter, setStatusFilter] = useState('')
  const [items, setItems] = useState<AdminTransactionItem[]>([])
  const [loading, setLoading] = useState(true)

  const load = useCallback(async (status: string) => {
    setLoading(true)
    try {
      const res = await fetchTransactions(status || undefined)
      setItems(res.items)
    } catch {
      addToast('加载交易列表失败', 'error')
    } finally {
      setLoading(false)
    }
  }, [addToast])

  useEffect(() => { load(statusFilter) }, [statusFilter, load])

  const fmtTime = (s: string | null) => s ? new Date(s).toLocaleString() : '-'

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <h2 style={{ fontSize: 16, fontWeight: 700 }}>交易列表</h2>
        {items.length > 0 && (
          <button className="btn btn-sm btn-outline" onClick={() => {
            exportCSV('transactions.csv',
              ['ID', '发起方', '对方', '金额', '币种', '状态', '创建时间', '确认时间'],
              items.map((tx) => [String(tx.id), String(tx.initiator_tg_id), String(tx.counterparty_tg_id), tx.amount, tx.currency, tx.status, tx.created_at || '', tx.confirmed_at || ''])
            )
            addToast('已导出 CSV', 'success')
          }}>
            📥 导出 CSV
          </button>
        )}
      </div>
      <div className="tabs">
        {TABS.map((t) => (
          <button key={t.value} className={`tab ${statusFilter === t.value ? 'active' : ''}`} onClick={() => setStatusFilter(t.value)}>
            {t.label}
          </button>
        ))}
      </div>
      {loading ? (
        <div className="empty-state"><div className="empty-state-text">加载中...</div></div>
      ) : items.length === 0 ? (
        <div className="empty-state"><div className="empty-state-icon">💰</div><div className="empty-state-text">暂无交易数据</div></div>
      ) : (
        <div className="card" style={{ overflow: 'auto' }}>
          <table className="data-table">
            <thead>
              <tr><th>ID</th><th>发起方</th><th>对方</th><th style={{ textAlign: 'right' }}>金额</th><th>币种</th><th>状态</th><th>创建</th><th>确认</th></tr>
            </thead>
            <tbody>
              {items.map((tx) => {
                const amt = parseFloat(tx.amount)
                const isBig = !isNaN(amt) && amt >= BIG_AMOUNT
                return (
                  <tr key={tx.id} style={{ cursor: 'default' }}>
                    <td>{tx.id}</td>
                    <td>{tx.initiator_tg_id}</td>
                    <td>{tx.counterparty_tg_id}</td>
                    <td style={{ textAlign: 'right', fontWeight: isBig ? 700 : 400, color: isBig ? 'var(--orange)' : undefined }}>
                      {tx.amount}
                    </td>
                    <td>{tx.currency}</td>
                    <td><StatusBadge status={tx.status} /></td>
                    <td style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{fmtTime(tx.created_at)}</td>
                    <td style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{fmtTime(tx.confirmed_at)}</td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}
