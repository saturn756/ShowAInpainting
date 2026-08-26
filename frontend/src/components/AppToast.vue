<template>
  <Teleport to="body">
    <div v-if="visible" :class="['app-toast', type]" @click="visible = false">
      {{ message }}
    </div>
  </Teleport>
</template>

<script setup>
import { ref } from 'vue'

const visible = ref(false)
const message = ref('')
const type = ref('info') // 'info' | 'error' | 'success'
let timer = null

function show(msg, t = 'info') {
  message.value = msg
  type.value = t
  visible.value = true
  clearTimeout(timer)
  timer = setTimeout(() => { visible.value = false }, 4000)
}

defineExpose({ show })
</script>

<style scoped>
.app-toast {
  position: fixed;
  top: 24px;
  right: 24px;
  z-index: 9999;
  padding: 12px 24px;
  border-radius: 6px;
  color: #fff;
  font-size: 14px;
  font-weight: 600;
  cursor: pointer;
  max-width: 420px;
  box-shadow: 0 4px 16px rgba(0,0,0,.15);
}
.app-toast.info    { background: #1e40af; }
.app-toast.error   { background: #dc2626; }
.app-toast.success { background: #16a34a; }
</style>
