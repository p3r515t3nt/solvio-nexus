"""Native Google clients after real temporary Vault rotation, synthetic HTTP only."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime
import os
import sys
from unittest.mock import patch

sys.path.insert(0,os.path.join(os.path.dirname(__file__),'..','src'))
sys.path.insert(0,os.path.dirname(__file__))
from _guard import enforce_assertions,require,require_equal
enforce_assertions()
from test_agent_action_services import native_fixture,Session,Reply
from solvio.secret_vault import admin,context as SC
from solvio.capabilities.policy import OriginClass
from solvio.capabilities.calendar import CalendarAuthError
from solvio.integrations.gmail import GmailAuthError

TOKEN_URL='https://oauth2.googleapis.com/token'


@contextmanager
def tracked():
    with native_fixture() as n:
        n.used_tokens=[]; n.refresh_count=0
        class TrackedReply(Reply):
            def __init__(self,*args,**kwargs):
                self.request_headers=kwargs.get('headers') or {}
                super().__init__(*args,**kwargs)
            async def __aenter__(self):
                await super().__aenter__()
                if self.url==TOKEN_URL and self.status==200:
                    n.refresh_count+=1
                    self.payload['access_token']='synthetic-new-access-'+str(n.refresh_count)
                elif self.url!=TOKEN_URL:
                    n.used_tokens.append(self.request_headers.get('Authorization'))
                return self
        class TrackedSession(Session):
            def request(self,method,url,**kwargs):
                return TrackedReply(self.transport,method,url,**kwargs)
        with patch('aiohttp.ClientSession',lambda **kwargs:TrackedSession(n.transport,**kwargs)):
            yield n


def context(kind):
    return SC.bound(SC.UseContext(origin=OriginClass.BACKGROUND_AUTOMATION,
        capability='calendar_list_events' if kind=='calendar' else 'gmail_search'))


async def read(client,kind):
    return await client._request('GET','/calendars/primary/events' if kind=='calendar' else '/profile')


async def t_rotated_refresh_or_client_secret_never_reuses_old_access_token():
    for kind in ('calendar','gmail'):
        for refname in ('REFRESH_TOKEN_REF','CLIENT_SECRET_REF'):
            with tracked() as n,context(kind):
                client=getattr(n,kind)
                await read(client,kind)
                require_equal(n.used_tokens,['Bearer synthetic-native-access'])
                admin.replace_value(secret_ref=getattr(client,refname),
                    plaintext=b'synthetic-rotated-google-value',store=n.broker.store)
                await read(client,kind)
                require_equal(n.refresh_count,1)
                require_equal(n.used_tokens[-1],'Bearer synthetic-new-access-1')
                await read(client,kind)
                require_equal(n.refresh_count,1,'matching credential cache should still work')


async def t_failed_refresh_after_rotation_never_falls_back_to_old_access():
    for kind in ('calendar','gmail'):
        with tracked() as n,context(kind):
            client=getattr(n,kind)
            admin.replace_value(secret_ref=client.REFRESH_TOKEN_REF,
                plaintext=b'synthetic-rotated-google-value',store=n.broker.store)
            n.transport.auth_error='invalid_grant'
            try: await read(client,kind)
            except (CalendarAuthError,GmailAuthError): pass
            else: raise AssertionError('old access token survived a failed credential refresh')
            require_equal(n.used_tokens,[])
            require_equal(client._access_token,'')


async def t_rotation_during_token_reply_cannot_authorize_native_request():
    for kind in ('calendar','gmail'):
        with tracked() as n,context(kind):
            client=getattr(n,kind); client._access_token=''; client._expires_at=0
            async def rotate(method,url):
                if url==TOKEN_URL:
                    admin.replace_value(secret_ref=client.REFRESH_TOKEN_REF,
                        plaintext=b'synthetic-mid-flight-google-value',store=n.broker.store)
            n.transport.before_response=rotate
            try: await read(client,kind)
            except (CalendarAuthError,GmailAuthError): pass
            else: raise AssertionError('token minted across account rotation reached native API')
            require_equal(n.used_tokens,[])
            require_equal(client._access_token,'')


if __name__=='__main__':
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
