// 客户端加密 (CSE)：上传内容在浏览器本地加密成密文后才进 OSS。
// 信封格式（与 GPU 服务 Python 端一致）：
//   - AES-256-GCM 加密内容（密文含 16 字节 tag）
//   - 数据密钥 (DK) 用 RSA-OAEP(SHA-256) 封装 → WK，随对象元数据存储
// 上传用「站点公钥」（GPU 私钥解密）；生成结果用「用户公钥」（浏览器私钥解密）。

import { apiCall } from './useApi.js'

// 站点主密钥会每日轮换（私钥在 GPU 服务器，历史留存），
// 前端应通过 /api/crypto/public-key 动态获取当前公钥 + kid；
// 内嵌一份作为兜底（首次/获取失败时用，GPU 会保留历史私钥保证可解）。
const FALLBACK_SITE_KEY = {
  kid: 'f5430159aee0',
  public_key_pem: `-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAqYueMhbtHUZQ/nvOkjMG
9XnAuUVaORj1H3tD093dcBREhnYqoHDqHJWwleO/+DAhfNWhFpa6tgLKBdIg0CZ0
SXj6PEMThhQUyyOv5cpF7ILFONJMMlOEIt0T51F0ZAfs5VIUaxeHfChb3D38nqhq
78XZgbiVGsCwJmvDpKTXwdd33D8KieYJ2n7R9boylDA1YOBfZj4r5fspkF/rp4hh
cgjNnpBP8lPmDFo/FmR97lNZ4BXtgTQAeqJIU4tMdMNFXwWqtOprEHmfHsVBbLgE
B9J1Eyb4JSMjD7i/kbKatz1ytwqo+GInJK8n9Z2xSBTuo9fvBuRHbX25urJOSrMx
KQIDAQAB
-----END PUBLIC KEY-----`,
}

const USER_PRIV_KEY = 'anomaly_gen_user_priv'
const USER_PUB_KEY = 'anomaly_gen_user_pub'

// ---------- base64 / PEM 工具 ----------

function b64Encode(u8) {
  let s = ''
  const CHUNK = 0x8000
  for (let i = 0; i < u8.length; i += CHUNK) {
    s += String.fromCharCode.apply(null, u8.subarray(i, i + CHUNK))
  }
  return btoa(s)
}

function b64Decode(b64) {
  const bin = atob(b64)
  const u8 = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i)
  return u8
}

function pemToArrayBuffer(pem) {
  const b64 = pem.replace(/-----[^-]+-----/g, '').replace(/\s+/g, '')
  return b64Decode(b64).buffer
}

function bufToPem(buf, type) {
  const b64 = b64Encode(new Uint8Array(buf))
  const lines = b64.match(/.{1,64}/g) || []
  return `-----BEGIN ${type} KEY-----\n${lines.join('\n')}\n-----END ${type} KEY-----`
}

// ---------- 站点公钥（上传用，每日轮换 → 动态获取） ----------

let _siteKeyPromise = null
function getSitePublicKey() {
  // 返回 { key: CryptoKey, kid: string }，会话内缓存；获取失败用内嵌兜底
  if (!_siteKeyPromise) {
    _siteKeyPromise = (async () => {
      let info = FALLBACK_SITE_KEY
      try {
        const resp = await fetch('/api/crypto/public-key', { credentials: 'include' })
        if (resp.ok) {
          const data = await resp.json()
          if (data && data.public_key_pem && data.kid) info = data
        }
      } catch { /* 走兜底 */ }
      const key = await crypto.subtle.importKey(
        'spki', pemToArrayBuffer(info.public_key_pem),
        { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['encrypt'],
      )
      return { key, kid: info.kid }
    })()
  }
  return _siteKeyPromise
}

// ---------- 用户密钥对（结果解密用） ----------

function loadUserKeypair() {
  const pubPem = localStorage.getItem(USER_PUB_KEY)
  const privPem = localStorage.getItem(USER_PRIV_KEY)
  if (!pubPem || !privPem) return null
  return { pubPem, privPem }
}

async function ensureUserKeypair() {
  const existing = loadUserKeypair()
  if (existing) return existing
  const pair = await crypto.subtle.generateKey(
    { name: 'RSA-OAEP', hash: 'SHA-256', modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]) },
    true, ['encrypt', 'decrypt'],
  )
  const pubBuf = await crypto.subtle.exportKey('spki', pair.publicKey)
  const privBuf = await crypto.subtle.exportKey('pkcs8', pair.privateKey)
  const kp = { pubPem: bufToPem(pubBuf, 'PUBLIC'), privPem: bufToPem(privBuf, 'PRIVATE') }
  localStorage.setItem(USER_PUB_KEY, kp.pubPem)
  localStorage.setItem(USER_PRIV_KEY, kp.privPem)
  return kp
}

async function getUserPrivateKey() {
  const kp = loadUserKeypair()
  if (!kp) return null
  return crypto.subtle.importKey(
    'pkcs8', pemToArrayBuffer(kp.privPem),
    { name: 'RSA-OAEP', hash: 'SHA-256' }, false, ['decrypt'],
  )
}

// 激活后调用：生成（或复用）用户密钥对并注册公钥，用于服务端加密生成结果
export async function ensurePublicKeyRegistered() {
  try {
    const kp = await ensureUserKeypair()
    await apiCall('POST', '/api/keys', { public_key_pem: kp.pubPem })
    return true
  } catch {
    return false
  }
}

// ---------- 加密上传 ----------

export async function encryptFileForUpload(file) {
  const plain = await file.arrayBuffer()
  const dk = await crypto.subtle.generateKey({ name: 'AES-GCM', length: 256 }, true, ['encrypt'])
  const dkRaw = await crypto.subtle.exportKey('raw', dk)
  const iv = crypto.getRandomValues(new Uint8Array(12))
  const cipher = await crypto.subtle.encrypt({ name: 'AES-GCM', iv }, dk, plain)
  const { key: sitePub, kid } = await getSitePublicKey()
  const wk = await crypto.subtle.encrypt({ name: 'RSA-OAEP' }, sitePub, dkRaw)
  // 文件名保持原名（服务器按后缀校验白名单），内容为密文
  const cipherFile = new File([cipher], file.name, { type: 'application/octet-stream' })
  return { cipherFile, ivB64: b64Encode(iv), wkB64: b64Encode(new Uint8Array(wk)), kid }
}

// ---------- 解密结果 ----------

export async function decryptPayload(cipherBytes, ivB64, wkB64) {
  const priv = await getUserPrivateKey()
  if (!priv) throw new Error('用户密钥缺失，请重新激活')
  const dk = await crypto.subtle.decrypt({ name: 'RSA-OAEP' }, priv, b64Decode(wkB64))
  const dkKey = await crypto.subtle.importKey('raw', dk, { name: 'AES-GCM' }, false, ['decrypt'])
  return crypto.subtle.decrypt({ name: 'AES-GCM', iv: b64Decode(ivB64) }, dkKey, cipherBytes)
}

// 抓取密文 → 解密 → 返回可显示/下载的 objectURL
export async function decryptResultImage(url, ivB64, wkB64) {
  // OSS 签名 URL 不需要会话；逻辑服务器中继结果是同源受保护接口，需要带 cookie。
  const sameOrigin = url.startsWith('/') ||
    new URL(url, window.location.href).origin === window.location.origin
  const resp = await fetch(url, { credentials: sameOrigin ? 'include' : 'omit' })
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  const cipher = await resp.arrayBuffer()
  const plain = await decryptPayload(new Uint8Array(cipher), ivB64, wkB64)
  return URL.createObjectURL(new Blob([plain], { type: 'image/jpeg' }))
}
