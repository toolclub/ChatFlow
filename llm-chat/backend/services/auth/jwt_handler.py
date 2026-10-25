import time
import jwt
from typing import Optional, Dict, Any
from config import settings

JWT_SECRET = settings.jwt_secret_key
JWT_ALGORITHM = settings.jwt_algorithm
ACCESS_EXPIRE = settings.jwt_access_expire_minutes * 60
REFRESH_EXPIRE = settings.jwt_refresh_expire_days * 24 * 60 * 60

def create_access_token(user_id: str) -> str:
    """创建 Access Token"""
    payload = {
        "sub": user_id,
        "type": "access",
        "exp": time.time() + ACCESS_EXPIRE,
        "iat": time.time(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def create_refresh_token(user_id: str) -> str:
    """创建 Refresh Token"""
    payload = {
        "sub": user_id,
        "type": "refresh",
        "exp": time.time() + REFRESH_EXPIRE,
        "iat": time.time(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

def verify_token(token: str) -> Optional[Dict[str, Any]]:
    """验证 Token"""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM], options={"require": ["sub", "exp", "iat", "type"]})
    except jwt.PyJWTError:
        return None
