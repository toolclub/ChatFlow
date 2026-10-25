"""Stable artifact identity across turns, retries and metadata replay."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
@pytest.mark.unit
async def test_artifact_handler_fills_turn_identity_without_erasing_metadata_only_content():
    from graph.runner.handlers.artifact_handler import FileArtifactHandler
    ctx = SimpleNamespace(assistant_message_id="round-1")
    event = {"data": {"id": 7, "name": "report.zip", "path": "report.zip", "downloadable": True}}
    chunks = [chunk async for chunk in FileArtifactHandler().handle(event, ctx)]
    artifact = json.loads(chunks[0].removeprefix("data: ").strip())["file_artifact"]
    assert artifact["id"] == 7
    assert artifact["message_id"] == "round-1"
    assert "content" not in artifact


@pytest.mark.asyncio
@pytest.mark.unit
async def test_sandbox_write_streams_the_persisted_artifact_identity(monkeypatch):
    from tools.sandboxed.sandbox_tools import sandbox_write
    from sandbox.context import current_conv_id, current_message_id
    from sandbox.manager import sandbox_manager
    dispatch = AsyncMock()
    saved = {"id": 71, "message_id": "round-1", "name": "index.html", "path": "index.html", "language": "html"}
    persist = AsyncMock(return_value=saved)
    monkeypatch.setattr("db.artifact_store.save_artifact", persist)
    monkeypatch.setattr("langchain_core.callbacks.manager.adispatch_custom_event", dispatch)
    monkeypatch.setattr(sandbox_manager, "write_file", AsyncMock(return_value=SimpleNamespace(to_display=lambda: "written")))
    conv_token = current_conv_id.set("conv-1")
    message_token = current_message_id.set("round-1")
    try:
        await sandbox_write.ainvoke({"path": "index.html", "content": "中文"})
    finally:
        current_conv_id.reset(conv_token)
        current_message_id.reset(message_token)
    assert persist.call_args.kwargs["size"] == 6
    assert dispatch.call_args.args[1]["id"] == 71
    assert dispatch.call_args.args[1]["message_id"] == "round-1"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_real_database_preserves_per_turn_versions_and_concurrent_retries():
    import asyncio
    import os
    import uuid
    from sqlalchemy import delete, select
    from sqlalchemy.engine import make_url
    from db import database
    from db.models import ArtifactModel
    from db.artifact_store import save_artifact, get_artifact_content, get_artifact_meta_list

    url = os.getenv("CHATFLOW_TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("isolated test DB not configured")
    assert make_url(url).database.endswith("_test")
    database.init_engine(url)
    engine = database.get_engine()
    conv_id, first, second = [str(uuid.uuid4()) for _ in range(3)]
    try:
        async with engine.begin() as conn:
            await conn.run_sync(lambda sync: ArtifactModel.__table__.create(sync, checkfirst=True))
        one = await save_artifact(conv_id, "same.html", "same.html", "first", message_id=first)
        two = await save_artifact(conv_id, "same.html", "same.html", "second", message_id=second)
        assert one["id"] != two["id"]
        assert (await get_artifact_content(one["id"]))["content"] == "first"
        assert (await get_artifact_content(two["id"]))["content"] == "second"
        retry = await save_artifact(conv_id, "same.html", "same.html", "second revised", message_id=second)
        assert retry["id"] == two["id"]
        concurrent = await asyncio.gather(*(
            save_artifact(conv_id, "other.html", "other.html", str(i), message_id=second)
            for i in range(8)
        ))
        assert len({artifact["id"] for artifact in concurrent}) == 1
        history = await get_artifact_meta_list(conv_id)
        assert len(history) == 3
        assert next(artifact for artifact in history if artifact["id"] == one["id"])["message_id"] == first
        assert next(artifact for artifact in history if artifact["id"] == two["id"])["message_id"] == second
    finally:
        async with database.AsyncSessionLocal() as session:
            await session.execute(delete(ArtifactModel).where(ArtifactModel.conv_id == conv_id))
            await session.commit()
        await engine.dispose()
