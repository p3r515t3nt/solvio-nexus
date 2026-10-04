"""Bounded, one-request local transport for a native worker's Core callback.

The worker owns no Core authority or store. There is deliberately no retry:
the Core's existing native delivery journal decides whether a call is known.
Only stdlib imports, so the reviewed isolated worker can load this sibling.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import stat

VERSION = 1
MAX_BYTES = 65536
TIMEOUT = 30.0


def encode(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("native_tool_wire_oversize")
    return raw


def decode(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("native_tool_wire_oversize")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("native_tool_wire_invalid")
            result[key] = value
        return result
    def invalid(_):
        raise ValueError("native_tool_wire_invalid")
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def validate_endpoint(endpoint):
    path = Path(endpoint)
    if (not path.is_absolute() or len(os.fsencode(endpoint)) >= 104
            or path.is_symlink() or str(path.resolve()) != endpoint):
        raise ValueError("native_tool_wire_endpoint_invalid")
    parent, node = path.parent.stat(), path.stat()
    if (parent.st_uid != os.getuid() or parent.st_mode & 0o077
            or node.st_uid != os.getuid() or node.st_mode & 0o077
            or not stat.S_ISSOCK(node.st_mode)):
        raise ValueError("native_tool_wire_endpoint_invalid")


class NativeToolClient:
    def __init__(self, endpoint, manifest_digest):
        self.endpoint, self.manifest_digest = endpoint, manifest_digest

    def _request(self, method, body):
        validate_endpoint(self.endpoint)
        raw = encode({"version": VERSION, "method": method, "body": body})
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(TIMEOUT)
            connection.connect(self.endpoint)
            connection.sendall(raw)
            connection.shutdown(socket.SHUT_WR)
            chunks, size = [], 0
            while True:
                chunk = connection.recv(min(4096, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_BYTES:
                    raise ValueError("native_tool_wire_oversize")
        return decode(b"".join(chunks))

    def manifest(self):
        body = self._request("manifest", {})
        if (type(body) is not dict or set(body) != {"tools"}
                or type(body["tools"]) is not list or digest(body["tools"]) != self.manifest_digest):
            raise ValueError("native_tool_manifest_changed")
        return body["tools"]

    def call(self, body):
        response = self._request("call", body)
        if (type(response) is not dict or set(response) != {"success", "contentItems"}
                or type(response["success"]) is not bool or type(response["contentItems"]) is not list
                or not response["contentItems"] or len(response["contentItems"]) > 4
                or any(type(item) is not dict or set(item) != {"type", "text"}
                       or item["type"] != "inputText" or type(item["text"]) is not str
                       for item in response["contentItems"])):
            raise ValueError("native_tool_wire_invalid")
        return response
