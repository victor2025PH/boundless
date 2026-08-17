interface Props {
  category: string | null
}

const CATS: Record<string, { icon: string; label: string }> = {
  scam: { icon: '🚨', label: '诈骗' },
  run_order: { icon: '📦', label: '跑单' },
  lost_contact: { icon: '📵', label: '失联' },
  other: { icon: '📝', label: '其他' },
}

export default function CategoryBadge({ category }: Props) {
  const c = category ? CATS[category] : null
  if (!c) return <span className="badge cat-other">-</span>
  return (
    <span className={`badge cat-${category}`}>
      {c.icon} {c.label}
    </span>
  )
}
