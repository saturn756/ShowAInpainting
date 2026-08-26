// 激活码认证
import { apiCall } from './useApi.js'

/**
 * 提交激活码
 * @param {string} code 5位激活码
 * @returns {Promise<{success: boolean, error?: string}>}
 */
export async function activate(code) {
  try {
    const data = await fetch('/api/auth/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: code.trim() }),
      credentials: 'include',
    }).then(r => {
      if (!r.ok) return r.json().then(e => { throw new Error(e.error || '激活失败') })
      return r.json()
    })
    if (data.ok) {
      return { success: true }
    }
    return { success: false, error: '响应格式无效' }
  } catch (e) {
    return { success: false, error: e.message }
  }
}

/**
 * 检查是否已登录（session cookie 是否有效）。
 * 必须用受保护的接口验证 —— /api/health 不需要鉴权，
 * 用它验证会导致 cookie 过期后仍误判为已登录。
 */
export async function checkSession() {
  try {
    await apiCall('GET', '/api/tasks')
    return true
  } catch {
    return false
  }
}
