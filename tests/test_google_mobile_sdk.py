"""Official OAuth and JWT verifier against synthetic HTTP, never Google."""
import base64
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import parse_qs
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
sys.path.insert(0,str(Path(__file__).resolve().parent))
from _guard import enforce_assertions, require, require_equal
enforce_assertions()
from solvio.secret_vault import google_mobile_exchange as W


def probe(case):
    if importlib.util.find_spec('google_auth_oauthlib') is None:
        require(os.environ.get('SOLVIO_MOBILE_SDK_CHILD') != '1')
        executable=os.environ.get('SOLVIO_GOOGLE_OAUTH_TEST_PYTHON','')
        require(Path(executable).is_file(),'isolated official OAuth runtime required')
        env=dict(os.environ, SOLVIO_MOBILE_SDK_CHILD='1', PYTHONDONTWRITEBYTECODE='1')
        env.pop('PYTHONPATH',None);env.pop('PYTHONHOME',None)
        out=subprocess.run([executable,__file__,'--sdk-case',case],capture_output=True,timeout=30,env=env)
        require_equal(out.returncode,0,'synthetic official SDK probe failed: '+out.stderr.decode()[-1800:])
        require_equal(out.stdout.decode().strip(),'SDK-MOBILE-PASS:'+case)
        return
    import requests
    from requests.adapters import BaseAdapter
    from google.auth import jwt, crypt
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization as S
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    pem=key.private_bytes(S.Encoding.PEM,S.PrivateFormat.PKCS8,S.NoEncryption())
    public=key.public_key().public_bytes(S.Encoding.PEM,S.PublicFormat.SubjectPublicKeyInfo).decode()
    signer=crypt.RSASigner.from_string(pem,key_id='synthetic-key')
    claims=dict(iss='https://accounts.google.com',aud=W.SERVER_CLIENT,sub='synthetic-id',
                email='synthetic@example.invalid',email_verified=True,iat=int(time.time()),exp=int(time.time())+3600)
    if case=='account': claims['sub']='someone-else'
    if case=='audience': claims['aud']='different-client'
    if case=='expired': claims['iat']-=7200;claims['exp']-=7200
    if case=='unverified': claims['email_verified']=False
    token=jwt.encode(signer,claims).decode()
    if case=='signature': token=token[:-8]+'xxxxxxxx'
    scopes=list(W.SCOPES)+['openid','https://www.googleapis.com/auth/userinfo.email','https://www.googleapis.com/auth/userinfo.profile']
    if case=='scope': scopes=scopes[1:]
    if case=='broader': scopes+=['https://www.googleapis.com/auth/drive']
    destinations=[]
    class Port(BaseAdapter):
        def send(self,request,**kwargs):
            destinations.append(request.url)
            response=requests.Response();response.request=request;response.status_code=200
            response.headers['Content-Type']='application/json'
            if request.url==W.TOKEN_URI:
                require_equal(request.method,'POST')
                require_equal(parse_qs(request.body)['code'],['synthetic-code'])
                require_equal(request.headers['Authorization'],'Basic '+base64.b64encode((W.SERVER_CLIENT+':synthetic-secret').encode()).decode())
                data=dict(access_token='synthetic-access',refresh_token='synthetic-refresh',id_token=token,
                          scope=' '.join(scopes),token_type='Bearer',expires_in=3600)
                if case=='missing_refresh': data.pop('refresh_token')
                if case=='redirect':
                    response.status_code=307;response.headers['Location']='https://foreign.invalid/token'
            elif request.url=='https://www.googleapis.com/oauth2/v1/certs':
                require('Authorization' not in request.headers)
                data={'synthetic-key': public}
            else: raise AssertionError('unexpected destination')
            response._content=json.dumps(data).encode();return response
        def close(self): pass
    original=requests.Session.__init__
    def init(session,*args,**kwargs):
        original(session,*args,**kwargs);session.mount('https://',Port())
    payload=dict(config={'web':dict(client_id=W.SERVER_CLIENT,client_secret='synthetic-secret',token_uri=W.TOKEN_URI,
        auth_uri='https://accounts.google.com/o/oauth2/auth')},code='synthetic-code',account_id='synthetic-id',account_email='synthetic@example.invalid')
    with patch.object(requests.Session,'__init__',init), patch.dict(os.environ,{},clear=False):
        try: result=W.exchange(payload)
        except Exception:
            if case=='success': raise
        else:
            require_equal(case,'success','invalid identity/scope accepted')
            require_equal(result,{'refresh_token':'synthetic-refresh'})
    require_equal(destinations.count(W.TOKEN_URI),1)
    require(all(x in (W.TOKEN_URI,'https://www.googleapis.com/oauth2/v1/certs') for x in destinations))


def t_official_code_exchange_and_signed_account_verification(): probe('success')
def t_wrong_or_unverified_account_signature_audience_and_expiry_are_refused():
    for case in ('account','audience','expired','unverified','signature'): probe(case)
def t_missing_or_broader_scopes_and_missing_refresh_are_refused():
    for case in ('scope','broader','missing_refresh'): probe(case)
def t_google_token_redirect_never_receives_code_or_client_secret(): probe('redirect')


if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--sdk-case':
        probe(sys.argv[2]);print('SDK-MOBILE-PASS:'+sys.argv[2]);raise SystemExit(0)
    from _harness import run_module
    raise SystemExit(run_module(globals(),__name__))
