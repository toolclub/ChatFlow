from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
import logging
import httpx
from config import settings

logger = logging.getLogger("oauth")

class OAuthProvider(ABC):
    def __init__(self, provider_name: str):
        self.provider_name = provider_name
        # 使用新配置格式：oauth_google_client_id, oauth_github_client_id 等
        if provider_name == "google":
            self.client_id = settings.oauth_google_client_id
            self.client_secret = settings.oauth_google_client_secret
            self.redirect_uri = settings.oauth_google_redirect_uri
        elif provider_name == "github":
            self.client_id = settings.oauth_github_client_id
            self.client_secret = settings.oauth_github_client_secret
            self.redirect_uri = settings.oauth_github_redirect_uri
        else:
            raise ValueError(f"Unknown OAuth provider: {provider_name}")

        # 防御：检查 ENC 值是否未被解密（开发配置错误时方便定位）
        if self.client_id.startswith("ENC("):
            logger.error(
                "OAuth %s client_id 仍是加密格式，解密可能未执行。"
                " 请确认密钥文件存在且 decrypt_env 正常加载。",
                provider_name,
            )

    @abstractmethod
    def get_login_url(self, state: str) -> str:
        """获取登录跳转 URL"""
        pass

    @abstractmethod
    async def get_token(self, code: str) -> Dict[str, Any]:
        """用 code 换取 access_token"""
        pass

    @abstractmethod
    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        """用 access_token 获取用户信息"""
        pass

    async def exchange_code_for_user(self, code: str) -> Dict[str, Any]:
        """完整流程：code -> token -> user_info"""
        token_data = await self.get_token(code)
        access_token = token_data.get("access_token")
        if not access_token:
            raise ValueError(f"Failed to get access_token from {self.provider_name}")
        
        user_info = await self.get_user_info(access_token)
        return {
            "provider": self.provider_name,
            "provider_id": str(user_info.get("id") or user_info.get("sub")),
            "email": user_info.get("email"),
            "name": user_info.get("name") or user_info.get("login"),
            "avatar_url": user_info.get("avatar_url") or user_info.get("picture"),
            "raw_profile": user_info
        }
