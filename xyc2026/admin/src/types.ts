export interface AdminStats {
  queries_today: number
  total_users: number
  blacklist_count: number
  reports_pending: number
  feedback_pending: number
  transactions_pending: number
  bound_wallets_count: number
  users_with_flow_visible: number
  flow_snapshots_count: number
}

export interface AdminReportItem {
  id: number
  reporter_tg_id: number
  target_tg_id: number
  reason: string
  category: string | null
  evidence_data: Record<string, unknown> | null
  status: string
  created_at: string | null
}

export interface AdminFeedbackItem {
  id: number
  reporter_tg_id: number
  target_tg_id: number
  reason: string
  status: string
  created_at: string | null
}

export interface AdminReporterItem {
  reporter_tg_id: number
  report_count: number
  verified_count: number
  last_at: string | null
}

export interface AdminTransactionItem {
  id: number
  initiator_tg_id: number
  counterparty_tg_id: number
  amount: string
  currency: string
  status: string
  created_at: string | null
  confirmed_at: string | null
}

export interface AdminRiskTrendDay {
  date: string
  low: number
  medium: number
  high: number
  extreme: number
}

export interface PendingAlertItem {
  id: number
  subscriber_tg_id: number
  target_tg_id: number
  event_type: string
  subscriber_lang: string | null
  created_at: string | null
}

export interface CheckResult {
  risk: string
  score: number
  tips: string[]
  blacklist: boolean
  tg_id: number | null
  username: string | null
  report_count: number
  report_pending: number
  report_verified: number
  is_premium: boolean
  tags: string[]
  tx_success_count: number
  tx_total_count: number
  tx_success_rate: number
  verdict: string
}

export type RiskLevel = 'low' | 'medium' | 'high' | 'extreme'
export type ReportStatus = 'pending' | 'verified' | 'rejected'
export type FeedbackStatus = 'pending' | 'processed' | 'rejected'
