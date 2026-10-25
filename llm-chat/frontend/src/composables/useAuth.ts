import { ref, computed, readonly } from 'vue'
import type { User, UserSettings } from '../types'
import * as authApi from '../api/auth'
import { setAccessToken } from '../api/http'

const user = ref<User | null>(null)
const settings = ref<UserSettings | null>(null)
const loading = ref(false)
const initialized = ref(false)
let identityGeneration = 0
let pendingInit: Promise<void> | null = null

export function useAuth() {
  const isLoggedIn = computed(() => !!user.value)
  const userName = computed(() => user.value?.name || '游客')
  const userAvatar = computed(() => user.value?.avatar_url || '')
  const userEmail = computed(() => user.value?.email || '')

  /**
   * 初始化认证状态
   * 尝试拉取用户信息。如果 access_token 失效或缺失，api 拦截器会自动尝试 cookie 刷新。
   */
  function init(): Promise<void> {
    if (initialized.value) return Promise.resolve()
    if (pendingInit) return pendingInit
    const generation = identityGeneration
    loading.value = true
    const task = (async () => {
      try {
        const me = await authApi.fetchMe()
        if (generation !== identityGeneration) return
        user.value = me.user
        settings.value = me.settings
      } catch (err) {
        if (generation !== identityGeneration) return
        console.error('[Auth] Init failed:', err)
        user.value = null
        settings.value = null
      } finally {
        if (generation === identityGeneration) {
          initialized.value = true
          loading.value = false
        }
      }
    })()
    pendingInit = task
    const clearPending = () => { if (pendingInit === task) pendingInit = null }
    void task.then(clearPending, clearPending)
    return task
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
    const generation = ++identityGeneration
    pendingInit = null
    setAccessToken(accessToken)
    user.value = null
    settings.value = null
    initialized.value = false // 强制重新获取用户信息
    await init()
    return generation === identityGeneration && isLoggedIn.value
  }

  /**
   * 登出
   */
  async function logout() {
    const generation = ++identityGeneration
    pendingInit = null
    initialized.value = true
    user.value = null
    settings.value = null
    loading.value = true
    try {
      await authApi.logout()
    } catch (err) {
      console.warn('[Auth] Logout API failed:', err)
    } finally {
      if (generation !== identityGeneration) return
      setAccessToken(null)
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
