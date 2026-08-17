import { request } from './client'
import type {
  AdminStats,
  AdminReportItem,
  AdminFeedbackItem,
  AdminReporterItem,
  AdminTransactionItem,
  AdminRiskTrendDay,
  PendingAlertItem,
} from '../types'

export const fetchStats = () =>
  request<AdminStats>('/v1/admin/stats')

export const fetchReports = (status?: string, limit = 50, offset = 0, reporterTgId?: number) => {
  const params = new URLSearchParams()
  if (status) params.set('status', status)
  params.set('limit', String(limit))
  params.set('offset', String(offset))
  if (reporterTgId !== undefined) params.set('reporter_tg_id', String(reporterTgId))
  return request<{ items: AdminReportItem[] }>(`/v1/admin/reports?${params}`)
}

export const patchReport = (id: number, status: 'verified' | 'rejected') =>
  request<{ ok: boolean; message: string }>(`/v1/admin/reports/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ status }),
  })

export const batchPatchReports = (ids: number[], status: 'verified' | 'rejected') =>
  request<{ ok: boolean; updated: number; message: string }>('/v1/admin/reports/batch', {
    method: 'POST',
    body: JSON.stringify({ ids, status }),
  })

export const fetchFeedback = (status?: string, limit = 100) => {
  const params = new URLSearchParams()
  if (status) params.set('status', status)
  params.set('limit', String(limit))
  return request<{ items: AdminFeedbackItem[] }>(`/v1/admin/feedback?${params}`)
}

export const patchFeedback = (id: number, status: 'processed' | 'rejected') =>
  request<{ ok: boolean; message: string }>(`/v1/admin/feedback/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ status }),
  })

export const addBlacklist = (tg_id: number, reason?: string, source = 'admin') =>
  request<{ ok: boolean; added: boolean }>('/v1/admin/blacklist', {
    method: 'POST',
    body: JSON.stringify({ tg_id, reason: reason || null, source }),
  })

export const fetchReporters = (limit = 100) =>
  request<{ items: AdminReporterItem[] }>(`/v1/admin/reporters?limit=${limit}`)

export const fetchTransactions = (status?: string, limit = 100) => {
  const params = new URLSearchParams()
  if (status) params.set('status', status)
  params.set('limit', String(limit))
  return request<{ items: AdminTransactionItem[] }>(`/v1/admin/transactions?${params}`)
}

export const fetchRiskTrend = (days = 7) =>
  request<{ items: AdminRiskTrendDay[] }>(`/v1/admin/stats/risk_trend?days=${days}`)

export const fetchPendingAlerts = (limit = 50) =>
  request<{ items: PendingAlertItem[] }>(`/v1/alert/pending?limit=${limit}`)
