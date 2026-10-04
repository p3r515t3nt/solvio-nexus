"""Delivery metadata beside the existing inbox, never a second inbox/authority store."""
from __future__ import annotations
import asyncio
import json
import time
from solvio.integrations.apple_push import ApplePush

SCHEMA = '''CREATE TABLE IF NOT EXISTS push_devices (
 device_id TEXT PRIMARY KEY, token TEXT NOT NULL, environment TEXT NOT NULL,
 seen TEXT NOT NULL DEFAULT '[]', last_result TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL
);'''


class PushDelivery:
    def __init__(self, store, control_plane, broker, *, sender=None):
        self.store, self.cp = store, control_plane
        self.sender = sender or ApplePush(broker)
        self.lock = asyncio.Lock()
        self.delivery_lock = asyncio.Lock()
        with store._open() as db:
            db.executescript(SCHEMA)

    async def register(self, device, token, environment):
        def write():
            with self.store._open() as db:
                if not token:
                    db.execute('DELETE FROM push_devices WHERE device_id=?', (device,))
                else:
                    # Rotating a token does not replay old inbox notices.
                    db.execute('INSERT INTO push_devices(device_id,token,environment,updated_at) VALUES(?,?,?,?) '
                        'ON CONFLICT(device_id) DO UPDATE SET token=excluded.token, '
                        'environment=excluded.environment, updated_at=excluded.updated_at',
                        (device, token, environment, time.time()))
        # A successful unsubscribe waits for any already-started delivery.
        async with self.delivery_lock:
            await self.store._run(write)

    async def eligible(self, device):
        from solvio.security.mobile_approval import store as S
        dev = await self.cp.store.get_device(device)
        return bool(dev and dev['status'] == S.DEVICE_ACTIVE
            and dev['attestation_status'] == S.ATT_ATTESTED
            and not self.cp._environment_blocked(dev) and not await self.cp._revoked(dev))

    async def poll(self):
        if not self.sender.configured or self.lock.locked():
            return
        async with self.lock:
            def rows():
                with self.store._open() as db:
                    devices = [dict(r) for r in db.execute('SELECT * FROM push_devices')]
                    ids = ['n:' + r[0] for r in db.execute(
                        'SELECT notification_id FROM proactive WHERE read_at IS NULL '
                        'AND (expires_at IS NULL OR expires_at>?) ORDER BY created_at DESC LIMIT 500', (time.time(),))]
                    return devices, ids
            devices, inbox = await self.store._run(rows)
            for dev in devices:
                if not await self.eligible(dev['device_id']):
                    await self.register(dev['device_id'], '', dev['environment'])
                    continue
                record = await self.cp.store.get_device(dev['device_id'])
                pending = await self.cp.store.list_pending(record['principal'])
                ids = set(inbox + ['a:' + r['approval_id'] for r in pending])
                previous = set(json.loads(dev['seen']))
                if ids == previous:
                    continue
                def claim():
                    with self.store._open() as db:
                        return db.execute('UPDATE push_devices SET seen=?,last_result=? '
                            'WHERE device_id=? AND token=? AND seen=?',
                            (json.dumps(sorted(ids)), 'claimed_unknown', dev['device_id'], dev['token'], dev['seen'])).rowcount
                if not await self.store._run(claim):
                    continue
                if not ids - previous:
                    continue
                # A revocation while awaiting inbox/approval reads still wins.
                if not await self.eligible(dev['device_id']):
                    continue
                async with self.delivery_lock:
                    def current():
                        with self.store._open() as db:
                            return db.execute('SELECT 1 FROM push_devices WHERE device_id=? AND token=? AND environment=?',
                                (dev['device_id'], dev['token'], dev['environment'])).fetchone() is not None
                    if not await self.store._run(current):
                        continue
                    result = await self.sender.send(dev['token'], dev['environment'])
                    def finish():
                        with self.store._open() as db:
                            if result == 'unregistered':
                                db.execute('DELETE FROM push_devices WHERE device_id=? AND token=?', (dev['device_id'], dev['token']))
                            else:
                                db.execute('UPDATE push_devices SET last_result=? WHERE device_id=? AND token=?',
                                           (result, dev['device_id'], dev['token']))
                    await self.store._run(finish)

    async def run(self):
        while True:
            await asyncio.sleep(20)
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Inbox/approval execution must continue even when push fails.
                continue
