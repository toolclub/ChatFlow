from typing import Dict, Any
import httpx
from .oauth_base import OAuthProvider

class GoogleOAuth(OAuthProvider):
    def __init__(self):
        super().__init__("google")
        self.auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
        self.token_url = "https://oauth2.googleapis.com/token"
        self.user_info_url = "https://www.googleapis.com/oauth2/v3/userinfo"

    def get_login_url(self, state: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "access_type": "offline",
            "prompt": "select_account"
        }
        query = "&".join([f"{k}={v}" for k, v in params.items()])
        return f"{self.auth_url}?{query}"

    async def get_token(self, code: str) -> Dict[str, Any]:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri
        }
        async with self._client() as client:
            resp = await client.post(self.token_url, data=data)
            resp.raise_for_status()
            return resp.json()

    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        headers = {"Authorization": f"Bearer {access_token}"}
        async with self._client() as client:
            resp = await client.get(self.user_info_url, headers=headers)
            resp.raise_for_status()
            return resp.json()
