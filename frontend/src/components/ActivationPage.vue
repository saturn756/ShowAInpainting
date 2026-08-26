<template>
  <div class="activation-page">
    <main>
      <h1>异常图片生成模型</h1>
      <p class="subtitle">请输入 5 位激活码进入</p>
      <form @submit.prevent="handleSubmit">
        <label for="code">激活码</label>
        <input
          id="code"
          v-model="code"
          type="password"
          minlength="5"
          maxlength="5"
          autocomplete="one-time-code"
          placeholder="请输入 5 位激活码"
          autofocus
          required
        />
        <button type="submit" :disabled="loading">
          {{ loading ? '验证中...' : '进入' }}
        </button>
      </form>
      <div v-if="error" class="error">{{ error }}</div>
    </main>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { activate } from '../composables/useAuth.js'

const emit = defineEmits(['authenticated'])

const code = ref('')
const loading = ref(false)
const error = ref('')

async function handleSubmit() {
  if (code.value.length !== 5) return
  loading.value = true
  error.value = ''
  const result = await activate(code.value)
  loading.value = false
  if (result.success) {
    emit('authenticated')
  } else {
    error.value = result.error
  }
}
</script>

<style scoped>
.activation-page {
  display: flex;
  min-height: 100vh;
  align-items: center;
  justify-content: center;
  padding: 24px;
}
main {
  width: min(420px, 100%);
  padding: 30px;
  border: 1px solid #d9dde5;
  border-radius: 8px;
  background: #fff;
  box-shadow: 0 12px 32px rgba(31,41,55,.08);
}
h1 { margin-bottom: 8px; font-size: 26px; }
.subtitle { margin-bottom: 22px; color: #6b7280; font-size: 15px; }
label { display: block; margin-bottom: 8px; font-size: 14px; font-weight: 600; }
input {
  width: 100%; height: 46px; padding: 0 13px;
  border: 1px solid #cfd5df; border-radius: 6px; font-size: 16px; outline: none;
  box-sizing: border-box;
}
input:focus { border-color: #d97706; box-shadow: 0 0 0 3px rgba(217,119,6,.14); }
button {
  width: 100%; height: 46px; margin-top: 16px;
  border: 0; border-radius: 6px; background: #d97706; color: #fff;
  font-size: 16px; font-weight: 700; cursor: pointer;
}
button:hover:not(:disabled) { background: #b65f05; }
button:disabled { opacity: .6; cursor: not-allowed; }
.error { margin-top: 14px; color: #b42318; font-size: 14px; text-align: center; }
</style>
