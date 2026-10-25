"""Opt-in real PostgreSQL/Redis regression using an isolated *_test database.

CHATFLOW_TEST_DATABASE_URL and CHATFLOW_TEST_REDIS_URL must point to disposable
services; normal unit runs skip this file. No production settings are used.
"""
import asyncio
import json
import os
import time
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select, text

DATABASE_URL = os.getenv('CHATFLOW_TEST_DATABASE_URL', '')
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not DATABASE_URL, reason='isolated test DB not configured')]


@pytest.mark.asyncio
async def test_real_database_round_recovery_and_prediction_ownership(monkeypatch):
    from sqlalchemy.engine import make_url
    assert make_url(DATABASE_URL).database.endswith('_test'), 'Integration tests require a *_test database'
    from db import database
    from db.models import Base, ConversationModel, MessageModel, EventLogModel, PredictionSnapshotModel
    from db.migrate import run_migrations
    from db.event_store import append_events_batch, get_events_since
    from memory import store
    from graph.runner.stream import resume_stream
    from graph.runner.utils import sse
    from fsm.sse_events import SSEEventType
    from db.prediction_store import save_prediction_snapshot, update_prediction_status
    from graph.prediction import runtime
    import redis.asyncio as redis

    database.init_engine(DATABASE_URL)
    engine = database.get_engine()
    monkeypatch.setattr(store, '_notify_cache_invalidation', AsyncMock())
    redis_client = redis.from_url(os.environ['CHATFLOW_TEST_REDIS_URL'])
    monkeypatch.setattr('db.redis_state._get_redis', lambda: redis_client)
    conv_id, round_id, old_round, pred_id = [str(uuid.uuid4()) for _ in range(4)]
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await run_migrations(conn)
            await run_migrations(conn)  # Existing deployments must migrate idempotently.
        now = time.time()
        async with database.AsyncSessionLocal() as session:
            session.add(ConversationModel(id=conv_id, client_id='test-client', status='active', created_at=now, updated_at=now))
            await session.commit()
        # Concurrent requests, even on different DB connections, admit one turn.
        claims = await asyncio.gather(*(store.try_start_stream(conv_id) for _ in range(8)))
        assert sum(claims) == 1
        observed = await store.db_get_conversation(conv_id)
        assert observed['client_id'] == 'test-client'
        async with database.AsyncSessionLocal() as session:
            session.add(MessageModel(conv_id=conv_id, message_id=round_id, role='assistant', stream_completed=False,
                                     stream_buffer='saved partial', created_at=now))
            await session.commit()
        # A stale observer may never close a newly claimed turn.
        assert not await store.recover_interrupted_stream(conv_id, expected_updated_at=observed['updated_at'] - 1)
        assert await store.recover_interrupted_stream(conv_id, expected_updated_at=observed['updated_at'])
        recovered = await store.db_get_conversation(conv_id)
        assert recovered['status'] == 'error'
        assert recovered['messages'][-1]['content'] == 'saved partial'
        assert recovered['messages'][-1]['stream_completed'] is True
        async with database.AsyncSessionLocal() as session:
            await session.execute(delete(EventLogModel).where(EventLogModel.conv_id == conv_id))
            await session.commit()
        # More than one replay page, with old-round terminal events interleaved.
        entries = [{'conv_id': conv_id, 'message_id': old_round, 'event_type': SSEEventType.DONE.value,
                    'sse_string': sse({SSEEventType.DONE.value: True})}]
        entries += [{'conv_id': conv_id, 'message_id': round_id, 'event_type': SSEEventType.CONTENT.value,
                     'sse_string': sse({SSEEventType.CONTENT.value: str(i)})} for i in range(1001)]
        entries.append({'conv_id': conv_id, 'message_id': round_id, 'event_type': SSEEventType.DONE.value,
                        'sse_string': sse({SSEEventType.DONE.value: True})})
        ids = await append_events_batch(entries)
        assert len(await get_events_since(conv_id, 0, message_id=round_id)) == 500
        chunks = [chunk async for chunk in resume_stream(conv_id, 0, round_id)]
        payloads = [json.loads(next(line[5:] for line in chunk.splitlines() if line.startswith('data:'))) for chunk in chunks]
        assert len(payloads) == 1002
        assert payloads[-1]['done'] is True
        assert [p['content'] for p in payloads[:-1]] == [str(i) for i in range(1001)]
        assert [p['id'] for p in payloads] == ids[1:]
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT 1 FROM pg_indexes WHERE indexname='ix_eventlog_conv_message_id'")) == 1
        # Real Redis SET NX + token comparison, not a mock of the lock algorithm.
        tokens = await asyncio.gather(*(runtime.acquire(pred_id) for _ in range(8)))
        owners = [token for token in tokens if token]
        assert len(owners) == 1
        assert not await runtime.release(pred_id, 'stale-owner')
        assert await runtime.renew(pred_id, owners[0])
        assert await runtime.request_cancel(pred_id, 'paused')
        assert await runtime.cancellation(pred_id, owners[0]) == 'paused'
        assert await runtime.release(pred_id, owners[0])
        # Durable FSM prevents a late inference result overwriting a pause.
        await save_prediction_snapshot(pred_id, 'test-client', {}, market='cn_a', symbol='000001.SZ')
        assert await update_prediction_status(pred_id, 'news_searching')
        assert await update_prediction_status(pred_id, 'paused')
        assert not await update_prediction_status(pred_id, 'done')
        assert not await update_prediction_status(pred_id, 'inferring')
        assert await update_prediction_status(pred_id, 'inferring', allow_resume=True)
        assert await update_prediction_status(pred_id, 'done')
        assert not await update_prediction_status(pred_id, 'queued', allow_resume=True)
    finally:
        async with database.AsyncSessionLocal() as session:
            for model in (EventLogModel, MessageModel):
                await session.execute(delete(model).where(model.conv_id == conv_id))
            await session.execute(delete(ConversationModel).where(ConversationModel.id == conv_id))
            await session.execute(delete(PredictionSnapshotModel).where(PredictionSnapshotModel.id == pred_id))
            await session.commit()
        await redis_client.delete('chatflow:prediction:owner:' + pred_id, 'chatflow:prediction:owner:' + pred_id + ':cancel')
        await redis_client.aclose()
        await engine.dispose()
