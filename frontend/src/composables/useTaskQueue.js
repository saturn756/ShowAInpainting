// 任务队列：提交、轮询、状态管理
import { apiCall } from './useApi.js'

/**
 * 提交生成任务
 * @returns {{ task_id, status, queue_ahead, user_task_index, message }}
 */
export async function submitTask(params) {
  return apiCall('POST', '/api/tasks', params)
}

/**
 * 查询单个任务状态
 */
export async function getTask(taskId) {
  return apiCall('GET', `/api/tasks/${taskId}`)
}

/**
 * 获取当前用户所有任务和结果
 */
export async function listTasks() {
  return apiCall('GET', '/api/tasks')
}

/**
 * 轮询任务直到完成或失败。
 * 网络瞬时错误（如隧道抖动导致逻辑服务器接口短暂不可达）不停止轮询，
 * 持续重试；只有连续多次失败才放弃，避免任务卡在前端、实际已在后端排队。
 * @param {string} taskId
 * @param {(data) => void} onUpdate  状态更新回调
 * @param {() => boolean} shouldStop  返回 true 则停止轮询
 */
export async function pollTask(taskId, onUpdate, shouldStop) {
  let errCount = 0
  const MAX_ERR = 10
  while (true) {
    if (shouldStop && shouldStop()) return
    try {
      const data = await getTask(taskId)
      errCount = 0
      onUpdate(data)
      if (data.status === 'done' || data.status === 'failed') return
    } catch {
      errCount++
      onUpdate({ status: 'error', error: '网络错误' })
      if (errCount >= MAX_ERR) return  // 连续失败才放弃
    }
    await new Promise(r => setTimeout(r, 2000))
  }
}
