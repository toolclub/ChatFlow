"""
第 9 层 – Extension（扩展）
CORS 中间件、插件钩子，以及未来的网关 / 多渠道支持。.
author: leizihao
email: lzh19162600626@gmail.com
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def apply_cors(app: FastAPI, origins: list[str] = None) -> None:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins or ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
