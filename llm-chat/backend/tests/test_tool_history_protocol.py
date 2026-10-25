"""Interrupted tool histories still form valid call/reply pairs on the next turn."""
from langchain_core.messages import AIMessage,HumanMessage,ToolMessage
from graph.nodes.base import BaseNode


def call(id):
    return {'id':id,'name':'lookup','args':{'id':id},'type':'tool_call'}


def test_partially_completed_parallel_batch_keeps_only_matching_calls():
    history=[HumanMessage(content='lookup'),AIMessage(content='',tool_calls=[call('a'),call('b')]),ToolMessage(content='answer b',tool_call_id='b'),HumanMessage(content='continue')]
    result=BaseNode._to_openai_messages(history)
    assert [c['id'] for c in result[1]['tool_calls']]==['b']
    assert result[2]['tool_call_id']=='b'
    assert result[-1]['content']=='continue'


def test_orphan_and_duplicate_tool_responses_are_not_sent():
    history=[ToolMessage(content='orphan',tool_call_id='old'),AIMessage(content='',tool_calls=[call('a')]),ToolMessage(content='first',tool_call_id='a'),ToolMessage(content='duplicate',tool_call_id='a')]
    result=BaseNode._to_openai_messages(history)
    assert len(result)==2
    assert result[-1]['content']=='first'


def test_full_parallel_batch_preserves_reversed_reply_order_and_reasoning():
    history=[AIMessage(content='',tool_calls=[call('a'),call('b')],additional_kwargs={'reasoning_content':'plan'}),ToolMessage(content='b',tool_call_id='b'),ToolMessage(content='a',tool_call_id='a')]
    result=BaseNode._to_openai_messages(history)
    assert result[0]['reasoning_content']=='plan'
    assert [c['id'] for c in result[0]['tool_calls']]==['a','b']
    assert [m['tool_call_id'] for m in result[1:]]==['b','a']
