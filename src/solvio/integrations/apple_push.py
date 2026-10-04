"""APNs transport using macOS curl's HTTP/2 and the existing secret broker.

Only a generic notification leaves the Core. No token/key is put in argv, logs,
exceptions, notification text, or a model. No custom HTTP/2 implementation.
"""
from __future__ import annotations
import asyncio
import base64
import json
import re
import time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from solvio.capabilities.policy import OriginClass
from solvio.secret_vault.policy import ExecutorId

REF = 'secret://apple-push/nexus'
TOPIC = 'de.solvio.approvals'
HOSTS = {'development': 'https://api.sandbox.push.apple.com',
         'production': 'https://api.push.apple.com'}
PAYLOAD = {'aps': {'alert': {'title': 'SOLVIO',
    'body': 'Es gibt Neuigkeiten oder eine offene Freigabe. Öffne SOLVIO.'},
    'sound': 'default'}, 'solvio_route': 'inbox'}


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def provider_token(raw: str, now: int) -> str:
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {'team_id', 'key_id', 'private_key'}:
        raise ValueError('invalid_push_configuration')
    if any(not re.fullmatch(r'[A-Z0-9]{10}', value[k]) for k in ('team_id', 'key_id')):
        raise ValueError('invalid_push_configuration')
    key = serialization.load_pem_private_key(value['private_key'].encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise ValueError('invalid_push_key')
    header = _b64(json.dumps({'alg': 'ES256', 'kid': value['key_id']}, separators=(',', ':')).encode())
    body = _b64(json.dumps({'iss': value['team_id'], 'iat': now}, separators=(',', ':')).encode())
    signed = (header + '.' + body).encode('ascii')
    r, s = utils.decode_dss_signature(key.sign(signed, ec.ECDSA(hashes.SHA256())))
    return signed.decode() + '.' + _b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))


class ApplePush:
    def __init__(self, broker, *, transport=None):
        self.broker = broker
        self.transport = transport or self._curl

    @property
    def configured(self):
        return self.broker is not None and self.broker.exists(REF)

    async def send(self, device_token: str, environment: str) -> str:
        if not re.fullmatch(r'[0-9a-f]{64,200}', device_token) or environment not in HOSTS:
            return 'invalid_registration'
        if not self.configured:
            return 'not_configured'
        host = HOSTS[environment]
        try:
            with self.broker.use(REF, executor=ExecutorId.HTTP, target=host,
                    capability='push_notify', origin=OriginClass.BACKGROUND_AUTOMATION) as secret:
                jwt = provider_token(secret.plaintext(), int(time.time()))
            # curl config quoting is JSON-compatible for this closed ASCII header/url.
            values = [('url', host + '/3/device/' + device_token),
                      ('header', 'authorization: bearer ' + jwt),
                      ('header', 'apns-topic: ' + TOPIC),
                      ('header', 'apns-push-type: alert'),
                      ('header', 'apns-priority: 10'),
                      ('header', 'apns-expiration: 0'),
                      ('header', 'apns-collapse-id: solvio-attention'),
                      ('header', 'content-type: application/json'),
                      ('data', json.dumps(PAYLOAD, ensure_ascii=True, separators=(',', ':')))]
            config = '\n'.join(k + ' = ' + json.dumps(v) for k, v in values).encode()
            return await self.transport(config)
        except asyncio.CancelledError:
            raise
        except Exception:
            return 'unavailable'

    @staticmethod
    async def _curl(config: bytes) -> str:
        proc = await asyncio.create_subprocess_exec('/usr/bin/curl', '--disable', '--http2',
            '--silent', '--max-time', '20', '--connect-timeout', '10', '--proto', '=https',
            '--proxy', '', '--output', '/dev/null', '--write-out', '%{http_code}', '--config', '-',
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(config), timeout=25)
            if proc.returncode != 0:
                return 'unknown'
            code = out.strip()
            return ('accepted' if code == b'200' else 'unregistered' if code == b'410'
                    else 'rejected')
        except BaseException:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise
