"""LLM 供应商管理 —— 单元测试（cc-switch 模式）

用例编号（见 test_case.md「十-J」）：
  T-LLM-P01: admin 鉴权 — 新端点全量 X-Admin-Key 拦截（无 key / 错 key → 403）
  T-LLM-P02: CRUD — create happy path（默认不激活）/ name 重复 409 / PUT·DELETE 不存在 404
  T-LLM-P03: 激活联动 — activate 调 store.activate_provider + 失效广播；
             deactivate 调 deactivate_all + 广播；删激活项 / 改激活项 → 广播
  T-LLM-P04: resolve_chat 模型名映射全分支（None/CHAT/SUMMARY/ROUTER/未知名/无快照回退 env）
  T-LLM-P05: resolve_vision 两分支（快照含 vision_model / 回退 .env VISION_*）
  T-LLM-P06: llm/chat 工厂快照感知 — get_chat_llm / get_vision_llm / get_vision_async_client
             的 base_url+model 正确，缓存 key 随快照切换新建条目
  T-LLM-P07: GET /api/models 两态 — 有快照返回 chat_model+extra_models（去重、无 base/key），
             无快照回退 env 行为（Ollama 拉取失败 → [CHAT_MODEL]）
  T-LLM-P08: 迁移幂等静态保证 — _MIGRATIONS 全部 DDL 均为 IF NOT EXISTS 系可重入语句，
             llm_providers 表 DDL + ux_llmprov_name 唯一索引在列（T-DB-04 思路的无 DB 版）；
             _notify_llm_config_changed 在 DB/Redis 双双异常时不向上传播（铁律 #9）
  T-LLM-P09: POST /test 连通性 — ok/false/传 {id}/字段全空 四路；
             POST /fetch-models 成功 / 上游失败 502
  T-LLM-P10: 同名角色缺陷修复 — env 角色名与 CHAT_MODEL 同名时（当前生产 .env 即如此），
             名字映射退化为 chat_model（回归护栏）；角色感知的 get_summary_llm /
             get_router_llm（resolve_summary / resolve_router）仍返回快照角色专属模型

隔离手段（mirror test_prediction_auth.py）：
  - monkeypatch config.ADMIN_SECRET_KEY + admin_router.ADMIN_SECRET_KEY 到测试值
  - monkeypatch db/llm_provider_store 模块函数（内存 dict 假库，同时挡住 admin_router 与
    llm_provider_service.refresh_from_db），避免真实 DB
  - monkeypatch admin_router._notify_llm_config_changed 拦截 Redis 广播
  - monkeypatch services/llm_provider_service 的 env 常量与 _snapshot，直接钉死快照态
"""
from __future__ import annotations

import asyncio
from itertools import count
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config  # noqa: E402
import db.llm_provider_store as store_mod  # noqa: E402
from db import migrate as migrate_mod  # noqa: E402
from db.models import LLMProviderModel  # noqa: E402
from llm import chat as llm_chat  # noqa: E402
from routers import admin_router, model_router  # noqa: E402
from services import llm_provider_service as service  # noqa: E402

TEST_KEY = "test-admin-secret-12345"

# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _pin_admin_key(monkeypatch):
    """钉住 admin 密钥（verify_admin_access 读 live config，/verify 读 router 快照）。"""
    monkeypatch.setattr(config, "ADMIN_SECRET_KEY", TEST_KEY)
    monkeypatch.setattr(admin_router, "ADMIN_SECRET_KEY", TEST_KEY)


@pytest.fixture(autouse=True)
def _reset_snapshot(monkeypatch):
    """每个用例从「无快照」状态出发（回退 env），需要快照的用例自行设置。"""
    monkeypatch.setattr(service, "_snapshot", None)


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(admin_router.router)
    _app.include_router(model_router.router)
    return _app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def fake_store(monkeypatch):
    """内存 dict 假库：同时挡住 admin_router 的 CRUD 与 service.refresh_from_db。"""
    rows: dict[int, SimpleNamespace] = {}
    captured: dict = {}
    next_id = count(1)

    def _row(pid: int, **kw) -> SimpleNamespace:
        """构造一行并插入内存表（测试预置数据用）。"""
        base = dict(
            id=pid, name="", base_url="", api_key="", chat_model="",
            summary_model=None, router_model=None, vision_model=None,
            extra_models=[], remark=None, enabled=True, is_active=False,
            created_at=1.0, updated_at=1.0,
        )
        base.update(kw)
        row = SimpleNamespace(**base)
        rows[pid] = row
        return row

    async def _list_providers():
        captured["list"] = True
        return [rows[i] for i in sorted(rows)]

    async def _get_provider(pid):
        return rows.get(pid)

    async def _get_provider_by_name(name):
        return next((r for r in rows.values() if r.name == name), None)

    async def _create_provider(**fields):
        captured["create"] = fields
        pid = next(next_id)
        rows[pid] = _row(pid, **fields)
        return rows[pid]

    async def _update_provider(pid, **fields):
        captured["update"] = (pid, fields)
        row = rows.get(pid)
        if not row:
            return None
        for k, v in fields.items():
            setattr(row, k, v)
        return row

    async def _delete_provider(pid):
        captured["delete"] = pid
        if pid not in rows:
            return False
        del rows[pid]
        return True

    async def _get_active_provider():
        return next((r for r in rows.values() if r.is_active and r.enabled), None)

    async def _activate_provider(pid):
        captured["activate"] = pid
        row = rows.get(pid)
        if not row:
            return None
        for r in rows.values():
            r.is_active = False
        row.is_active = True
        return row

    async def _deactivate_all():
        captured["deactivate_all"] = True
        return sum(1 for r in rows.values() if r.is_active)

    monkeypatch.setattr(store_mod, "list_providers", _list_providers)
    monkeypatch.setattr(store_mod, "get_provider", _get_provider)
    monkeypatch.setattr(store_mod, "get_provider_by_name", _get_provider_by_name)
    monkeypatch.setattr(store_mod, "create_provider", _create_provider)
    monkeypatch.setattr(store_mod, "update_provider", _update_provider)
    monkeypatch.setattr(store_mod, "delete_provider", _delete_provider)
    monkeypatch.setattr(store_mod, "get_active_provider", _get_active_provider)
    monkeypatch.setattr(store_mod, "activate_provider", _activate_provider)
    monkeypatch.setattr(store_mod, "deactivate_all", _deactivate_all)
    return SimpleNamespace(rows=rows, captured=captured, row=_row)


@pytest.fixture
def notify_spy(monkeypatch):
    """拦截写操作后的「本机刷新 + Redis 广播」。"""
    calls: list[dict] = []

    async def _spy():
        calls.append({"called": True})

    monkeypatch.setattr(admin_router, "_notify_llm_config_changed", _spy)
    return calls


def _pin_env_names(monkeypatch):
    """把 service 模块内的 env 角色名钉成互不相同的假值，让映射分支可独立触发。

    生产 .env 里所有角色常是同一个模型名（如 deepseek-v4-pro），
    不钉住的话 SUMMARY/ROUTER 分支永远走不进去。
    （角色感知函数 resolve_summary/resolve_router 读的也是这些 service 模块常量。）
    """
    monkeypatch.setattr(service, "LLM_BASE_URL", "https://env.example/v1")
    monkeypatch.setattr(service, "API_KEY", "sk-env")
    monkeypatch.setattr(service, "CHAT_MODEL", "env-chat")
    monkeypatch.setattr(service, "SUMMARY_MODEL", "env-summary")
    monkeypatch.setattr(service, "ROUTER_MODEL", "env-router")
    monkeypatch.setattr(service, "SEARCH_MODEL", "env-search")
    monkeypatch.setattr(service, "VISION_BASE_URL", "https://vision-env.example/v1")
    monkeypatch.setattr(service, "VISION_API_KEY", "sk-vision-env")
    monkeypatch.setattr(service, "VISION_MODEL", "env-vision")
    monkeypatch.setattr(service, "_ENV_ROLE_MODELS", {"env-router", "env-search", "env-map"})


def _pin_same_env_names(monkeypatch):
    """复刻生产 .env 场景：所有角色名与 CHAT_MODEL 同名（名字映射失效的场景）。"""
    monkeypatch.setattr(service, "LLM_BASE_URL", "https://env.example/v1")
    monkeypatch.setattr(service, "API_KEY", "sk-env")
    for name in ("CHAT_MODEL", "SUMMARY_MODEL", "ROUTER_MODEL", "SEARCH_MODEL"):
        monkeypatch.setattr(service, name, "same-name-model")
    monkeypatch.setattr(service, "_ENV_ROLE_MODELS", {"same-name-model"})


def _make_snapshot(**kw) -> service.ActiveProviderSnapshot:
    fields = dict(
        base_url="https://snap.example/v1", api_key="sk-snap",
        chat_model="snap-chat", summary_model="snap-summary",
        router_model="snap-router", vision_model="snap-vision",
        extra_models=["extra-a", "extra-b"],
    )
    fields.update(kw)
    return service.ActiveProviderSnapshot(**fields)


# ── T-LLM-P01: 新端点全量 admin 鉴权 ────────────────────────────────────────

_NEW_ENDPOINTS = [
    ("get", "/api/admin/llm-providers"),
    ("post", "/api/admin/llm-providers"),
    ("put", "/api/admin/llm-providers/1"),
    ("delete", "/api/admin/llm-providers/1"),
    ("post", "/api/admin/llm-providers/1/activate"),
    ("post", "/api/admin/llm-providers/deactivate"),
    ("post", "/api/admin/llm-providers/test"),
    ("post", "/api/admin/llm-providers/fetch-models"),
]

_CREATE_BODY = {
    "name": "p", "base_url": "https://x.example/v1",
    "api_key": "sk", "chat_model": "m",
}


@pytest.mark.unit
@pytest.mark.parametrize("method,path", _NEW_ENDPOINTS)
def test_p01_no_admin_key_403(client, method, path):
    """无 X-Admin-Key → 403（铁律 #1：拦截来自后端，普通用户不可见）。"""
    kwargs = {"json": _CREATE_BODY} if method in ("post", "put") else {}
    r = getattr(client, method)(path, **kwargs)
    assert r.status_code == 403


@pytest.mark.unit
@pytest.mark.parametrize("method,path", _NEW_ENDPOINTS)
def test_p01_wrong_admin_key_403(client, method, path):
    """错 key → 403。"""
    headers = {"X-Admin-Key": "definitely-wrong"}
    kwargs = {"json": _CREATE_BODY} if method in ("post", "put") else {}
    r = getattr(client, method)(path, headers=headers, **kwargs)
    assert r.status_code == 403


# ── T-LLM-P02: CRUD ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_p02_create_happy_path(client, fake_store):
    r = client.post(
        "/api/admin/llm-providers", json=_CREATE_BODY, headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 200
    body = r.json()["provider"]
    assert body["name"] == "p"
    assert body["is_active"] is False, "新建配置默认不激活"
    assert body["enabled"] is True
    assert fake_store.captured["create"]["is_active"] is False


@pytest.mark.unit
def test_p02_create_duplicate_name_409(client, fake_store):
    fake_store.row(1, name="p", base_url="u", api_key="k", chat_model="m")
    r = client.post(
        "/api/admin/llm-providers", json=_CREATE_BODY, headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 409


@pytest.mark.unit
def test_p02_update_missing_404(client, fake_store):
    r = client.put(
        "/api/admin/llm-providers/999", json={"remark": "x"},
        headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 404


@pytest.mark.unit
def test_p02_update_duplicate_name_409(client, fake_store):
    fake_store.row(1, name="a", base_url="u", api_key="k", chat_model="m")
    fake_store.row(2, name="b", base_url="u", api_key="k", chat_model="m")
    r = client.put(
        "/api/admin/llm-providers/2", json={"name": "a"},
        headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 409


@pytest.mark.unit
def test_p02_update_partial_fields_only(client, fake_store):
    """PUT 只传 remark → 其他字段不被清空（exclude_unset 语义）。"""
    fake_store.row(1, name="a", base_url="u", api_key="k", chat_model="m")
    r = client.put(
        "/api/admin/llm-providers/1", json={"remark": "新备注"},
        headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 200
    provider = r.json()["provider"]
    assert provider["remark"] == "新备注"
    assert provider["name"] == "a" and provider["chat_model"] == "m"
    pid, fields = fake_store.captured["update"]
    assert pid == 1 and fields == {"remark": "新备注"}


@pytest.mark.unit
def test_p02_delete_missing_404(client, fake_store):
    r = client.delete("/api/admin/llm-providers/999", headers={"X-Admin-Key": TEST_KEY})
    assert r.status_code == 404


@pytest.mark.unit
def test_p02_list_returns_full_rows_and_effective_env(client, fake_store, monkeypatch):
    fake_store.row(1, name="a", base_url="https://a.example/v1", api_key="sk-a",
                   chat_model="m-a", is_active=False)
    r = client.get("/api/admin/llm-providers", headers={"X-Admin-Key": TEST_KEY})
    assert r.status_code == 200
    body = r.json()
    assert len(body["providers"]) == 1
    assert body["providers"][0]["api_key"] == "sk-a", "admin 列表返回 api_key 明文（cc-switch 同做法）"
    assert body["effective"]["source"] == "env"
    assert "api_key" not in body["effective"], "effective 永不泄漏 api_key"
    assert body["effective"]["chat_model"] == config.CHAT_MODEL


@pytest.mark.unit
def test_p02_list_effective_db_source(client, fake_store, monkeypatch):
    monkeypatch.setattr(service, "_snapshot", _make_snapshot())
    r = client.get("/api/admin/llm-providers", headers={"X-Admin-Key": TEST_KEY})
    body = r.json()
    assert body["effective"]["source"] == "db"
    assert body["effective"]["base_url"] == "https://snap.example/v1"
    assert body["effective"]["models"] == ["snap-chat", "extra-a", "extra-b"]


# ── T-LLM-P03: 激活 / 停用 / 删除联动 ──────────────────────────────────────


@pytest.mark.unit
def test_p03_activate_calls_store_and_notify(client, fake_store, notify_spy):
    fake_store.row(1, name="a", base_url="u", api_key="k", chat_model="m")
    r = client.post("/api/admin/llm-providers/1/activate", headers={"X-Admin-Key": TEST_KEY})
    assert r.status_code == 200
    assert r.json()["provider"]["is_active"] is True
    assert fake_store.captured["activate"] == 1
    assert len(notify_spy) == 1, "激活后必须广播失效"


@pytest.mark.unit
def test_p03_activate_missing_404(client, fake_store, notify_spy):
    r = client.post("/api/admin/llm-providers/999/activate", headers={"X-Admin-Key": TEST_KEY})
    assert r.status_code == 404
    assert notify_spy == []


@pytest.mark.unit
def test_p03_deactivate_calls_store_and_notify(client, fake_store, notify_spy):
    fake_store.row(1, name="a", base_url="u", api_key="k", chat_model="m", is_active=True)
    r = client.post("/api/admin/llm-providers/deactivate", headers={"X-Admin-Key": TEST_KEY})
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert fake_store.captured.get("deactivate_all") is True
    assert len(notify_spy) == 1


@pytest.mark.unit
def test_p03_delete_active_provider_notifies(client, fake_store, notify_spy):
    fake_store.row(1, name="a", base_url="u", api_key="k", chat_model="m", is_active=True)
    r = client.delete("/api/admin/llm-providers/1", headers={"X-Admin-Key": TEST_KEY})
    assert r.status_code == 200
    assert fake_store.captured["delete"] == 1
    assert len(notify_spy) == 1, "删激活项 → 广播（各 worker 回退 env）"


@pytest.mark.unit
def test_p03_delete_inactive_provider_still_notifies(client, fake_store, notify_spy):
    """所有写操作统一广播（spec），非激活删除也不例外。"""
    fake_store.row(1, name="a", base_url="u", api_key="k", chat_model="m")
    r = client.delete("/api/admin/llm-providers/1", headers={"X-Admin-Key": TEST_KEY})
    assert r.status_code == 200
    assert len(notify_spy) == 1


@pytest.mark.unit
def test_p03_update_active_provider_notifies(client, fake_store, notify_spy):
    fake_store.row(1, name="a", base_url="u", api_key="k", chat_model="m", is_active=True)
    r = client.put(
        "/api/admin/llm-providers/1", json={"chat_model": "m2"},
        headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 200
    assert len(notify_spy) == 1, "改激活中配置 → 生效值变化，广播"


@pytest.mark.unit
def test_p03_update_inactive_provider_no_notify(client, fake_store, notify_spy):
    fake_store.row(1, name="a", base_url="u", api_key="k", chat_model="m")
    r = client.put(
        "/api/admin/llm-providers/1", json={"chat_model": "m2"},
        headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 200
    assert notify_spy == [], "改非激活配置不影响生效值，不必广播"


@pytest.mark.unit
def test_p03_create_does_not_notify(client, fake_store, notify_spy):
    """新建默认不激活，不影响生效配置 → 不广播。"""
    r = client.post(
        "/api/admin/llm-providers", json=_CREATE_BODY, headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 200
    assert notify_spy == []


# ── T-LLM-P04: resolve_chat 映射规则全分支 ─────────────────────────────────


@pytest.mark.unit
def test_p04_resolve_chat_without_snapshot(monkeypatch):
    _pin_env_names(monkeypatch)
    assert service.resolve_chat(None) == ("https://env.example/v1", "sk-env", "env-chat")
    assert service.resolve_chat("user-pick") == ("https://env.example/v1", "sk-env", "user-pick")


@pytest.mark.unit
def test_p04_resolve_chat_with_snapshot_all_branches(monkeypatch):
    _pin_env_names(monkeypatch)
    monkeypatch.setattr(service, "_snapshot", _make_snapshot())
    snap_triple = ("https://snap.example/v1", "sk-snap")

    # None / env 默认名 → 快照主模型
    assert service.resolve_chat(None) == (*snap_triple, "snap-chat")
    assert service.resolve_chat("env-chat") == (*snap_triple, "snap-chat")
    # 摘要角色 → 快照.summary_model
    assert service.resolve_chat("env-summary") == (*snap_triple, "snap-summary")
    # 路由/搜索/MAP 角色 → 快照.router_model
    assert service.resolve_chat("env-router") == (*snap_triple, "snap-router")
    assert service.resolve_chat("env-search") == (*snap_triple, "snap-router")
    assert service.resolve_chat("env-map") == (*snap_triple, "snap-router")
    # 用户显式选择的模型名 → 原样透传
    assert service.resolve_chat("extra-a") == (*snap_triple, "extra-a")


@pytest.mark.unit
def test_p04_resolve_chat_role_fallback_when_blank(monkeypatch):
    """快照未填 summary/router 模型 → 回退快照.chat_model。"""
    _pin_env_names(monkeypatch)
    monkeypatch.setattr(
        service, "_snapshot", _make_snapshot(summary_model="", router_model=""),
    )
    assert service.resolve_chat("env-summary")[2] == "snap-chat"
    assert service.resolve_chat("env-router")[2] == "snap-chat"


@pytest.mark.unit
def test_p04_resolve_default_request_model(monkeypatch):
    _pin_env_names(monkeypatch)
    assert service.resolve_default_request_model() == "env-chat"
    monkeypatch.setattr(service, "_snapshot", _make_snapshot())
    assert service.resolve_default_request_model() == "snap-chat"


# ── T-LLM-P05: resolve_vision 两分支 ────────────────────────────────────────


@pytest.mark.unit
def test_p05_resolve_vision_snapshot(monkeypatch):
    _pin_env_names(monkeypatch)
    monkeypatch.setattr(service, "_snapshot", _make_snapshot())
    assert service.resolve_vision() == ("https://snap.example/v1", "sk-snap", "snap-vision")


@pytest.mark.unit
def test_p05_resolve_vision_env_fallback(monkeypatch):
    _pin_env_names(monkeypatch)
    assert service.resolve_vision() == ("https://vision-env.example/v1", "sk-vision-env", "env-vision")


@pytest.mark.unit
def test_p05_resolve_vision_snapshot_without_vision_model(monkeypatch):
    """快照存在但未配置 vision_model → 视觉不随供应商切换，沿用 .env VISION_*。"""
    _pin_env_names(monkeypatch)
    monkeypatch.setattr(service, "_snapshot", _make_snapshot(vision_model=""))
    assert service.resolve_vision() == ("https://vision-env.example/v1", "sk-vision-env", "env-vision")


# ── T-LLM-P06: llm/chat 工厂快照感知 ────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_llm_caches(monkeypatch):
    """隔离工厂模块级缓存：换成空 dict，避免跨用例污染（monkeypatch 结束后还原）。"""
    monkeypatch.setattr(llm_chat, "_http_cache", {})
    monkeypatch.setattr(llm_chat, "_client_cache", {})


@pytest.mark.unit
def test_p06_get_chat_llm_with_snapshot(monkeypatch):
    _pin_env_names(monkeypatch)
    monkeypatch.setattr(service, "_snapshot", _make_snapshot())
    llm = llm_chat.get_chat_llm()
    assert llm.model == "snap-chat"
    assert str(llm._client.base_url).rstrip("/") == "https://snap.example/v1"
    # 底层 HTTP 客户端进了共享缓存（key = base_url:api_key）
    assert "https://snap.example/v1:sk-snap" in llm_chat._http_cache


@pytest.mark.unit
def test_p06_get_chat_llm_without_snapshot(monkeypatch):
    _pin_env_names(monkeypatch)
    llm = llm_chat.get_chat_llm()
    assert llm.model == "env-chat"
    assert str(llm._client.base_url).rstrip("/") == "https://env.example/v1"


@pytest.mark.unit
def test_p06_snapshot_switch_creates_new_cache_entry(monkeypatch):
    """切换供应商后缓存 key 自然变化 → 新建条目，旧条目保留（不清空 dict）。"""
    _pin_env_names(monkeypatch)
    llm_chat.get_chat_llm()
    monkeypatch.setattr(service, "_snapshot", _make_snapshot())
    llm2 = llm_chat.get_chat_llm()
    assert llm2.model == "snap-chat"
    assert len(llm_chat._client_cache) == 2, "新旧配置各一条缓存"


@pytest.mark.unit
def test_p06_get_summary_llm_with_snapshot(monkeypatch):
    _pin_env_names(monkeypatch)
    monkeypatch.setattr(service, "_snapshot", _make_snapshot())
    llm = llm_chat.get_summary_llm()
    assert llm.model == "snap-summary"
    assert llm.temperature == 0.2


@pytest.mark.unit
def test_p06_get_vision_llm_snapshot_overrides(monkeypatch):
    _pin_env_names(monkeypatch)
    monkeypatch.setattr(service, "_snapshot", _make_snapshot())
    llm = llm_chat.get_vision_llm(model="user-pick")
    assert llm.model == "snap-vision", "快照配置了 vision_model 时显式 model 让位"
    assert str(llm._client.base_url).rstrip("/") == "https://snap.example/v1"


@pytest.mark.unit
def test_p06_get_vision_llm_env_fallback_keeps_model(monkeypatch):
    _pin_env_names(monkeypatch)
    llm = llm_chat.get_vision_llm(model="user-pick")
    assert llm.model == "user-pick", "env 模式保留显式 model 回退链"
    assert str(llm._client.base_url).rstrip("/") == "https://vision-env.example/v1"
    assert llm_chat.get_vision_llm().model == "env-vision"


@pytest.mark.unit
def test_p06_get_vision_async_client(monkeypatch):
    _pin_env_names(monkeypatch)
    monkeypatch.setattr(service, "_snapshot", _make_snapshot())
    c1 = llm_chat.get_vision_async_client()
    c2 = llm_chat.get_vision_async_client()
    assert c1 is c2, "复用共享 HTTP 缓存"
    assert str(c1.base_url).rstrip("/") == "https://snap.example/v1"


# ── T-LLM-P10: 同名角色缺陷修复（角色感知解析） ─────────────────────────────


@pytest.mark.unit
def test_p10a_summary_role_aware_with_same_env_names(monkeypatch):
    """同名场景（CHAT==SUMMARY==ROUTER）下 get_summary_llm 仍返回快照.summary_model。

    名字映射（resolve_chat）在该场景下退化为 chat_model —— 已知局限的回归护栏；
    角色感知的 get_summary_llm（resolve_summary）不受同名碰撞影响。
    """
    _pin_same_env_names(monkeypatch)
    monkeypatch.setattr(service, "_snapshot", _make_snapshot())

    # 名字映射路径：同名 → 退化为 chat_model（文档化局限）
    assert service.resolve_chat("same-name-model")[2] == "snap-chat"
    # 角色感知路径：拿到角色专属模型
    llm = llm_chat.get_summary_llm()
    assert llm.model == "snap-summary"
    assert llm.temperature == 0.2
    assert str(llm._client.base_url).rstrip("/") == "https://snap.example/v1"
    # 无快照 → 回退 env 角色名（SUMMARY_MODEL）
    monkeypatch.setattr(service, "_snapshot", None)
    assert llm_chat.get_summary_llm().model == "same-name-model"


@pytest.mark.unit
def test_p10b_router_role_aware_with_same_env_names(monkeypatch):
    """同名场景下 get_router_llm 返回快照.router_model（route_node 调用路径）。"""
    _pin_same_env_names(monkeypatch)
    monkeypatch.setattr(service, "_snapshot", _make_snapshot())

    assert service.resolve_chat("same-name-model")[2] == "snap-chat"
    llm = llm_chat.get_router_llm()
    assert llm.model == "snap-router"
    assert llm.temperature == 0.0
    assert str(llm._client.base_url).rstrip("/") == "https://snap.example/v1"
    # 快照 router_model 留空 → 回退快照.chat_model
    monkeypatch.setattr(service, "_snapshot", _make_snapshot(router_model=""))
    assert llm_chat.get_router_llm().model == "snap-chat"
    # 无快照 → 回退 env ROUTER_MODEL
    monkeypatch.setattr(service, "_snapshot", None)
    assert llm_chat.get_router_llm().model == "same-name-model"


# ── T-LLM-P07: GET /api/models 两态 ────────────────────────────────────────


@pytest.mark.unit
def test_p07_models_with_snapshot(client, monkeypatch):
    monkeypatch.setattr(
        service, "_snapshot",
        _make_snapshot(extra_models=["extra-a", "snap-chat"]),  # 带重复名
    )
    r = client.get("/api/models")
    assert r.status_code == 200
    assert r.json() == {"models": ["snap-chat", "extra-a"]}, "去重保序，且不含 base_url/api_key"


@pytest.mark.unit
def test_p07_models_without_snapshot_env_fallback(client, monkeypatch):
    monkeypatch.setattr(service, "_snapshot", None)
    # 指向不可达端点 → 走异常回退 [CHAT_MODEL]，不打真实网络
    monkeypatch.setattr(model_router, "API_BASE_URL", "http://127.0.0.1:9")
    r = client.get("/api/models")
    assert r.status_code == 200
    assert r.json() == {"models": [config.CHAT_MODEL]}


# ── T-LLM-P08: 迁移幂等静态保证（无 DB 版 T-DB-04 思路） ────────────────────


@pytest.mark.unit
def test_p08_migrations_idempotent_by_construction():
    """所有 DDL 均为可重入语句：二次执行不报错的结构性保证。"""
    assert migrate_mod._MIGRATIONS, "迁移列表为空"
    for sql in migrate_mod._MIGRATIONS:
        s = sql.upper()
        if s.strip().startswith("CREATE TABLE"):
            assert "IF NOT EXISTS" in s, f"CREATE TABLE 缺 IF NOT EXISTS: {sql[:60]}"
        elif s.strip().startswith("CREATE INDEX") or s.strip().startswith("CREATE UNIQUE INDEX"):
            assert "IF NOT EXISTS" in s, f"CREATE INDEX 缺 IF NOT EXISTS: {sql[:60]}"
        elif s.strip().startswith("ALTER TABLE") and "ADD COLUMN" in s:
            assert "IF NOT EXISTS" in s, f"ADD COLUMN 缺 IF NOT EXISTS: {sql[:60]}"


@pytest.mark.unit
def test_p08_llm_providers_ddl_present():
    """llm_providers 建表语句 + 唯一索引在迁移列表中，与 ORM 表名一致。"""
    assert LLMProviderModel.__tablename__ == "llm_providers"
    joined = "\n".join(migrate_mod._MIGRATIONS)
    assert "CREATE TABLE IF NOT EXISTS llm_providers" in joined
    assert "ux_llmprov_name" in joined and "ON llm_providers(name)" in joined


@pytest.mark.unit
def test_p08_notify_helper_survives_redis_and_db_failure(monkeypatch):
    """_notify_llm_config_changed：刷新/广播双双抛异常也不向上传播（铁律 #9 降级）。"""
    async def _boom():
        raise RuntimeError("redis down")

    async def _boom2():
        raise RuntimeError("db down")

    monkeypatch.setattr(service, "refresh_from_db", _boom2)
    import db.redis_state as redis_state
    monkeypatch.setattr(redis_state, "publish_llm_config_invalidation", _boom)
    asyncio.run(admin_router._notify_llm_config_changed())  # 不抛即通过


# ── 连通性测试 / 拉模型端点（AsyncOpenAI 打桩） ────────────────────────────


class _FakeAsyncOpenAI:
    """admin_router.AsyncOpenAI 桩：按 outcome 返回成功/异常，并记录入参。"""
    outcome = "ok"
    last = None

    def __init__(self, base_url=None, api_key=None, timeout=None, **kw):
        self.base_url = base_url
        self.api_key = api_key
        self.timeout = timeout
        _FakeAsyncOpenAI.last = self
        self.chat = SimpleNamespace(completions=_FakeCompletions())
        self.models = SimpleNamespace(list=lambda: _FakeModelPage())


class _FakeCompletions:
    @staticmethod
    async def create(**kwargs):
        if _FakeAsyncOpenAI.outcome == "fail":
            raise RuntimeError("Insufficient Balance (余额不足)")
        return SimpleNamespace(choices=[
            SimpleNamespace(message=SimpleNamespace(content="OK，一切正常")),
        ])


class _FakeModelPage:
    """模拟 openai 的 async 分页：models.list() 返回可 async-for 的页。"""

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for mid in ("m-a", "m-b"):
            yield SimpleNamespace(id=mid)


@pytest.mark.unit
def test_connectivity_ok(client, monkeypatch):
    monkeypatch.setattr(admin_router, "AsyncOpenAI", _FakeAsyncOpenAI)
    _FakeAsyncOpenAI.outcome = "ok"
    r = client.post(
        "/api/admin/llm-providers/test",
        json={"base_url": "https://x.example/v1", "api_key": "sk", "model": "m"},
        headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["error"] is None
    assert body["content"].startswith("OK")
    assert isinstance(body["latency_ms"], int)


@pytest.mark.unit
def test_connectivity_error_passthrough(client, monkeypatch):
    """ok=false 时错误信息原样透传（订阅额度不足要能给前端看）。"""
    monkeypatch.setattr(admin_router, "AsyncOpenAI", _FakeAsyncOpenAI)
    _FakeAsyncOpenAI.outcome = "fail"
    r = client.post(
        "/api/admin/llm-providers/test",
        json={"base_url": "https://x.example/v1", "api_key": "sk", "model": "m"},
        headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 200  # 测试本身成功，结果 ok=false
    body = r.json()
    assert body["ok"] is False
    assert "Insufficient Balance" in body["error"]


@pytest.mark.unit
def test_connectivity_by_stored_id(client, fake_store, monkeypatch):
    """传 {id} 直接测已存配置（显式字段缺省时取库里的值）。"""
    monkeypatch.setattr(admin_router, "AsyncOpenAI", _FakeAsyncOpenAI)
    _FakeAsyncOpenAI.outcome = "ok"
    fake_store.row(1, name="a", base_url="https://stored.example/v1",
                   api_key="sk-stored", chat_model="stored-m")
    r = client.post(
        "/api/admin/llm-providers/test", json={"id": 1}, headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 200
    assert _FakeAsyncOpenAI.last.base_url == "https://stored.example/v1"
    assert _FakeAsyncOpenAI.last.api_key == "sk-stored"


@pytest.mark.unit
def test_connectivity_missing_fields_422(client, monkeypatch):
    monkeypatch.setattr(admin_router, "AsyncOpenAI", _FakeAsyncOpenAI)
    r = client.post(
        "/api/admin/llm-providers/test", json={}, headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 422


@pytest.mark.unit
def test_fetch_models_ok(client, monkeypatch):
    monkeypatch.setattr(admin_router, "AsyncOpenAI", _FakeAsyncOpenAI)
    r = client.post(
        "/api/admin/llm-providers/fetch-models",
        json={"base_url": "https://x.example/v1", "api_key": "sk"},
        headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 200
    assert r.json() == {"models": ["m-a", "m-b"]}


@pytest.mark.unit
def test_fetch_models_failure_502(client, monkeypatch):
    class _BrokenPage:
        def __aiter__(self):
            raise RuntimeError("connect timeout")

    class _Broken:
        def __init__(self, **kw):
            self.models = SimpleNamespace(list=lambda: _BrokenPage())

    monkeypatch.setattr(admin_router, "AsyncOpenAI", _Broken)
    r = client.post(
        "/api/admin/llm-providers/fetch-models",
        json={"base_url": "https://x.example/v1", "api_key": "sk"},
        headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 502
    assert "connect timeout" in r.json()["detail"]
