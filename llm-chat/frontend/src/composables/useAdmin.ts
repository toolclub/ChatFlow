import { ref, computed } from 'vue'
import { post } from '../api'

/**
 * 管理员密码门禁 composable —— 单例（module-scoped refs）。
 *
 * 智能选股 + 股票预测共享同一已验证会话：一次解锁，两页通用。
 * 验证通过后内存持有密钥（刷新即重输，与现有系统看板一致），
 * 所有受保护 API 调用经 getAdminHeaders() 注入 X-Admin-Key header。
 *
 * 仿 App.vue:24-34 verifyAdmin 流程 + SysDashboard.vue:33 X-Admin-Key header。
 */
const verifiedAdminKey = ref<string>('')
const isVerified = computed<boolean>(() => verifiedAdminKey.value.length > 0)

async function verify(key: string): Promise<boolean> {
  try {
    const res = await post<{ ok: boolean }>('/api/admin/verify', { key })
    if (res.ok) {
      verifiedAdminKey.value = key
      return true
    }
    // 非预期响应：清空并拒绝
    verifiedAdminKey.value = ''
    return false
  } catch {
    // 验证失败（401/网络）：清空，不抛异常给调用方（返回 false，由调用方决定如何提示）
    verifiedAdminKey.value = ''
    return false
  }
}

function logout(): void {
  verifiedAdminKey.value = ''
}

/**
 * 受保护端点请求头。已验证返回 { 'X-Admin-Key': key }，否则 {}。
 * 调用方将其并入 fetch headers（additive，不覆盖 Authorization/Content-Type）。
 */
function getAdminHeaders(): Record<string, string> {
  return isVerified.value ? { 'X-Admin-Key': verifiedAdminKey.value } : {}
}

export function useAdmin() {
  return {
    verifiedAdminKey,
    isVerified,
    verify,
    logout,
    getAdminHeaders,
  }
}
