import { useState, useEffect, useRef, useCallback } from 'react'

interface Props {
  label: string
  onConfirm: () => Promise<void>
  onComplete: () => void
  seconds?: number
}

export default function UndoAction({ label, onConfirm, onComplete, seconds = 5 }: Props) {
  const [count, setCount] = useState(seconds)
  const [executing, setExecuting] = useState(false)
  const [cancelled, setCancelled] = useState(false)
  const timer = useRef<ReturnType<typeof setInterval>>(undefined)

  const execute = useCallback(async () => {
    setExecuting(true)
    try {
      await onConfirm()
      onComplete()
    } catch (err: unknown) {
      const msg = err && typeof err === 'object' && 'message' in err
        ? (err as { message: string }).message
        : '操作失败'
      alert(msg)
    } finally {
      setExecuting(false)
    }
  }, [onConfirm, onComplete])

  useEffect(() => {
    timer.current = setInterval(() => {
      setCount((c) => {
        if (c <= 1) {
          clearInterval(timer.current)
          return 0
        }
        return c - 1
      })
    }, 1000)
    return () => clearInterval(timer.current)
  }, [])

  useEffect(() => {
    if (count === 0 && !cancelled) execute()
  }, [count, cancelled, execute])

  const handleCancel = () => {
    clearInterval(timer.current)
    setCancelled(true)
  }

  if (cancelled) return <div style={{ fontSize: 13, color: 'var(--text-secondary)' }}>已撤回</div>
  if (executing) return <div style={{ fontSize: 13, color: 'var(--blue)' }}>执行中...</div>

  return (
    <div className="undo-bar">
      <span>{label}</span>
      <span className="countdown">{count}s</span>
      <button className="btn btn-sm btn-outline" onClick={handleCancel}>撤回</button>
    </div>
  )
}
