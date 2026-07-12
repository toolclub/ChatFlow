"""股票预测鉴权 —— 单元测试

验证 spec 铁律 #1（admin key 拦截来自后端 403，非前端 v-show）：
  T-PREDICT-AUTH-01: verify_admin_access 依赖
    - 无 X-Admin-Key header → HTTPException 403
    - 错误 key → HTTPException 403
    - 正确 key (== config.ADMIN_SECRET_KEY) → 返回 True
  T-PREDICT-AUTH-02: POST /api/admin/verify body {key}
    - 正确 key → {ok: True} (200)
    - 错误 key → 401
  T-PREDICT-AUTH-03: prediction 路由全量 admin 鉴权（无 X-Admin-Key → 403）
    - GET /records、GET /active、GET /{id}、POST /predict 均 403
  T-PREDICT-AUTH-04: 正确 key + prediction_store mock → 200（不触 DB）
  T-PREDICT-AUTH-05: X-Client-ID → client_id 取值（admin key-only 路径 D1）

隔离手段（mirror test_quant_analyze/screen）：
  - monkeypatch config.ADMIN_SECRET_KEY + admin_router.ADMIN_SECRET_KEY 到测试值
  - monkeypatch prediction_router.* 的 store 函数，避免真实 DB
  - statsmodels/arch 注入 sys.modules 桩（engine.py 延迟 import，此处为防回归保险）
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

# ── statsmodels / arch 桩 ───────────────────────────────────────────────────
# engine.py 在函数体内延迟 import statsmodels/arch（系统 python 未装），
# 正常 collection 不会触发；此处提前注入 sys.modules 桩，防止未来某处
# 变成顶层 import 时 collection 崩溃。
for _mod in (
    "statsmodels",
    "statsmodels.tsa",
    "statsmodels.tsa.arima",
    "statsmodels.tsa.arima.model",
):
    sys.modules.setdefault(_mod, types.ModuleType(_mod))
sys.modules["statsmodels.tsa.arima.model"].ARIMA = object  # type: ignore[attr-defined]
if "arch" not in sys.modules:
    _arch_stub = types.ModuleType("arch")
    _arch_stub.arch_model = lambda *a, **k: None  # type: ignore[attr-defined]
    sys.modules["arch"] = _arch_stub

import config  # noqa: E402
from routers import admin_router, prediction_router  # noqa: E402
from services.auth.dependencies import verify_admin_access  # noqa: E402

TEST_KEY = "test-admin-secret-12345"


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _set_admin_key(monkeypatch):
    """把 config + admin_router 的 admin 密钥钉到测试值。

    verify_admin_access 读 config.ADMIN_SECRET_KEY（live）；
    /api/admin/verify 读 admin_router.ADMIN_SECRET_KEY（import 时拷贝的全局）。
    两者都需 patch，否则 .env 真实值会干扰。
    """
    monkeypatch.setattr(config, "ADMIN_SECRET_KEY", TEST_KEY)
    monkeypatch.setattr(admin_router, "ADMIN_SECRET_KEY", TEST_KEY)


@pytest.fixture
def app() -> FastAPI:
    """最小 FastAPI app，挂载真实 admin_router + prediction_router。"""
    _app = FastAPI()
    _app.include_router(admin_router.router)
    _app.include_router(prediction_router.router)
    return _app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def _mock_store(monkeypatch):
    """拦截 prediction_router 内的 store 函数，避免真实 DB；记录调用参数。"""
    captured: dict = {}

    async def _list_records(client_id, user_id="", market=None, limit=50):
        captured["client_id"] = client_id
        captured["market"] = market
        captured["limit"] = limit
        return []

    async def _save_snapshot(**kwargs):
        captured["save"] = kwargs
        return None

    async def _get_active(client_id, market=None, user_id=""):
        captured["active_client_id"] = client_id
        return None

    async def _get_snapshot(snapshot_id):
        return None

    async def _start_prediction_graph(snapshot_id, state):
        captured["start"] = {"snapshot_id": snapshot_id, "status": state.get("status")}
        return None

    monkeypatch.setattr(prediction_router, "list_prediction_records", _list_records)
    monkeypatch.setattr(prediction_router, "save_prediction_snapshot", _save_snapshot)
    monkeypatch.setattr(prediction_router, "get_active_prediction", _get_active)
    monkeypatch.setattr(prediction_router, "get_prediction_snapshot", _get_snapshot)
    monkeypatch.setattr(prediction_router, "start_prediction_graph", _start_prediction_graph)
    return captured


# ── T-PREDICT-AUTH-01: verify_admin_access 依赖直接调用 ─────────────────────


@pytest.mark.unit
def test_verify_admin_access_no_header_403():
    """无 X-Admin-Key → 403。"""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(verify_admin_access(x_admin_key=None))
    assert exc.value.status_code == 403


@pytest.mark.unit
def test_verify_admin_access_wrong_key_403():
    """错误 key → 403。"""
    with pytest.raises(HTTPException) as exc:
        asyncio.run(verify_admin_access(x_admin_key="definitely-wrong"))
    assert exc.value.status_code == 403


@pytest.mark.unit
def test_verify_admin_access_correct_key_returns_true():
    """正确 key (== config.ADMIN_SECRET_KEY) → True。"""
    assert asyncio.run(verify_admin_access(x_admin_key=TEST_KEY)) is True


# ── T-PREDICT-AUTH-02: POST /api/admin/verify ───────────────────────────────


@pytest.mark.unit
def test_admin_verify_correct_key_ok(client):
    """正确 key body → {ok: True}。"""
    r = client.post("/api/admin/verify", json={"key": TEST_KEY})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


@pytest.mark.unit
def test_admin_verify_wrong_key_401(client):
    """错误 key body → 401。"""
    r = client.post("/api/admin/verify", json={"key": "nope"})
    assert r.status_code == 401


# ── T-PREDICT-AUTH-03: prediction 路由无 X-Admin-Key → 403 ──────────────────


@pytest.mark.unit
def test_prediction_records_no_key_403(client):
    r = client.get("/api/prediction/records")
    assert r.status_code == 403


@pytest.mark.unit
def test_prediction_active_no_key_403(client):
    r = client.get("/api/prediction/active")
    assert r.status_code == 403


@pytest.mark.unit
def test_prediction_read_one_no_key_403(client):
    r = client.get("/api/prediction/pred_abc")
    assert r.status_code == 403


@pytest.mark.unit
def test_prediction_predict_no_key_403(client):
    """POST /predict：带合法 body 但无 key → 403（鉴权先于业务）。"""
    r = client.post(
        "/api/prediction/predict",
        json={"symbol": "000001.SZ", "market": "cn_a", "horizon": 5},
    )
    assert r.status_code == 403


# ── T-PREDICT-AUTH-04: 正确 key + store mock → 200 ──────────────────────────


@pytest.mark.unit
def test_prediction_records_correct_key_200(client, _mock_store):
    r = client.get("/api/prediction/records", headers={"X-Admin-Key": TEST_KEY})
    assert r.status_code == 200
    assert r.json() == []


@pytest.mark.unit
def test_prediction_active_correct_key_200(client, _mock_store):
    r = client.get("/api/prediction/active", headers={"X-Admin-Key": TEST_KEY})
    assert r.status_code == 200
    assert r.json() == {"active": False}


@pytest.mark.unit
def test_prediction_predict_correct_key_200(client, _mock_store):
    r = client.post(
        "/api/prediction/predict",
        json={"symbol": "000001.SZ", "market": "cn_a", "horizon": 5},
        headers={"X-Admin-Key": TEST_KEY},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "queued"
    assert body["prediction_id"].startswith("pred_")
    assert body["criteria"]["symbol"] == "000001.SZ"
    # save_prediction_snapshot 被以 queued 状态调用
    assert _mock_store["save"]["status"] == "queued"
    assert _mock_store["save"]["symbol"] == "000001.SZ"
    # start_prediction_graph 被以后台 queued 状态调用（fire-and-forget 触发图）
    assert _mock_store["start"]["status"] == "queued"
    assert _mock_store["start"]["snapshot_id"] == body["prediction_id"]


# ── T-PREDICT-AUTH-05: X-Client-ID → client_id（admin key-only 路径 D1）─────


@pytest.mark.unit
def test_client_id_from_header(client, _mock_store):
    """有 X-Client-ID → store 收到该 client_id。"""
    client.get(
        "/api/prediction/records",
        headers={"X-Admin-Key": TEST_KEY, "X-Client-ID": "user42"},
    )
    assert _mock_store["client_id"] == "user42"


@pytest.mark.unit
def test_client_id_defaults_admin(client, _mock_store):
    """无 X-Client-ID → client_id 降级为 "admin"。"""
    client.get("/api/prediction/records", headers={"X-Admin-Key": TEST_KEY})
    assert _mock_store["client_id"] == "admin"


@pytest.mark.unit
def test_client_id_blank_falls_back_to_admin(client, _mock_store):
    """X-Client-ID 为空白 → strip 后为空 → 降级 "admin"。"""
    client.get(
        "/api/prediction/records",
        headers={"X-Admin-Key": TEST_KEY, "X-Client-ID": "   "},
    )
    assert _mock_store["client_id"] == "admin"
