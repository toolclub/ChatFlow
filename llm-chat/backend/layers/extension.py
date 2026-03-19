"""
Layer 9 – Extension
CORS middleware, plugin hooks, future gateway / multi-channel support.
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
