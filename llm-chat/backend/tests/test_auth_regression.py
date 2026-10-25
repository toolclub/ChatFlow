"""Authentication must not silently turn expired users into a different guest."""
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from starlette.requests import Request
from services.auth import dependencies
from routers import auth_router, tool_router


def request(headers=()):
    return Request({'type':'http','headers':[(k.lower().encode(),v.encode()) for k,v in headers]})


@pytest.mark.asyncio
@pytest.mark.parametrize('payload',[None,{'type':'refresh','sub':'u'}, {'type':'access'}])
async def test_bad_bearer_never_downgrades_to_guest(monkeypatch,payload):
    monkeypatch.setattr(dependencies,'verify_token',lambda _:payload)
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_current_user(request([('Authorization','Bearer invalid'),('X-Client-ID','guest')]))
    assert exc.value.status_code==401


@pytest.mark.asyncio
async def test_valid_user_and_guest_and_missing_identity(monkeypatch):
    monkeypatch.setattr(dependencies,'verify_token',lambda _:{'type':'access','sub':'u'})
    monkeypatch.setattr(dependencies,'get_user_by_id',AsyncMock(return_value={'id':'u','is_active':True}))
    assert (await dependencies.get_current_user(request([('Authorization','Bearer valid')])) )['id']=='u'
    assert (await dependencies.get_current_user(request([('X-Client-ID','guest')])) )['client_id']=='guest'
    for headers,status in [([],401),([('X-Client-ID','x'*37)],400)]:
        with pytest.raises(HTTPException) as exc:
            await dependencies.get_current_user(request(headers))
        assert exc.value.status_code==status


@pytest.mark.asyncio
async def test_oauth_state_consumed_atomically_even_for_empty_client(monkeypatch):
    redis=AsyncMock();redis.getdel.return_value=''
    monkeypatch.setattr(auth_router,'_get_redis',lambda:redis)
    assert await auth_router._verify_oauth_state('state')==''
    redis.getdel.assert_awaited_once_with('chatflow:oauth_state:state')
    redis.get.assert_not_called();redis.delete.assert_not_called()


@pytest.mark.asyncio
async def test_profile_update_persists_and_settings_reject_identity(monkeypatch):
    from pydantic import ValidationError
    saved={'id':'u','name':'changed'}
    update=AsyncMock(return_value=saved)
    monkeypatch.setattr(auth_router.user_store,'update_user_profile',update)
    assert await auth_router.update_me(auth_router.ProfileUpdate(name='changed'),{'id':'u'})==saved
    update.assert_awaited_once_with('u',{'name':'changed'})
    with pytest.raises(ValidationError):
        auth_router.SettingsUpdate(user_id='other')


@pytest.mark.asyncio
async def test_missing_and_corrupt_artifacts_are_http_errors(monkeypatch):
    monkeypatch.setattr(tool_router,'get_artifact_content',AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await tool_router.download_artifact(123,{'id':'u'})
    assert exc.value.status_code==404
    monkeypatch.setattr(tool_router,'get_artifact_content',AsyncMock(return_value={'conv_id':'c','binary':True,'content':'%%%%'}))
    monkeypatch.setattr(tool_router,'_check_artifact_access',AsyncMock())
    with pytest.raises(HTTPException) as exc:
        await tool_router.download_artifact(123,{'id':'u'})
    assert exc.value.status_code==422
