interface Props {
  status: string
}

const LABELS: Record<string, string> = {
  pending: '待处理',
  verified: '已核实',
  processed: '已处理',
  confirmed: '已确认',
  rejected: '已驳回',
}

export default function StatusBadge({ status }: Props) {
  const label = LABELS[status] || status
  return <span className={`badge badge-${status}`}>{label}</span>
}
