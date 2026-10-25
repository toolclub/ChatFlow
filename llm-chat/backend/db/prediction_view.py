"""Compact, consistent prediction recovery views; raw source text stays in JSONB.

A recovery cursor is valid only after every visible event has been projected.
Read snapshot columns and event projections in one REPEATABLE READ transaction:
node dispatch and snapshot updates use separate transactions and either may win.
Never use a separately queried latest event ID as a snapshot checkpoint.
"""
from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select, text, literal_column

from db.database import AsyncSessionLocal
from db.models import PredictionSnapshotModel
from fsm.sse_events import SSEEventType as E


def compact_news_sql(expression: str) -> str:
    """SQL expressions are internal constants, never caller-provided strings."""
    return f"""(({expression}) - 'results') || jsonb_build_object('results',
      COALESCE((SELECT jsonb_agg(
        (source - ARRAY['full_content','raw_content','content','snippet']) ||
        jsonb_build_object('snippet', left(COALESCE(source->>'snippet',''), 500),
                          'source_index', ordinal - 1, 'body_available', true)
        ORDER BY ordinal)
        FROM jsonb_array_elements(COALESCE(({expression})->'results','[]'::jsonb))
          WITH ORDINALITY AS sources(source, ordinal)), '[]'::jsonb))"""


def compact_digest_sql(expression: str) -> str:
    return f"""COALESCE((SELECT jsonb_agg({compact_news_sql('dimension')} ORDER BY ordinal)
      FROM jsonb_array_elements(COALESCE({expression}, '[]'::jsonb))
        WITH ORDINALITY AS dims(dimension, ordinal)), '[]'::jsonb)"""


COMPACT_NEWS = compact_digest_sql('news_digest')
# DONE also embeds the entire digest, often larger than every other event
# combined. Compact nested digests as well as individual NEWS_ITEM events.
COMPACT_EVENT = f"""CASE WHEN event_type = :news THEN {compact_news_sql('event_data')}
  WHEN event_data ? 'news_digest' THEN (event_data - 'news_digest') ||
    jsonb_build_object('news_digest', {compact_digest_sql("event_data->'news_digest'")})
  ELSE event_data END"""


def normalized_events_sql(predicate: str) -> str:
    # Historical prediction rows may use `type` or only sse_string. Modern
    # rows read event_data directly; only legacy rows parse the JSON string.
    # Malformed legacy JSON fails the view instead of acknowledging lost data.
    return f"""WITH raw_events AS MATERIALIZED (
      SELECT id, event_type, CASE
        WHEN jsonb_typeof(event_data) = 'object' AND event_data <> '{{}}'::jsonb THEN event_data
        WHEN left(ltrim(sse_string), 1) = '{{' THEN sse_string::jsonb
        WHEN ltrim(sse_string) LIKE 'data:%' THEN substring(ltrim(sse_string) from 6)::jsonb
        ELSE event_data END AS payload
      FROM event_log WHERE conv_id = :pid AND message_id = :pid {predicate}
    ), normalized AS MATERIALIZED (
      SELECT id, event_type, CASE WHEN payload ? 'event' THEN payload ELSE payload || jsonb_build_object(
        'event', COALESCE(payload->>'event', payload->>'type', event_type),
        'text', COALESCE(payload->>'text', payload->>'delta', payload->>event_type)
      ) END AS event_data FROM raw_events
    ) """


def _merge(rows: list, item: dict, key) -> None:
    signature = key(item)
    for index, old in enumerate(rows):
        if key(old) == signature:
            rows[index] = {**old, **item}
            return
    rows.append(item)


def project_events(snapshot: dict, events: list[dict]) -> dict:
    """Fill snapshot/event commit gaps. Status always comes from the DB FSM.

    Nodes dispatch immutable dimension/formula/candle results before their
    snapshot write; the latest event for the same business key therefore wins.
    Persisted columns seed keys without an event (legacy logs or failed event
    writes). Final DONE carries the complete analysis/forecast/digests and wins
    over intermediate events. Snapshot-only model_params/historical_bars are
    retained; forecast parameters are available inside the projected forecast.
    Progress/status terminal cleanup is authoritative in the snapshot, never
    inferred from text or replaying a historical PAUSED/DONE event.
    """
    for ev in events:
        kind = ev.get('event')
        if kind == E.DONE.value:
            for dim in ev.get('news_digest') or []:
                _merge(snapshot['news_digest'], dim, lambda x: x.get('dimension'))
            for dim in ev.get('filing_digest') or []:
                _merge(snapshot['filing_digest'], dim, lambda x: x.get('dimension'))
            for field in ('analysis', 'risk_notes', 'thinking_segments', 'news_sentiment', 'event_score'):
                if field in ev:
                    snapshot[field] = ev[field]
            if ev.get('forecast'):
                snapshot['_event_forecast'] = ev['forecast']
        elif kind == E.ERROR.value and snapshot['status'] == 'failed':
            snapshot['error_message'] = ev.get('message') or snapshot.get('error_message', '')
        elif kind == E.NEWS_ITEM.value:
            _merge(snapshot['news_digest'], ev, lambda x: x.get('dimension'))
        elif kind == E.DATA_ITEM.value:
            dim = ev.get('result') or ev
            if not dim.get('dimension'):
                continue
            if 'item' in dim and 'items' not in dim:
                # Individual filing cards precede their complete dimension.
                existing = next((d for d in snapshot['filing_digest'] if d.get('dimension') == dim['dimension']), None)
                if existing is None:
                    existing = {'dimension': dim['dimension'], 'items': []}
                    snapshot['filing_digest'].append(existing)
                _merge(existing.setdefault('items', []), dim['item'], lambda x: (x.get('url'), x.get('title'), x.get('ann_date')))
            else:
                _merge(snapshot['filing_digest'], dim, lambda x: x.get('dimension'))
        elif kind == E.FORMULA_STEP.value:
            step = dict(ev.get('step') or ev)
            if step.get('stage') == step.get('key'):
                step.pop('stage', None)  # Wire/DB dedupe alias, not a second formula field.
            _merge(snapshot['formula_steps'], step, lambda x: x.get('key') or x.get('stage'))
        elif kind == E.FORECAST_DAY.value:
            _merge(snapshot['forecast_candles'], ev.get('candle') or ev, lambda x: x.get('day'))
        elif kind == E.EVENT_MARKER.value:
            marker = ev.get('marker') or ev
            signature = lambda x: (x.get('date'), x.get('event_type'), x.get('title'))
            # Chart markers are append-only. Preserve the stored aggregate's
            # scores/dimensions when a historical display event has the same key.
            if not any(signature(old) == signature(marker) for old in snapshot['event_markers']):
                snapshot['event_markers'].append(marker)
        elif kind == E.TOOL_TRACE.value:
            trace = ev.get('trace') or ev
            _merge(snapshot['tool_traces'], trace, lambda x: (x.get('dimension'), x.get('tool'), x.get('phase')))
        elif kind == E.FORECAST.value and ev.get('forecast'):
            snapshot['_event_forecast'] = ev['forecast']
        elif kind == E.INFLECTION.value:
            snapshot['inflection_points'] = ev.get('points') or []
        elif kind == E.PREDICTION_PROGRESS.value and ev.get('phase'):
            # Terminal snapshots deliberately clear progress; stale events must
            # not resurrect a finished progress indicator.
            if snapshot['status'] in ('queued', 'news_searching', 'inferring'):
                snapshot['progress'] = {k: v for k, v in ev.items() if k not in ('event', 'id')}
    return snapshot


async def get_prediction_view(snapshot_id: str):
    model = PredictionSnapshotModel
    columns = [c for c in model.__table__.columns if c.name != 'news_digest']
    columns.append(literal_column(COMPACT_NEWS).label('news_digest'))
    async with AsyncSessionLocal() as session:
        await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
        # JSON lateral projections have high estimated costs but tiny row counts;
        # JIT compilation otherwise costs seconds for a millisecond-scale read.
        await session.execute(text('SET LOCAL jit = off'))
        row = (await session.execute(select(*columns).where(model.id == snapshot_id))).mappings().first()
        if row is None:
            return None
        # Postgres aggregates the token stream without transferring thousands
        # of objects (and strips news bodies before asyncpg JSON decoding).
        aggregate = (await session.execute(text(normalized_events_sql('') + f"""
          SELECT COALESCE(max(id), 0) AS event_cursor,
            string_agg(event_data->>'text', '' ORDER BY id)
              FILTER (WHERE event_type IN (:thinking, :content)) AS stream_text,
            COALESCE(jsonb_agg({COMPACT_EVENT} ORDER BY id)
              FILTER (WHERE event_type NOT IN (:thinking, :content)), '[]'::jsonb) AS events
          FROM normalized
        """), {'pid': snapshot_id, 'news': E.NEWS_ITEM.value,
                 'thinking': E.THINKING.value, 'content': E.CONTENT.value})).mappings().one()
        view = project_events(dict(row), aggregate['events'])
        view['event_cursor'] = aggregate['event_cursor']
        view['stream_text'] = aggregate['stream_text']
        return SimpleNamespace(**view)


async def get_compact_prediction_events(snapshot_id: str, after_id: int, limit: int = 500):
    async with AsyncSessionLocal() as session:
        await session.execute(text('SET LOCAL jit = off'))
        rows = (await session.execute(text(normalized_events_sql('AND id > :after ORDER BY id LIMIT :limit') + f"""
          SELECT id, event_type, {COMPACT_EVENT} AS event_data
          FROM normalized ORDER BY id
        """), {'pid': snapshot_id, 'after': after_id, 'limit': limit, 'news': E.NEWS_ITEM.value})).mappings().all()
        return [dict(row) for row in rows]


async def get_prediction_source(snapshot_id: str, dimension: str, source_index: int, url: str):
    """Fetch one original source. Event fallback covers dispatch-before-snapshot."""
    async with AsyncSessionLocal() as session:
        await session.execute(text('SET LOCAL jit = off'))
        row = await session.scalar(text("""
          SELECT source FROM prediction_snapshots,
            LATERAL jsonb_array_elements(news_digest) AS dims(d),
            LATERAL jsonb_array_elements(d->'results') WITH ORDINALITY AS sources(source, ordinal)
          WHERE id = :pid AND d->>'dimension' = :dimension
            AND ordinal = :ordinal AND COALESCE(source->>'url', '') = :url LIMIT 1
        """), {'pid': snapshot_id, 'dimension': dimension, 'ordinal': source_index + 1, 'url': url})
        if row is not None:
            return row
        return await session.scalar(text("""
          SELECT event_data->'results'->CAST(:idx AS integer) FROM event_log
          WHERE conv_id = :pid AND message_id = :pid AND event_type = :news
            AND event_data->>'dimension' = :dimension
            AND COALESCE(event_data->'results'->CAST(:idx AS integer)->>'url', '') = :url
          ORDER BY id DESC LIMIT 1
        """), {'pid': snapshot_id, 'dimension': dimension, 'idx': source_index,
                 'url': url, 'news': E.NEWS_ITEM.value})
