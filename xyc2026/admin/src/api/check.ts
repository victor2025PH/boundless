import { request } from './client'
import type { CheckResult } from '../types'

const cache = new Map<number, { data: CheckResult; ts: number }>()
const CACHE_TTL = 5 * 60 * 1000

export async function checkUser(tgId: number): Promise<CheckResult> {
  const cached = cache.get(tgId)
  if (cached && Date.now() - cached.ts < CACHE_TTL) return cached.data
  const data = await request<CheckResult>(`/v1/check?user_id=${tgId}`)
  cache.set(tgId, { data, ts: Date.now() })
  return data
}

export function invalidateCheckCache(tgId: number) {
  cache.delete(tgId)
}
