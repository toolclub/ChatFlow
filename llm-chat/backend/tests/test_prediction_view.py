"""Recovery views cover every skipped event without fetching news bodies."""
import json
import os
import uuid

import pytest
from sqlalchemy import delete, select, update

from db.prediction_view import project_events
from fsm.sse_events import SSEEventType as E


def base_view():
    return {'news_digest': [], 'filing_digest': [], 'formula_steps': [],
            'forecast_candles': [], 'event_markers': [], 'tool_traces': [],
            'status': 'inferring', 'progress': {}}


def test_projection_covers_every_structured_event_without_changing_fsm_status():
    snapshot = base_view()
    card = {'title': '公告', 'url': 'https://example.test/1'}
    events = [
        {'event': E.NEWS_ITEM.value, 'dimension': '宏观', 'results': []},
        {'event': E.DATA_ITEM.value, 'dimension': '公告', 'item': card},
        {'event': E.DATA_ITEM.value, 'dimension': '公告', 'items': [card], 'score': -0.2},
        {'event': E.FORMULA_STEP.value, 'step': {'key': 'a', 'result': 1}},
        {'event': E.FORMULA_STEP.value, 'key': 'a', 'result': 2},
        {'event': E.FORECAST_DAY.value, 'candle': {'day': 1, 'c': 3}},
        {'event': E.EVENT_MARKER.value, 'marker': {'date': '2026-10-01', 'title': '事件'}},
        {'event': E.TOOL_TRACE.value, 'tool': 'lookup', 'dimension': '公告', 'phase': 'start'},
        {'event': E.TOOL_TRACE.value, 'trace': {'tool': 'lookup', 'dimension': '公告', 'phase': 'end'}},
        {'event': E.FORECAST.value, 'forecast': {'point_forecast': [3]}},
        {'event': E.INFLECTION.value, 'points': [{'date': '2026-10-01', 'price': 3}]},
        {'event': E.PREDICTION_PROGRESS.value, 'phase': 'inferring', 'index': 2},
        {'event': E.PAUSED.value}, {'event': E.STOPPED.value}, {'event': E.ERROR.value}, {'event': E.DONE.value},
    ]
    result = project_events(snapshot, events)
    assert result['status'] == 'inferring'
    assert result['news_digest'][0]['dimension'] == '宏观'
    assert result['filing_digest'][0]['items'] == [card]
    assert result['formula_steps'][0]['result'] == 2
    assert len(result['formula_steps']) == 1
    assert result['forecast_candles'][0]['c'] == 3
    assert len(result['tool_traces']) == 2
    assert len(result['event_markers']) == 1
    assert result['_event_forecast'] == {'point_forecast': [3]}
    assert result['inflection_points'][0]['price'] == 3
    assert result['progress']['index'] == 2
    done = {**base_view(), 'status': 'done'}
    assert project_events(done, events)['progress'] == {}


DATABASE_URL = os.getenv('CHATFLOW_TEST_DATABASE_URL', '')


@pytest.mark.integration
@pytest.mark.skipif(not DATABASE_URL, reason='isolated test DB not configured')
@pytest.mark.asyncio
async def test_real_compact_checkpoint_covers_commit_gaps_and_preserves_originals(monkeypatch):
    from sqlalchemy.engine import make_url
    from db import database
    from db import prediction_view
    from db.models import EventLogModel, PredictionSnapshotModel
    from db.prediction_store import save_prediction_snapshot, list_prediction_records, get_active_prediction
    from db.event_store import append_event, append_events_batch
    from routers.prediction_router import read_prediction

    assert make_url(DATABASE_URL).database.endswith('_test')
    database.init_engine(DATABASE_URL)
    pid = f'view_{uuid.uuid4().hex[:20]}'
    raw = '完整原文不能截断。' * 100_000
    source = {'title': 'Original', 'url': 'https://example.test/news', 'snippet': 'S' * 2000, 'full_content': raw}
    news = {'event': E.NEWS_ITEM.value, 'dimension': '宏观', 'results': [source]}
    real_factory = database.AsyncSessionLocal
    try:
        await save_prediction_snapshot(pid, pid, {}, status='news_searching')
        async with real_factory() as session:
            await session.execute(update(PredictionSnapshotModel).where(PredictionSnapshotModel.id == pid).values(
                news_digest=[news], thinking_segments=[{'content': 'STALE SNAPSHOT MUST NOT DUPLICATE TOKENS'}],
            ))
            await session.commit()
        # The second dimension event is committed before its node snapshot write.
        pending_news = {**news, 'dimension': '行业'}
        entries = [
            {'conv_id': pid, 'message_id': pid, 'event_type': E.THINKING.value,
             'event_data': {'event': E.THINKING.value, 'text': f'{i},'}, 'sse_string': ''}
            for i in range(1001)
        ]
        entries[0]['event_data'] = {'type': E.THINKING.value, 'text': '0,'}
        entries[1]['event_data'] = {}
        entries[1]['sse_string'] = 'data: ' + json.dumps({'event': E.THINKING.value, 'text': '1,'}) + '\n\n'
        entries[2]['event_data'] = {'text': '2,'}  # Missing event/type: use DB event_type.
        entries += [{'conv_id': pid, 'message_id': pid, 'event_type': E.NEWS_ITEM.value,
                     'event_data': pending_news, 'sse_string': json.dumps(pending_news)}]
        entries.append({'conv_id': pid, 'message_id': pid, 'event_type': E.DONE.value,
                        'event_data': {'event': E.DONE.value, 'news_digest': [pending_news], 'analysis': 'Final analysis'},
                        'sse_string': ''})
        ids = await append_events_batch(entries)
        injected = False
        concurrent_id = None

        class RacingSession:
            async def __aenter__(self):
                self.session = real_factory()
                await self.session.__aenter__()
                return self

            async def __aexit__(self, *args):
                return await self.session.__aexit__(*args)

            async def execute(self, stmt, *args, **kwargs):
                nonlocal injected, concurrent_id
                result = await self.session.execute(stmt, *args, **kwargs)
                if not injected and str(stmt).startswith('SELECT'):
                    injected = True
                    # This commit happens AFTER reading the snapshot but BEFORE
                    # reading event_log. READ COMMITTED/latestid would lose it.
                    concurrent_id = await append_event(pid, E.CONTENT.value,
                        {'event': E.CONTENT.value, 'text': 'AFTER_CHECKPOINT'}, '', message_id=pid)
                return result

        monkeypatch.setattr(prediction_view, 'AsyncSessionLocal', RacingSession)
        view = await prediction_view.get_prediction_view(pid)
        assert injected
        assert view.event_cursor == ids[-1]
        assert view.stream_text == ''.join(f'{i},' for i in range(1001))
        assert len(view.news_digest) == 2  # Dispatch-before-snapshot is included.
        assert all('full_content' not in item for dim in view.news_digest for item in dim['results'])
        assert all(len(dim['results'][0]['snippet']) == 500 for dim in view.news_digest)
        assert len(json.dumps(vars(view))) < len(raw) / 50
        monkeypatch.setattr(prediction_view, 'AsyncSessionLocal', real_factory)
        tail = await prediction_view.get_compact_prediction_events(pid, view.event_cursor)
        assert [r['id'] for r in tail] == [concurrent_id]
        assert tail[0]['event_data']['text'] == 'AFTER_CHECKPOINT'
        full = await prediction_view.get_prediction_source(pid, '宏观', 0, source['url'])
        assert full == source  # Snapshot source, every byte preserved.
        full_gap = await prediction_view.get_prediction_source(pid, '行业', 0, source['url'])
        assert full_gap == source  # Event-before-snapshot source is readable too.
        assert await prediction_view.get_prediction_source(pid, '宏观', 0, 'wrong-url') is None
        compact_events = await prediction_view.get_compact_prediction_events(pid, ids[-3])
        assert len(json.dumps(compact_events)) < 3000
        assert 'full_content' not in compact_events[1]['event_data']['news_digest'][0]['results'][0]
        assert compact_events[0]['event_data']['results'][0]['source_index'] == 0
        response = await read_prediction(pid, admin=None, compact=True)
        assert response['event_cursor'] == concurrent_id
        assert response['stream_text'].endswith('AFTER_CHECKPOINT')
        assert response['id'] == pid
        # List/active SQL must not load megabytes only to discard them in router.
        record = (await list_prediction_records(pid))[0]
        active = await get_active_prediction(pid)
        assert 'news_digest' not in record.__dict__
        assert 'news_digest' not in active.__dict__
    finally:
        monkeypatch.setattr(prediction_view, 'AsyncSessionLocal', real_factory)
        async with real_factory() as session:
            await session.execute(delete(EventLogModel).where(EventLogModel.conv_id == pid))
            await session.execute(delete(PredictionSnapshotModel).where(PredictionSnapshotModel.id == pid))
            await session.commit()
        await database.get_engine().dispose()


def test_historical_markers_do_not_overwrite_stored_scores():
    snap = base_view()
    marker = {'date': '2026-10-01', 'title': 'Risk', 'event_type': 'negative', 'score': -1}
    snap['event_markers'] = [marker]
    result = project_events(snap, [{'event': E.EVENT_MARKER.value, **marker, 'score': -0.5}])
    assert result['event_markers'] == [marker]


@pytest.mark.asyncio
async def test_rejected_pause_does_not_cancel_a_completed_prediction(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    import graph.prediction_agent as pa
    monkeypatch.setattr(pa, 'get_prediction_snapshot', AsyncMock(return_value=SimpleNamespace(status='inferring')))
    monkeypatch.setattr(pa, '_transition_status', AsyncMock(return_value=False))
    monkeypatch.setattr(pa, 'get_prediction_runtime_snapshot', AsyncMock(return_value=SimpleNamespace(status='done')))
    cancel = AsyncMock()
    monkeypatch.setattr(pa.prediction_runtime, 'request_cancel', cancel)
    result = await pa.pause_prediction('completed-between-read-and-CAS')
    assert result['ok'] is False
    assert result['status'] == 'done'
    cancel.assert_not_awaited()
