<template>
  <AppToast ref="toastRef" />
  <ActivationPage v-if="!authenticated" @authenticated="handleAuthenticated" />
  <MainPage v-else ref="mainPage" :toastRef="toastRef" />
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import ActivationPage from './components/ActivationPage.vue'
import MainPage from './components/MainPage.vue'
import AppToast from './components/AppToast.vue'
import { checkSession } from './composables/useAuth.js'
import { clearToken } from './composables/useApi.js'
import { ensurePublicKeyRegistered } from './composables/useCrypto.js'

const authenticated = ref(false)
const mainPage = ref(null)
const toastRef = ref(null)

function handleAuthenticated() {
  authenticated.value = true
  ensurePublicKeyRegistered()
}

function handleAuthExpired() {
  clearToken()
  authenticated.value = false
}

onMounted(async () => {
  // The cookie is HttpOnly, so validate it directly instead of keeping a
  // duplicate session token in localStorage.
  clearToken()
  const valid = await checkSession()
  if (valid) {
    authenticated.value = true
    ensurePublicKeyRegistered()
  }
  window.addEventListener('auth:expired', handleAuthExpired)
  setupVersionCheck()
})

onUnmounted(() => {
  window.removeEventListener('auth:expired', handleAuthExpired)
  clearInterval(_versionTimer)
})

// 版本自检：定期对比线上 index.html 的 bundle hash，发现新版本自动刷新。
// 避免用户浏览器长期停留在旧 bundle（旧 bundle 缺压缩/超时兜底等修复，会导致上传慢等"旧 bug"复发）。
let _versionTimer = null
function setupVersionCheck() {
  const scriptEl = document.querySelector('script[src*="/assets/index-"]')
  const currentHash = (scriptEl && scriptEl.src.match(/index-([^.]+)\.js/) || [])[1] || ''
  if (!currentHash) return
  _versionTimer = setInterval(async () => {
    try {
      const resp = await fetch(location.pathname + '?v=' + Date.now(), { credentials: 'omit' })
      const html = await resp.text()
      const m = html.match(/index-([^.]+)\.js/)
      if (m && m[1] !== currentHash) {
        location.reload()
      }
    } catch { /* 网络抖动忽略，下轮再试 */ }
  }, 60000)
}
</script>
