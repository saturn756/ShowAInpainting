// 上传：先在浏览器本地加密（CSE），再优先直传 OSS；OSS 不可用时经逻辑服务器层转发到 GPU 中继。
// 针对跨国用户（如新西兰导师直连上海 OSS 仅 ~18KB/s）：
//   - 上传前在浏览器压缩大图（背景→512，参考→2048），大幅减小体积
//   - 直传带超时，超时自动切到逻辑服务器代理（NZ→逻辑服务器→OSS，通常更快）
import { apiCall } from './useApi.js'
import { encryptFileForUpload } from './useCrypto.js'

// 直传 OSS 的超时（毫秒）：超过则放弃直传，改走代理
const DIRECT_UPLOAD_TIMEOUT_MS = 10000
const PROXY_UPLOAD_TIMEOUT_MS = 10000
const RELAY_UPLOAD_TIMEOUT_MS = 30000

/**
 * 在浏览器端压缩超大图片，返回不超过 maxSize 的 File（JPEG，尽量保质量）。
 * 若原图已 ≤ maxSize 则原样返回（不重复编码，避免无损图被压坏）。
 */
async function resizeImage(file, maxSize) {
  const bitmap = await createImageBitmap(file)
  const w = bitmap.width, h = bitmap.height
  if (w <= maxSize && h <= maxSize) {
    bitmap.close()
    return file
  }
  const scale = Math.min(maxSize / w, maxSize / h)
  const nw = Math.max(1, Math.round(w * scale))
  const nh = Math.max(1, Math.round(h * scale))
  const canvas = document.createElement('canvas')
  canvas.width = nw
  canvas.height = nh
  const ctx = canvas.getContext('2d')
  ctx.drawImage(bitmap, 0, 0, nw, nh)
  bitmap.close()
  const blob = await new Promise((res) => canvas.toBlob(res, 'image/jpeg', 0.92))
  const name = (file.name || 'image').replace(/\.[^.]+$/, '') + '.jpg'
  return new File([blob], name, { type: 'image/jpeg' })
}

/**
 * 尝试浏览器直传 OSS（内容为密文 + crypto 元数据），带超时。
 */
async function tryDirectUpload(cipherFile, ivB64, wkB64, kid) {
  const policy = await apiCall('POST', '/api/oss/upload-policy', {
    filename: cipherFile.name,
    contentType: 'application/octet-stream',
    size: cipherFile.size,
  })

  const form = new FormData()
  for (const [name, value] of Object.entries(policy.fields)) {
    form.append(name, value)
  }
  form.append('x-oss-meta-crypto-iv', ivB64)
  form.append('x-oss-meta-crypto-wk', wkB64)
  if (kid) form.append('x-oss-meta-crypto-kid', kid)
  form.append('file', cipherFile, cipherFile.name)

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), DIRECT_UPLOAD_TIMEOUT_MS)
  let resp
  try {
    resp = await fetch(policy.url, {
      method: 'POST',
      body: form,
      credentials: 'omit',
      mode: 'cors',
      signal: controller.signal,
    })
  } finally {
    clearTimeout(timer)
  }

  if (resp.status !== 204 && resp.status !== 200) {
    const text = await resp.text().catch(() => '')
    throw new Error(`OSS 上传失败 (HTTP ${resp.status}): ${text.slice(0, 200)}`)
  }

  return policy.key
}

/**
 * 服务端代理上传（绕过 CORS/跨国链路慢）；内容为密文，逻辑服务器只存储不透传私钥
 */
async function proxyUpload(cipherFile, ivB64, wkB64, kid) {
  const form = new FormData()
  form.append('file', cipherFile, cipherFile.name)
  form.append('crypto_iv', ivB64)
  form.append('crypto_wk', wkB64)
  if (kid) form.append('crypto_kid', kid)

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), PROXY_UPLOAD_TIMEOUT_MS)
  let resp
  try {
    resp = await fetch('/api/oss/proxy-upload', {
      method: 'POST',
      body: form,
      credentials: 'include',
      signal: controller.signal,
    })
  } finally {
    clearTimeout(timer)
  }

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }))
    throw new Error(err.error || `代理上传失败 (${resp.status})`)
  }

  return resp.json()
}

/**
 * OSS 不可用时的备用上传：浏览器 → 公网逻辑层 → SSH → GPU 服务。
 * 传输内容仍是 CSE 密文，GPU 服务负责落盘并在推理前解密。
 */
async function relayUpload(cipherFile, ivB64, wkB64, kid) {
  const form = new FormData()
  form.append('file', cipherFile, cipherFile.name)
  form.append('crypto_iv', ivB64)
  form.append('crypto_wk', wkB64)
  if (kid) form.append('crypto_kid', kid)

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), RELAY_UPLOAD_TIMEOUT_MS)
  let resp
  try {
    resp = await fetch('/api/relay/upload', {
      method: 'POST',
      body: form,
      credentials: 'include',
      signal: controller.signal,
    })
  } finally {
    clearTimeout(timer)
  }

  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ error: `HTTP ${resp.status}` }))
    throw new Error(err.error || `GPU 备用上传失败 (${resp.status})`)
  }

  return resp.json()
}

/**
 * 完整上传：浏览器本地压缩 + 加密 → 先试直传 OSS（超时/失败则走代理）。
 * maxSize 传 512/1024/2048 会对大图做浏览器端压缩；不传则原图上传。
 * 返回 { key }；预览用浏览器本地明文，不展示 OSS 密文。
 */
export async function uploadImage(file, maxSize = null) {
  const target = maxSize ? await resizeImage(file, maxSize) : file
  const { cipherFile, ivB64, wkB64, kid } = await encryptFileForUpload(target)
  try {
    const key = await tryDirectUpload(cipherFile, ivB64, wkB64, kid)
    return { key, url: '' }
  } catch (e) {
    // OSS 直传失败后先尝试逻辑服务器 OSS 代理；OSS 整体不可用时再走 GPU 中继
    try {
      return await proxyUpload(cipherFile, ivB64, wkB64, kid)
    } catch (pe) {
      try {
        return await relayUpload(cipherFile, ivB64, wkB64, kid)
      } catch (re) {
        throw new Error(`上传失败: ${re.message}`)
      }
    }
  }
}

/**
 * Canvas Blob → 加密上传（蒙版已是 512×512，不再压缩，避免 PNG 蒙版被压坏）
 */
export async function uploadCanvasToOss(canvas) {
  const blob = await new Promise((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('Canvas 导出失败'))), 'image/png')
  })
  const file = new File([blob], 'mask.png', { type: 'image/png' })
  return uploadImage(file, 512)  // 512 已是上限，resize 会原样返回
}

/**
 * 创建空白蒙版上传
 */
export async function uploadEmptyMask() {
  const canvas = document.createElement('canvas')
  canvas.width = 512
  canvas.height = 512
  const ctx = canvas.getContext('2d')
  ctx.fillStyle = '#000000'
  ctx.fillRect(0, 0, 512, 512)
  return uploadCanvasToOss(canvas)
}
