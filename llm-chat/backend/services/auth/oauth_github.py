from typing import Dict, Any
import httpx
from .oauth_base import OAuthProvider

class GitHubOAuth(OAuthProvider):
    def __init__(self):
        super().__init__("github")
        self.auth_url = "https://github.com/login/oauth/authorize"
        self.token_url = "https://github.com/login/oauth/access_token"
        self.user_info_url = "https://api.github.com/user"

    def get_login_url(self, state: str) -> str:
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": "read:user user:email",
            "state": state
        }
        query = "&".join([f"{k}={v}" for k, v in params.items()])
        return f"{self.auth_url}?{query}"

    async def get_token(self, code: str) -> Dict[str, Any]:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": self.redirect_uri
        }
        headers = {"Accept": "application/json"}
        async with self._client() as client:
            resp = await client.post(self.token_url, data=data, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def get_user_info(self, access_token: str) -> Dict[str, Any]:
        headers = {
            "Authorization": f"token {access_token}",
            "Accept": "application/json"
        }
        async with self._client() as client:
            resp = await client.get(self.user_info_url, headers=headers)
            resp.raise_for_status()
            return resp.json()
