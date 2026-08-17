const BASE_URL = import.meta.env.VITE_API_BASE_URL || ''

let onUnauthorized: (() => void) | null = null

export function setOnUnauthorized(fn: () => void) {
  onUnauthorized = fn
}

function getKey(): string {
  return sessionStorage.getItem('admin_key') || ''
}

interface ApiError {
  status: number
  message: string
  retryAfter?: number
}

export async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const key = getKey()
  const headers: Record<string, string> = {
    ...((options.headers as Record<string, string>) || {}),
  }
  if (key) headers['Authorization'] = `Bearer ${key}`
  if (options.body && typeof options.body === 'string') {
    headers['Content-Type'] = 'application/json'
  }

  const res = await fetch(`${BASE_URL}${path}`, { ...options, headers })

  if (res.status === 403) {
    onUnauthorized?.()
    const err: ApiError = { status: 403, message: '未授权，请重新登录' }
    throw err
  }

  if (res.status === 429) {
    const ra = res.headers.get('Retry-After')
    const err: ApiError = {
      status: 429,
      message: `操作过于频繁，请 ${ra || '60'} 秒后重试`,
      retryAfter: ra ? parseInt(ra, 10) : 60,
    }
    throw err
  }

  if (!res.ok) {
    let detail = ''
    try {
      const body = await res.json()
      detail = body.detail || body.message || ''
    } catch {}
    const err: ApiError = {
      status: res.status,
      message: detail || `请求失败 (${res.status})`,
    }
    throw err
  }

  return res.json() as Promise<T>
}
