import { ref, computed, readonly } from 'vue'
import type { User, UserSettings } from '../types'
import * as authApi from '../api/auth'
import { get } from '../api/index'

const user = ref<User | null>(null)
const settings = ref<UserSettings | null>(null)
const loading = ref(false)
const initialized = ref(false)

export function useAuth() {
  const isLoggedIn = computed(() => user.value !== null)
  const userName = computed(() => user.value?.name || '游客')
  const userAvatar = computed(() => user.value?.avatar_url || '')
  const userEmail = computed(() => user.value?.email || '')

  /**
   * 初始化认证状态
   * 尝试拉取用户信息，如果失败且没有 access_token，尝试静默刷新一次
   */
  async function init() {
    if (initialized.value) return
    
    loading.value = true
    try {
      // 1. 尝试直接获取用户信息
      const me = await authApi.fetchMe()
      user.value = me.user
      settings.value = me.settings
    } catch (err) {
      console.warn('[Auth] FetchMe failed, checking if we can refresh...')
      // 2. 如果失败，且拦截器没有自动处理成功（通常是因为 localStorage 确实空的）
      // 我们这里可以尝试手动触发一次刷新
      try {
        const { refreshAccessToken } = await import('../api/index')
        await refreshAccessToken()
        const me = await authApi.fetchMe()
        user.value = me.user
        settings.value = me.settings
      } catch (refreshErr) {
        console.error('[Auth] Init/Refresh failed:', refreshErr)
      }
    } finally {
      initialized.value = true
      loading.value = false
    }
  }

  /**
   * 发起 OAuth 登录
   * 直接重定向到后端接口，由后端处理 state 并重定向到 Provider
   */
  function loginWithOAuth(provider: 'google' | 'github') {
    const apiBase = import.meta.env.VITE_API_BASE || ''
    const clientId = localStorage.getItem('cf_client_id') || ''
    window.location.href = `${apiBase}/api/auth/oauth/${provider}/login?client_id=${clientId}`
  }

  /**
   * 处理登录成功回调
   * 由 App.vue 在检测到 URL 参数/Hash 时调用
   */
  async function handleAuthSuccess(accessToken: string): Promise<boolean> {
    localStorage.setItem('cf_access_token', accessToken)
    initialized.value = false // 强制重新获取用户信息
    await init()
    return true
  }

  /**
   * 登出
   */
  async function logout() {
    loading.value = true
    try {
      await authApi.logout()
    } catch (err) {
      console.warn('[Auth] Logout API failed:', err)
    } finally {
      localStorage.removeItem('cf_access_token')
      user.value = null
      settings.value = null
      loading.value = false
    }
  }

  return {
    user: readonly(user),
    settings: readonly(settings),
    loading: readonly(loading),
    initialized: readonly(initialized),
    isLoggedIn,
    userName,
    userAvatar,
    userEmail,
    init,
    loginWithOAuth,
    handleAuthSuccess,
    logout
  }
}
