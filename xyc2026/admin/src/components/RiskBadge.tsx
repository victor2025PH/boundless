interface Props {
  level: string
  score?: number
}

const LABELS: Record<string, string> = {
  low: '低风险',
  medium: '中风险',
  high: '高风险',
  extreme: '极高风险',
}

const ICONS: Record<string, string> = {
  low: '🟢',
  medium: '🟡',
  high: '🟠',
  extreme: '🔴',
}

export default function RiskBadge({ level, score }: Props) {
  return (
    <span className={`badge badge-${level}`}>
      {ICONS[level] || '⚪'} {LABELS[level] || level}
      {score !== undefined && ` ${score}`}
    </span>
  )
}
