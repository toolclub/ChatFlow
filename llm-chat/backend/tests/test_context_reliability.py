import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import pytest


@pytest.mark.asyncio
async def test_optional_memory_timeout_does_not_hold_up_chat(monkeypatch):
    from graph.nodes import retrieve_context_node as mod
    async def slow(*args,**kwargs):
        await asyncio.sleep(10)
    monkeypatch.setattr(mod,'LONGTERM_MEMORY_ENABLED',True)
    monkeypatch.setattr(mod,'CONTEXT_LOOKUP_TIMEOUT',0.01)
    monkeypatch.setattr(mod.memory_store,'get',lambda _:None)
    monkeypatch.setattr('rag.retriever.search_memories',slow)
    result=await asyncio.wait_for(mod.RetrieveContextNode([]).execute({'conv_id':'c','user_message':'question'}),0.5)
    assert result['long_term_memories']==[]
    assert result['messages'][-1].content=='question'


@pytest.mark.asyncio
async def test_cache_skips_followups_and_file_context(monkeypatch):
    from graph.nodes.cache_node import SemanticCacheNode
    from memory import store
    from cache import factory
    lookup=AsyncMock(side_effect=AssertionError('must not reuse context-free answer'))
    monkeypatch.setattr(factory,'get_cache',lambda:SimpleNamespace(lookup=lookup))
    monkeypatch.setattr(store,'get',lambda _:SimpleNamespace(messages=[SimpleNamespace(role='user')]))
    node=SemanticCacheNode()
    assert not (await node.execute({'conv_id':'c','user_message':'continue'}))['cache_hit']
    monkeypatch.setattr(store,'get',lambda _:None)
    for fields in ({'file_ids':[1]},{'context_refs':[{'id':'p'}]}):
        assert not (await node.execute({'conv_id':'c','user_message':'analyze',**fields}))['cache_hit']
    lookup.assert_not_called()


@pytest.mark.asyncio
async def test_cache_transport_failure_is_a_miss(monkeypatch):
    from graph.nodes.cache_node import SemanticCacheNode
    monkeypatch.setattr('cache.factory.get_cache',lambda:SimpleNamespace(lookup=AsyncMock(side_effect=ConnectionError('offline'))))
    monkeypatch.setattr('memory.store.get',lambda _:None)
    assert not (await SemanticCacheNode().execute({'conv_id':'c','user_message':'question'}))['cache_hit']


@pytest.mark.asyncio
async def test_snapshot_reference_authorization_and_prediction_projection(monkeypatch):
    from services import context_reference_service as mod
    snap=SimpleNamespace(id='q',user_id='',client_id='owner',criteria={},analysis='answer',risk_notes=[],rows=[])
    monkeypatch.setattr(mod,'get_quant_snapshot',AsyncMock(return_value=snap))
    with pytest.raises(ValueError,match='无权'):
        await mod.load_context_reference({'type':'quant_snapshot','id':'q'},client_id='other')
    assert 'answer' in await mod.load_context_reference({'type':'quant_snapshot','id':'q'},client_id='owner')
    row=SimpleNamespace(user_id='user',client_id='',_mapping={'id':'p','analysis':'prediction result','user_id':'user'})
    session=AsyncMock();result=MagicMock();result.first.return_value=row;session.execute.return_value=result
    session.__aenter__.return_value=session
    monkeypatch.setattr(mod,'AsyncSessionLocal',lambda:session)
    data=await mod.load_context_reference({'type':'prediction_snapshot','id':'p'},user_id='user')
    assert 'prediction result' in data and 'user_id' not in data
    statement=session.execute.call_args.args[0]
    assert 'news_digest' not in str(statement)
