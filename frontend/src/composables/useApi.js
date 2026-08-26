// API 调用基础层：HttpOnly session cookie + fetch 封装
// 清理早期版本曾写入 localStorage 的 token，避免遗留敏感凭证。
const LEGACY_TOKEN_KEY = 'anomaly_gen_token'

export function clearToken() {
  localStorage.removeItem(LEGACY_TOKEN_KEY)
}

export async function apiCall(method, path, body = null) {
  const headers = {}

  const opts = { method, headers, credentials: 'include' }
  if (body) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }

  const resp = await fetch(path, opts)
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }))
    if (resp.status === 401) {
      // session 失效：通知 App 回到激活页
      window.dispatchEvent(new CustomEvent('auth:expired'))
    }
    throw new Error(err.error || err.detail || `HTTP ${resp.status}`)
  }
  return resp.json()
}
