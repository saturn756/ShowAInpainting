<template>
  <div class="app-page">
    <div class="app-header">
      <h1>异常图片生成模型</h1>
      <p>模型自动从参考异常图片中提取异常特征，并将其生成到背景图片的指定区域</p>
    </div>

    <!-- 队列状态 -->
    <div class="queue-bar">
      <span v-if="queueTotal === 0">当前队列为空，上传图片后即可提交任务</span>
      <span v-else>当前队列: {{ queueTotal }} 个任务（{{ queueGenerating }} 个生成中，{{ queueWaiting }} 个等待中）</span>
    </div>

    <div class="section-block">
      <h2>使用方法</h2>
      <div class="usage-guide">
        <p><b>第一步：</b>上传背景图片。</p>
        <p><b>第二步：</b>用白色画笔在背景上绘制缺陷区域（可调画笔大小）。</p>
        <p><b>第三步：</b>上传参考异常图片。</p>
        <p><b>第四步（可选）：</b>在参考图上拖拽框选正方形缺陷区域。</p>
        <p><b>第五步：</b>点击"生成"，结果出现在右侧可切换查看。</p>
      </div>
    </div>

    <div class="section-block">
      <h2>图片输入与区域选择</h2>
      <div class="three-col">
        <!-- 背景 + 蒙版 -->
        <div class="col">
          <h3>背景图片与缺陷区域</h3>
          <div id="bg-editor" class="editor-area">
            <canvas ref="maskCanvas" width="512" height="512"
              @mousedown="maskStart" @mousemove="maskMove" @mouseup="maskEnd" @mouseleave="maskEnd" />
            <div v-if="!bgImage" class="editor-placeholder">请上传背景图片并绘制缺陷区域</div>
          </div>
          <div class="tool-row">
            <label>画笔</label>
            <input type="range" min="5" max="120" v-model.number="brushSize" />
            <button @click="clearMask">清除蒙版</button>
          </div>
          <input type="file" accept="image/*" @change="handleBgUpload" :disabled="uploadingBg"
            style="width:100%;min-height:48px;margin-top:8px;border:1px solid #d1d5db;border-radius:6px;background:#fff;font-size:14px;cursor:pointer;padding:12px" />
          <div v-if="bgUploadMsg" :class="['upload-msg', { done: bgUploadMsg.includes('✓'), fail: bgUploadMsg.includes('失败') }]">
            {{ bgUploadMsg }}
          </div>
          <div v-if="bgError" class="error">{{ bgError }}</div>
        </div>

        <!-- 参考图 + ROI 框选 -->
        <div class="col">
          <h3>初始参考缺陷图片</h3>
          <div class="image-box" ref="refBox"
            @mousedown="roiStart" @mousemove="roiMove" @mouseup="roiEnd" @mouseleave="roiEnd">
            <canvas ref="refCanvas" />
            <img v-if="refPreviewUrl" :src="refPreviewUrl" style="display:none" ref="refImgLoader"
              @load="drawRefToCanvas" />
            <div v-if="!refPreviewUrl" class="placeholder">请上传参考图片</div>
            <div v-if="refPreviewUrl && roiShowBox" class="roi-box" :style="roiBoxStyle"></div>
          </div>
          <input type="file" accept="image/*" @change="handleRefUpload" :disabled="uploadingRef"
            style="width:100%;min-height:48px;margin-top:8px;border:1px solid #d1d5db;border-radius:6px;background:#fff;font-size:14px;cursor:pointer;padding:12px" />
          <div v-if="refUploadMsg" :class="['upload-msg', { done: refUploadMsg.includes('✓'), fail: refUploadMsg.includes('失败') }]">
            {{ refUploadMsg }}
          </div>
          <div v-if="refError" class="error">{{ refError }}</div>
        </div>

        <!-- ROI 预览 -->
        <div class="col">
          <h3>最终参考缺陷图片</h3>
          <div class="image-box">
            <canvas ref="roiCanvas" />
            <div v-if="!refPreviewUrl" class="placeholder">请先上传参考图片</div>
          </div>
          <button class="action-btn" @click="resetRoi" :disabled="!roiActive">重置参考区域</button>
          <div class="roi-hint">{{ refStatus }}</div>
        </div>
      </div>
    </div>

    <!-- 生成 -->
    <div class="section-block">
      <h2>生成结果</h2>
      <div class="gen-row">
        <div class="gen-controls">
          <div class="param">
            <label>采样步数</label>
            <input type="range" min="1" max="100" v-model.number="ddimSteps" />
            <span class="val">{{ ddimSteps }}</span>
          </div>
          <div class="param">
            <label>引导强度</label>
            <input type="range" min="-10" max="20" step="0.1" v-model.number="guidanceScale" />
            <span class="val">{{ guidanceScale.toFixed(1) }}</span>
          </div>
          <div class="param">
            <label>随机种子</label>
            <input type="number" v-model.number="seed" min="0" class="seed-input" />
          </div>
          <button class="generate-btn" :disabled="!canGenerate || submitting" @click="doGenerate">
            {{ submitting ? '提交中...' : userTaskCount >= maxTasksPerUser ? `已满 (${userTaskCount}/${maxTasksPerUser})` : '生成' }}
          </button>
          <div class="gen-status">{{ statusText }}</div>
        </div>

        <!-- 结果区：大图 + 缩略图切换 -->
        <div class="gen-result">
          <template v-if="results.length > 0">
            <div class="result-viewer">
              <div class="result-img-wrap">
                <img :src="results[activeResult].url" class="result-main" />
                <button class="download-btn" @click="downloadActiveResult" title="下载当前结果">下载</button>
              </div>
              <div class="result-label">{{ results[activeResult].label }}</div>
            </div>
            <div class="result-thumbs">
              <div v-for="(item, i) in results" :key="i"
                :class="['thumb', { active: i === activeResult }]"
                @click="activeResult = i">
                <img :src="item.url" />
                <span>{{ item.label }}</span>
              </div>
            </div>
          </template>
          <div v-else class="no-result">生成结果将显示在此处</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted, onUnmounted } from 'vue'
import { uploadImage, uploadCanvasToOss, uploadEmptyMask } from '../composables/useOssUpload.js'
import { decryptResultImage } from '../composables/useCrypto.js'
import { submitTask, pollTask, listTasks } from '../composables/useTaskQueue.js'

const props = defineProps({ toastRef: { type: Object, default: null } })

// ---- 背景 / 蒙版 ----
const bgImage = ref(null); const bgKey = ref(null)
const bgPreviewUrl = ref('')
const uploadingBg = ref(false); const bgError = ref('')
const bgUploadMsg = ref('')
const maskCanvas = ref(null); let maskCtx = null
let maskOffCtx = null; let isMaskDrawing = false
const brushSize = ref(30)

// ---- 参考图 ----
const refPreviewUrl = ref(''); const refKey = ref(null)
const uploadingRef = ref(false); const refError = ref('')
const refUploadMsg = ref('')
const refStatus = ref('请先上传或选择参考图片')
const refCanvas = ref(null); const refBox = ref(null)
const refImgLoader = ref(null)
let refCtx = null; let refImgEl = null

// ROI
const roiShowBox = ref(false); const roiBoxStyle = ref({})
const roiRect = ref(null); const roiActive = ref(false)
const roiCanvas = ref(null)

// ---- 内置参考 ----
// ---- 生成 ----
const ddimSteps = ref(50); const guidanceScale = ref(7.5); const seed = ref(42)
const generating = ref(false); const submitting = ref(false)
const statusText = ref('请先上传背景图片')
const results = ref([]); const activeResult = ref(0)
const userTaskCount = ref(0)
const maxTasksPerUser = ref(5)
const taskSeqNo = ref(1)  // 前端自增序号，不依赖后端
const canGenerate = computed(() => !!(bgKey.value && refKey.value) && userTaskCount.value < maxTasksPerUser.value)

// ---- 队列 ----
const queueTotal = ref(0); const queueWaiting = ref(0); const queueGenerating = ref(0)
let queueTimer = null

// ===================== 蒙版 =====================
let maskOffCanvas = null  // 离屏 canvas 引用
function initMaskCtx() {
  if (!maskCanvas.value) return
  maskCtx = maskCanvas.value.getContext('2d')
  maskCtx.fillStyle = '#202225'; maskCtx.fillRect(0, 0, 512, 512)
  // 离屏蒙版（纯黑白，用于上传）
  maskOffCanvas = document.createElement('canvas')
  maskOffCanvas.width = 512; maskOffCanvas.height = 512
  maskOffCtx = maskOffCanvas.getContext('2d')
  maskOffCtx.fillStyle = '#000'; maskOffCtx.fillRect(0, 0, 512, 512)
}
function drawBgToMask() {
  if (!bgImage.value || !maskCtx || !maskCanvas.value) return
  const cw = maskCanvas.value.width, ch = maskCanvas.value.height
  const dw = _iw * _s, dh = _ih * _s
  maskCtx.clearRect(0, 0, cw, ch)
  maskCtx.fillStyle = '#202225'; maskCtx.fillRect(0, 0, cw, ch)
  maskCtx.drawImage(bgImage.value, _ox, _oy, dw, dh)
}
function maskCPos(e) {
  const c = maskCanvas.value
  const r = c.getBoundingClientRect()
  return { x: (e.clientX - r.left) * c.width / r.width, y: (e.clientY - r.top) * c.height / r.height }
}
// display 坐标 → 512 蒙版填充空间映射（与 GPU letterbox 布局一致：
// 背景等比放置于 512 画布，蒙版随背景走，黑边区域为 0）
function toMask(dx, dy) {
  if (_s > 0 && _padS > 0) {
    const m = _padS / _s
    return { x: _padX + (dx - _ox) * m, y: _padY + (dy - _oy) * m }
  }
  return { x: dx, y: dy }
}
// 蒙版画布相对显示画布的缩放（画笔半径同步换算，保持手感一致）
function maskScale() {
  return _s > 0 ? _padS / _s : 1
}
function maskStart(e) { if (!bgImage.value) return; isMaskDrawing = true; const p = maskCPos(e); const mp = toMask(p.x, p.y); const r = brushSize.value / 2 * maskScale()
  maskCtx.beginPath(); maskCtx.arc(p.x, p.y, brushSize.value / 2, 0, Math.PI * 2); maskCtx.fillStyle = '#FFF'; maskCtx.fill()
  maskOffCtx.beginPath(); maskOffCtx.arc(mp.x, mp.y, r, 0, Math.PI * 2); maskOffCtx.fillStyle = '#FFF'; maskOffCtx.fill() }
function maskMove(e) { if (!isMaskDrawing) return; const p = maskCPos(e); const mp = toMask(p.x, p.y); const r = brushSize.value / 2 * maskScale()
  maskCtx.beginPath(); maskCtx.arc(p.x, p.y, brushSize.value / 2, 0, Math.PI * 2); maskCtx.fillStyle = '#FFF'; maskCtx.fill()
  maskOffCtx.beginPath(); maskOffCtx.arc(mp.x, mp.y, r, 0, Math.PI * 2); maskOffCtx.fillStyle = '#FFF'; maskOffCtx.fill() }
function maskEnd() { isMaskDrawing = false }
function clearMask() {
  maskOffCtx.clearRect(0, 0, 512, 512); maskOffCtx.fillStyle = '#000'; maskOffCtx.fillRect(0, 0, 512, 512)
  // 重新绘制背景到 canvas（contain 不拉伸）
  const cw = maskCanvas.value?.width || 512, ch = maskCanvas.value?.height || 512
  drawImageContain(maskCtx, bgImage.value, cw, ch, '#202225')
}

// ===================== 上传 =====================
// 根据上传状态刷新底部提示（避免背景图已上传仍提示"请先上传背景图片"）
function refreshUploadHint() {
  if (!bgKey.value && !refKey.value) statusText.value = '请先上传背景图片'
  else if (!bgKey.value) statusText.value = '参考图片已就绪，请上传背景图片'
  else if (!refKey.value) statusText.value = '背景图片已就绪，请上传参考图片'
  else statusText.value = '图片已就绪，可以点击生成'
}

async function handleBgUpload(e) {
  const file = e.target.files[0]; if (!file) return
  bgError.value = ''
  // 立即本地预览
  const localUrl = URL.createObjectURL(file)
  const localImg = await loadImg(localUrl)
  bgImage.value = localImg
  bgPreviewUrl.value = localUrl
  bgUploadMsg.value = '上传中...'
  uploadingBg.value = true
  try {
    // 背景压缩到 512（GPU 推理只用到 512×512，体积大幅缩小，跨国上传更快）
    const r = await uploadImage(file, 512); bgKey.value = r.key
    bgPreviewUrl.value = localUrl  // 预览始终用本地明文（OSS 里存的是密文）
    bgUploadMsg.value = '上传完成 ✓'
    refreshUploadHint()
  } catch (err) {
    bgError.value = err.message
    bgUploadMsg.value = '上传失败'
    bgImage.value = null; bgPreviewUrl.value = ''
    refreshUploadHint()
  }
  uploadingBg.value = false
}

// watch bgImage 变化后画到 canvas
import { watch } from 'vue'
// 坐标映射参数：显示画布 contain 布局 + 512 蒙版 letterbox 布局（与 GPU 一致）
let _ox = 0, _oy = 0, _s = 1, _iw = 512, _ih = 512
let _padX = 0, _padY = 0, _padS = 1
watch(bgImage, (img) => {
  if (!img) return
  const c = document.querySelector('#bg-editor canvas')
  if (!c) return
  // 内部尺寸跟随显示框，避免 CSS 把 512×512 压扁/拉长（组件本身不是方形）
  const box = c.parentElement
  const cw = box.clientWidth || 512, ch = box.clientHeight || 512
  if (c.width !== cw) c.width = cw
  if (c.height !== ch) c.height = ch
  maskCtx = c.getContext('2d')
  _iw = img.naturalWidth || img.width; _ih = img.naturalHeight || img.height
  _s = Math.min(cw / _iw, ch / _ih)
  const dw = _iw * _s, dh = _ih * _s
  _ox = (cw - dw) / 2; _oy = (ch - dh) / 2
  maskCtx.clearRect(0, 0, cw, ch)
  maskCtx.fillStyle = '#202225'; maskCtx.fillRect(0, 0, cw, ch)
  maskCtx.drawImage(img, _ox, _oy, dw, dh)
  // 蒙版画布（512×512）：背景按 letterbox 布局放置，与 GPU 端一致
  _padS = Math.min(512 / _iw, 512 / _ih)
  _padX = (512 - _iw * _padS) / 2
  _padY = (512 - _ih * _padS) / 2
  // 创建离屏蒙版画布（纯黑底，黑边区域为 0 不参与修复）
  maskOffCanvas = document.createElement('canvas')
  maskOffCanvas.width = 512; maskOffCanvas.height = 512
  maskOffCtx = maskOffCanvas.getContext('2d')
  maskOffCtx.fillStyle = '#000'; maskOffCtx.fillRect(0, 0, 512, 512)
})
async function handleRefUpload(e) {
  const file = e.target.files[0]; if (!file) return
  refError.value = ''
  // 立即本地预览
  const localUrl = URL.createObjectURL(file)
  refPreviewUrl.value = localUrl
  resetRoi()
  refUploadMsg.value = '上传中...'
  uploadingRef.value = true
  try {
    // 参考图压缩到 2048（兼顾 ROI 框选精度与上传体积）
    const r = await uploadImage(file, 2048); refKey.value = r.key
    refPreviewUrl.value = localUrl  // 预览用本地明文（OSS 里存的是密文）
    refUploadMsg.value = '上传完成 ✓'
    resetRoi()
    refreshUploadHint()
  } catch (err) {
    refError.value = err.message
    refUploadMsg.value = '上传失败'
    refPreviewUrl.value = ''
    refreshUploadHint()
  }
  uploadingRef.value = false
}
// ===================== 参考图 Canvas + ROI =====================
// 以 contain 方式绘制图片（等比缩放、居中、可选底色），避免拉伸变形
function drawImageContain(ctx, img, cw, ch, bgColor, sx = 0, sy = 0, sw = 0, sh = 0) {
  ctx.clearRect(0, 0, cw, ch)
  if (bgColor) { ctx.fillStyle = bgColor; ctx.fillRect(0, 0, cw, ch) }
  const iw = sw > 0 ? sw : img.naturalWidth
  const ih = sh > 0 ? sh : img.naturalHeight
  if (iw <= 0 || ih <= 0) return
  const s = Math.min(cw / iw, ch / ih)
  const dw = iw * s, dh = ih * s
  const ox = (cw - dw) / 2, oy = (ch - dh) / 2
  ctx.drawImage(img, sx, sy, iw, ih, ox, oy, dw, dh)
}

function drawRefToCanvas() {
  if (!refCanvas.value || !refImgLoader.value) { nextTick(() => drawRefToCanvas()); return }
  refImgEl = refImgLoader.value
  const c = refCanvas.value, box = refBox.value
  c.width = box.clientWidth; c.height = box.clientHeight
  refCtx = c.getContext('2d')
  drawImageContain(refCtx, refImgEl, c.width, c.height, '#202225')
  // 画到 ROI 预览（完整图）
  drawRoiFull()
}

function _fitRoiCanvasSize() {
  const c = roiCanvas.value
  if (!c || !c.parentElement) return false
  const box = c.parentElement
  const cw = box.clientWidth || c.width, ch = box.clientHeight || c.height
  if (c.width !== cw) c.width = cw
  if (c.height !== ch) c.height = ch
  return true
}
function drawRoiFull() {
  if (!roiCanvas.value || !refImgEl) return
  if (!_fitRoiCanvasSize()) return
  const c = roiCanvas.value, ctx = c.getContext('2d')
  drawImageContain(ctx, refImgEl, c.width, c.height, '#202225')
}

let roiStartPos = null
function roiStart(e) {
  if (!refPreviewUrl.value || !refCtx) return
  const r = refCanvas.value.getBoundingClientRect()
  roiStartPos = { x: e.clientX - r.left, y: e.clientY - r.top }
  roiShowBox.value = true
}
function roiMove(e) {
  if (!roiShowBox.value || !roiStartPos) return
  const r = refCanvas.value.getBoundingClientRect()
  const cx = e.clientX - r.left, cy = e.clientY - r.top
  // 约束为正方形：取 max(|dx|, |dy|)
  const dx = cx - roiStartPos.x, dy = cy - roiStartPos.y
  const side = Math.max(Math.abs(dx), Math.abs(dy))
  const sx = dx >= 0 ? roiStartPos.x : roiStartPos.x - side
  const sy = dy >= 0 ? roiStartPos.y : roiStartPos.y - side
  roiBoxStyle.value = { left: sx + 'px', top: sy + 'px', width: side + 'px', height: side + 'px' }
}
function roiEnd() {
  roiShowBox.value = false
  if (!roiStartPos || !refCanvas.value) return
  const r = refCanvas.value.getBoundingClientRect()
  const cw = refCanvas.value.width, ch = refCanvas.value.height
  const side = parseFloat(roiBoxStyle.value.width) || 0
  if (side < 10) { roiStartPos = null; return }
  const left = parseFloat(roiBoxStyle.value.left) || 0
  const top = parseFloat(roiBoxStyle.value.top) || 0
  roiStartPos = null
  // contain 映射：display 像素 → 图片像素（参考图是等比居中显示的）
  const iw = refImgEl?.naturalWidth || cw, ih = refImgEl?.naturalHeight || ch
  const s = Math.min(cw / iw, ch / ih)
  const ox = (cw - iw * s) / 2, oy = (ch - ih * s) / 2
  const ix = Math.max(0, Math.min(iw, (left - ox) / s))
  const iy = Math.max(0, Math.min(ih, (top - oy) / s))
  const iside = Math.max(0, Math.min(side / s, iw - ix, ih - iy))
  if (iside < 10) { roiActive.value = false; return }
  roiRect.value = { x: Math.round(ix), y: Math.round(iy), side: Math.round(iside) }
  roiActive.value = true
  refStatus.value = `已框选 ${roiRect.value.side}×${roiRect.value.side} 正方形区域`
  drawRoiPreview()
}
function drawRoiPreview() {
  if (!roiCanvas.value || !roiRect.value || !refImgEl) return
  if (!_fitRoiCanvasSize()) return
  const c = roiCanvas.value, ctx = c.getContext('2d')
  const { x, y, side } = roiRect.value
  drawImageContain(ctx, refImgEl, c.width, c.height, '#202225', x, y, side, side)
}
function resetRoi() {
  roiRect.value = null; roiActive.value = false; roiBoxStyle.value = {}; roiShowBox.value = false
  drawRoiFull()
  refStatus.value = '当前使用完整参考图片'
}

// ===================== 队列 =====================
async function refreshQueue() {
  try {
    const data = await listTasks()
    const tasks = data.tasks || []
    if (Number.isInteger(data.max_tasks_per_user) && data.max_tasks_per_user > 0) {
      maxTasksPerUser.value = data.max_tasks_per_user
    }
    queueTotal.value = tasks.length
    queueWaiting.value = tasks.filter(t => t.status === 'queued').length
    queueGenerating.value = tasks.filter(t => t.status === 'generating').length
    userTaskCount.value = tasks.length
  } catch {}
}
onMounted(() => { refreshQueue(); queueTimer = setInterval(refreshQueue, 5000) })
onUnmounted(() => { clearInterval(queueTimer) })

// ===================== 生成 =====================
const pollingTasks = ref([])
const _lastPollStatus = {}  // task_id → status, 用于判断是否有任务在生成中

async function pushResult(s, taskIdx, taskSeed) {
  let url = s.result_url
  // 结果在 OSS 里是 CSE 密文：先抓取并用浏览器用户私钥解密，再入列表。
  // 注意：不能在 push 之后再改 entry.url —— 那样改的是原始对象，不触发 Vue 响应式，
  // 会导致第一张图一直显示破图（密文 URL），直到下一次重渲染才恢复。
  if (s.crypto_iv && s.crypto_wk) {
    try {
      url = await decryptResultImage(s.result_url, s.crypto_iv, s.crypto_wk)
    } catch (err) {
      props.toastRef?.show(`结果解密失败: ${err.message}`, 'error')
    }
  }
  results.value.push({ url, label: `任务 #${taskIdx} | seed=${taskSeed}`, taskNo: taskIdx, seed: taskSeed })
}

async function doGenerate() {
  if (!canGenerate.value || submitting.value) return
  submitting.value = true
  try {
    let maskKey
    try { const r = await uploadCanvasToOss(maskOffCanvas || maskCanvas.value); maskKey = r.key } catch {}
    if (!maskKey) { const r = await uploadEmptyMask(); maskKey = r.key }

    const taskIdx = taskSeqNo.value
    taskSeqNo.value++

    const data = await submitTask({
      background_key: bgKey.value,
      reference_key: refKey.value,
      mask_key: maskKey,
      roi: roiRect.value ? { x: roiRect.value.x, y: roiRect.value.y, width: roiRect.value.side, height: roiRect.value.side } : null,
      ddim_steps: ddimSteps.value,
      guidance_scale: guidanceScale.value,
      seed: seed.value,
    })
    const queueAhead = data.queue_ahead || 0
    statusText.value = queueAhead > 0
      ? `任务 #${taskIdx} 已加入队列 | 前面还有 ${queueAhead} 个任务`
      : `任务 #${taskIdx} 正在生成中`
    refreshQueue()

    const taskSeed = seed.value
    seed.value++
    pollingTasks.value.push({ task_id: data.task_id, user_task_index: taskIdx, seed: taskSeed })
    pollInBackground(data.task_id, taskIdx, taskSeed)
  } catch (e) {
    statusText.value = '生成失败'; props.toastRef?.show(e.message, 'error')
  }
  submitting.value = false; refreshQueue()
}

async function pollInBackground(taskId, taskIdx, taskSeed) {
  try {
    await pollTask(taskId, async (s) => {
      // 检查是否有正在生成的任务
      const hasGenerating = pollingTasks.value.some(t =>
        t.task_id !== taskId && _lastPollStatus[t.task_id] === 'generating'
      )

      if (s.status === 'done') {
        _lastPollStatus[taskId] = 'done'
        await pushResult(s, taskIdx, taskSeed)
        if (!hasGenerating) statusText.value = `任务 #${taskIdx} 已完成`
        activeResult.value = results.value.length - 1
        props.toastRef?.show(`任务 #${taskIdx} 已完成`, 'success')
      } else if (s.status === 'queued') {
        _lastPollStatus[taskId] = 'queued'
        if (!hasGenerating) {
          const ahead = s.queue_ahead || 0
          statusText.value = `前面还有 ${ahead} 个任务（你的任务 #${taskIdx}）`
        }
      } else if (s.status === 'generating') {
        _lastPollStatus[taskId] = 'generating'
        statusText.value = `任务 #${taskIdx} 正在生成中`
      } else if (s.status === 'failed') {
        _lastPollStatus[taskId] = 'failed'
        if (!hasGenerating) statusText.value = '任务失败'
        props.toastRef?.show('任务失败', 'error')
      }
      refreshQueue()
    })
  } catch {}
  delete _lastPollStatus[taskId]
  pollingTasks.value = pollingTasks.value.filter(t => t.task_id !== taskId)
}

function loadImg(url) { return new Promise((resolve, reject) => { const img = new Image(); img.onload = () => resolve(img); img.onerror = reject; img.src = url }) }

// ===================== 下载结果 =====================
async function downloadActiveResult() {
  const item = results.value[activeResult.value]
  if (!item) return
  // 结果 URL 是 OSS 跨域签名链接，<a download> 会失效，先 fetch 成 blob 再下载
  try {
    const resp = await fetch(item.url, { credentials: 'omit' })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const blob = await resp.blob()
    const a = document.createElement('a')
    const objectUrl = URL.createObjectURL(blob)
    a.href = objectUrl
    a.download = `result_task${item.taskNo}_seed${item.seed}.jpg`
    document.body.appendChild(a)
    a.click()
    a.remove()
    URL.revokeObjectURL(objectUrl)
  } catch (e) {
    // 兜底：新标签页打开，让用户自己保存
    window.open(item.url, '_blank')
  }
}
</script>

<style scoped>
.app-page { max-width: 1280px; margin: 0 auto; padding: 0 28px 56px; }
.app-header { padding: 34px 0 8px; }
.app-header h1 { font-size: 32px; margin-bottom: 8px; }
.app-header p { color: #6b7280; font-size: 15px; }
.section-block { padding: 30px 0 34px; border-bottom: 1px solid #e5e7eb; }
.app-header + .section-block { padding-top: 14px; }
.section-block h2 { font-size: 21px; margin-bottom: 18px; }
.section-desc { color: #6b7280; font-size: 14px; margin: -8px 0 18px; max-width: 900px; line-height: 1.7; }
.usage-guide { max-width: 980px; }
.usage-guide p { margin-bottom: 8px; line-height: 1.7; font-size: 14px; }

/* 队列条 */
.queue-bar { padding: 12px 16px; border-radius: 6px; background: #ecfdf5; border: 1px solid #a7f3d0; color: #065f46; font-weight: 600; font-size: 14px; text-align: center; margin-bottom: 16px; }

.three-col { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 18px; }
.col h3 { font-size: 16px; margin-bottom: 10px; }

/* Canvas */
.editor-area { position: relative; min-height: 512px; background: #202225; border-radius: 6px; border: 1px solid #4b5563; overflow: hidden; }
.editor-area canvas { display: block; cursor: crosshair; width: 100%; height: 512px; }
.editor-placeholder { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: #9ca3af; font-size: 14px; pointer-events: none; }
.tool-row { display: flex; align-items: center; gap: 8px; margin-top: 8px; }
.tool-row label { font-size: 13px; color: #6b7280; }
.tool-row input[type="range"] { flex: 1; }
.tool-row button { padding: 4px 12px; border: 1px solid #d1d5db; border-radius: 4px; background: #fff; cursor: pointer; font-size: 13px; }
.tool-row button:hover { background: #f3f4f6; }

/* 参考图 */
.image-box { position: relative; min-height: 512px; border: 1px solid #e5e7eb; border-radius: 6px; background: #202225; display: flex; align-items: center; justify-content: center; overflow: hidden; }
.image-box canvas { position: absolute; inset: 0; width: 100%; height: 100%; cursor: crosshair; z-index: 0; }
.image-box .placeholder { color: #9ca3af; font-size: 14px; z-index: 1; }
.roi-box { position: absolute; border: 2px solid #d97706; background: rgba(217,119,6,.15); pointer-events: none; z-index: 2; }
.roi-hint { text-align: center; font-size: 12px; color: #6b7280; margin-top: 6px; }

/* 按钮 */
.action-btn { width: 100%; min-height: 48px; margin-top: 8px; border: 1px solid #d1d5db; border-radius: 6px; background: #fff; font-size: 14px; cursor: pointer; }
.action-btn:hover:not(:disabled) { background: #f3f4f6; }
.action-btn:disabled { opacity: .6; cursor: not-allowed; }
.file-label { display: flex; align-items: center; justify-content: center; position: relative; overflow: hidden; text-align: center; }
.file-label input[type="file"] { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
.file-label.disabled { opacity: .6; pointer-events: none; }
.error { color: #dc2626; font-size: 13px; margin-top: 6px; }
.upload-msg { font-size: 13px; font-weight: 600; margin-top: 6px; color: #d97706; }
.upload-msg.done { color: #16a34a; }
.upload-msg.fail { color: #dc2626; }

/* 内置参考：单行铺满整行宽度，放不下横向滚动 */
.ref-strip { display: flex; gap: 12px; overflow-x: auto; width: 100%; padding-bottom: 6px; }
.ref-strip img { width: 132px; height: 132px; object-fit: contain; border: 2px solid transparent; border-radius: 6px; cursor: pointer; flex-shrink: 0; background: #f3f4f6; }
.ref-strip img:hover, .ref-strip img.active { border-color: #d97706; }

/* 生成 */
.gen-row { display: flex; gap: 24px; }
.gen-controls { flex: 1; min-width: 260px; }
.gen-result { flex: 2; min-width: 320px; }
.param { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.param label { font-size: 13px; color: #6b7280; min-width: 60px; }
.param input[type="range"] { flex: 1; }
.param .val { font-size: 13px; min-width: 40px; text-align: right; color: #6b7280; }
.seed-input { width: 80px; padding: 4px 8px; border: 1px solid #d1d5db; border-radius: 4px; }
.generate-btn { width: 100%; min-height: 48px; margin-top: 8px; border: 0; border-radius: 6px; background: #d97706; color: #fff; font-size: 16px; font-weight: 700; cursor: pointer; }
.generate-btn:hover:not(:disabled) { background: #b65f05; }
.generate-btn:disabled { background: #d1d5db; cursor: not-allowed; }
.gen-status { min-height: 68px; padding: 12px; margin-top: 8px; background: #ecfdf5; border-radius: 6px; color: #065f46; font-weight: 600; font-size: 14px; text-align: center; display: flex; align-items: center; justify-content: center; }
.no-result { color: #9ca3af; font-size: 14px; text-align: center; padding: 60px 20px; }

/* 结果区 */
.result-viewer { border: 1px solid #e5e7eb; border-radius: 8px; overflow: hidden; background: #f9fafb; margin-bottom: 12px; }
.result-img-wrap { position: relative; }
.result-viewer .result-main { width: 100%; max-height: 400px; object-fit: contain; display: block; }
.download-btn { position: absolute; top: 8px; right: 8px; padding: 6px 14px; border: 0; border-radius: 4px; background: rgba(17,24,39,.65); color: #fff; font-size: 13px; cursor: pointer; backdrop-filter: blur(2px); }
.download-btn:hover { background: rgba(217,119,6,.9); }
.result-viewer .result-label { padding: 8px 12px; font-size: 14px; font-weight: 600; color: #374151; text-align: center; background: #fff; border-top: 1px solid #e5e7eb; }
.result-thumbs { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 4px; }
.thumb { flex-shrink: 0; width: 100px; border: 2px solid transparent; border-radius: 6px; overflow: hidden; cursor: pointer; text-align: center; background: #fff; }
.thumb.active { border-color: #d97706; }
.thumb img { width: 100%; height: 80px; object-fit: contain; display: block; }
.thumb span { display: block; font-size: 11px; color: #6b7280; padding: 2px 0 4px; }

@media (max-width: 900px) { .three-col { grid-template-columns: 1fr; } .gen-row { flex-direction: column; } .app-page { padding: 0 14px 36px; } }
</style>
