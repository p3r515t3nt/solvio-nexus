"""Read-only address-book observations, beside confirmed contacts in the same store.

An import never calls BindingStore.confirm. A matching address-book entry is a
suggestion; the existing Face-ID contact binding remains the only identity writer.
No photos, notes, birthdays, mailing addresses or message contents are imported.
"""
from __future__ import annotations

import json
import time
import unicodedata
from difflib import SequenceMatcher

from solvio.communication.bindings import normalize_alias

MAX_CONTACTS = 3000
MAX_BYTES = 750_000
MAX_AGE = 24 * 60 * 60


def contacts_payload(raw: str) -> list[dict]:
    from solvio.capabilities.gmail import extract_address
    from solvio.agent_runtime.store import _refuse_credentials
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_BYTES:
        raise ValueError('contacts_too_large')
    rows = json.loads(raw)
    if not isinstance(rows, list) or len(rows) > MAX_CONTACTS:
        raise ValueError('invalid_contacts')
    clean = []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'name', 'emails'}:
            raise ValueError('invalid_contact')
        name, emails = row['name'], row['emails']
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 200
                or any(unicodedata.category(c).startswith('C') for c in name)
                or not isinstance(emails, list) or not 1 <= len(emails) <= 10):
            raise ValueError('invalid_contact')
        if any(not isinstance(e, str) or not 3 <= len(e) <= 254
               or any(unicodedata.category(c).startswith('C') for c in e)
               or extract_address(e) != e for e in emails):
            raise ValueError('invalid_contact_address')
        _refuse_credentials(json.dumps(row, ensure_ascii=False), where='contact_source')
        clean.append({'name': name.strip(), 'emails': sorted(set(emails))})
    return clean


class ContactSources:
    def __init__(self, bindings, *, now=time.time):
        self.bindings, self.now = bindings, now
        self.db = bindings._db
        self.db.execute('CREATE TABLE IF NOT EXISTS contact_sources ('
                        'source_key TEXT PRIMARY KEY, updated_at REAL NOT NULL, '
                        'payload TEXT NOT NULL)')

    def replace(self, device_id: str, raw: str) -> int:
        contacts = contacts_payload(raw)
        if not device_id or len(device_id) > 256:
            raise ValueError('invalid_contact_source')
        # The authenticated endpoint supplies the key, never the imported JSON.
        key = 'iphone:' + device_id
        if not contacts:
            self.db.execute('DELETE FROM contact_sources WHERE source_key=?', (key,))
        else:
            self.db.execute('INSERT INTO contact_sources VALUES (?, ?, ?) '
                            'ON CONFLICT(source_key) DO UPDATE SET '
                            'updated_at=excluded.updated_at, payload=excluded.payload',
                            (key, self.now(), json.dumps(contacts, ensure_ascii=False)))
        return len(contacts)

    def search(self, query: str) -> list[dict]:
        wanted = normalize_alias(query)
        if not 2 <= len(wanted) <= 200:
            return []
        now = self.now()
        rows = self.db.execute('SELECT payload FROM contact_sources '
                               'WHERE updated_at>=? AND updated_at<=?',
                               (now - MAX_AGE, now)).fetchall()
        ranked = {}
        for row in rows:
            for contact in json.loads(row[0]):
                name = normalize_alias(contact['name'])
                score = (1.0 if wanted == name else
                         0.95 if set(wanted.split()) <= set(name.split()) else
                         SequenceMatcher(None, wanted, name).ratio())
                # Similarity proposes names only. It never edits an address or
                # turns a source observation into a confirmed contact.
                if score < 0.72:
                    continue
                identity = (contact['name'], tuple(contact['emails']))
                ranked[identity] = (score, {
                    'display_name': contact['name'],
                    'handles': [{'channel': 'gmail', 'value': e} for e in contact['emails']],
                    'confirmed': False, 'content_trust': 'untrusted_contact',
                    'source': 'iphone_contacts', 'match': 'exact' if score == 1 else 'similar',
                })
        return [x[1] for x in sorted(ranked.values(), key=lambda x: (
            -x[0], x[1]['display_name'], str(x[1]['handles'])))[:10]]
