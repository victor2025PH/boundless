import { useState, type FormEvent } from 'react'
import { addBlacklist } from '../api/admin'
import UserProfile from '../components/UserProfile'
import { useApp } from '../context'

interface LocalRecord {
  tg_id: number
  added: boolean
  time: string
}

function getLocalRecords(): LocalRecord[] {
  try {
    return JSON.parse(sessionStorage.getItem('bl_records') || '[]')
  } catch { return [] }
}
function saveLocalRecord(r: LocalRecord) {
  const records = [r, ...getLocalRecords()].slice(0, 50)
  sessionStorage.setItem('bl_records', JSON.stringify(records))
}

export default function BlacklistPage() {
  const { addToast } = useApp()
  const [tgId, setTgId] = useState('')
  const [reason, setReason] = useState('')
  const [source, setSource] = useState('admin')
  const [loading, setLoading] = useState(false)
  const [preview, setPreview] = useState(false)
  const [records, setRecords] = useState<LocalRecord[]>(getLocalRecords)

  const numId = parseInt(tgId, 10)
  const valid = !isNaN(numId) && numId > 0

  const handlePreview = (e: FormEvent) => {
    e.preventDefault()
    if (!valid) return
    setPreview(true)
  }

  const handleConfirm = async () => {
    if (!valid || loading) return

    const dup = records.find((r) => r.tg_id === numId)
    if (dup) {
      const ok = confirm(`该用户 (${numId}) 本次会话内已操作，确认继续？`)
      if (!ok) return
    }

    setLoading(true)
    try {
      const res = await addBlacklist(numId, reason || undefined, source)
      const rec: LocalRecord = { tg_id: numId, added: res.added, time: new Date().toLocaleString() }
      saveLocalRecord(rec)
      setRecords([rec, ...records].slice(0, 50))
      addToast(res.added ? '已加入黑名单' : '该用户已在黑名单中', res.added ? 'success' : 'info')
      setTgId('')
      setReason('')
      setPreview(false)
    } catch (err: unknown) {
      const msg = err && typeof err === 'object' && 'message' in err ? (err as { message: string }).message : '操作失败'
      addToast(msg, 'error')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{ padding: '0' }}>
      <h2 style={{ fontSize: 16, fontWeight: 700, marginBottom: 20 }}>黑名单管理</h2>

      <div style={{ display: 'flex', gap: 32, alignItems: 'flex-start' }}>
        <div className="blacklist-form">
          <form onSubmit={handlePreview}>
            <div className="form-group">
              <label className="form-label">Telegram ID *</label>
              <input className="form-input" type="text" placeholder="例如 123456789" value={tgId} onChange={(e) => { setTgId(e.target.value); setPreview(false) }} />
            </div>
            <div className="form-group">
              <label className="form-label">原因</label>
              <input className="form-input" type="text" placeholder="选填" value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
            <div className="form-group">
              <label className="form-label">来源</label>
              <select className="form-select" value={source} onChange={(e) => setSource(e.target.value)}>
                <option value="admin">admin</option>
                <option value="verified_reports">verified_reports</option>
                <option value="other">other</option>
              </select>
            </div>

            {!preview && (
              <button className="btn btn-primary" type="submit" disabled={!valid}>
                预览用户画像
              </button>
            )}
          </form>

          {preview && valid && (
            <div style={{ marginTop: 16 }}>
              <UserProfile tgId={numId} title="即将加黑用户" />
              <div style={{ display: 'flex', gap: 8, marginTop: 12 }}>
                <button className="btn btn-danger" onClick={handleConfirm} disabled={loading}>
                  {loading ? '提交中...' : '确认加黑'}
                </button>
                <button className="btn btn-outline" onClick={() => setPreview(false)}>取消</button>
              </div>
            </div>
          )}
        </div>

        <div style={{ flex: 1, minWidth: 300 }}>
          <h3 style={{ fontSize: 14, fontWeight: 600, marginBottom: 12 }}>本次会话操作记录</h3>
          {records.length === 0 ? (
            <div style={{ color: 'var(--text-secondary)', fontSize: 13 }}>暂无记录</div>
          ) : (
            <table className="data-table">
              <thead>
                <tr><th>TG ID</th><th>结果</th><th>时间</th></tr>
              </thead>
              <tbody>
                {records.map((r, i) => (
                  <tr key={i} style={{ cursor: 'default' }}>
                    <td>{r.tg_id}</td>
                    <td>{r.added ? <span style={{ color: 'var(--green)' }}>已加黑</span> : <span style={{ color: 'var(--text-secondary)' }}>已存在</span>}</td>
                    <td style={{ fontSize: 12 }}>{r.time}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  )
}
