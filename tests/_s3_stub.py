"""Ein winziger S3-Stub auf der Rueckschleife — kein neuer Test-Dienst (§22).

Der Vertrag ist ausdruecklich: „S3 wird im Test durch einen kleinen lokalen
HTTP-Stub gestellt; der echte Provider wird in der Live-Abnahme bewiesen,
nicht im Gate simuliert." Dieser Stub ist deshalb bewusst DUMM — er kann
genau, was die fuenf Verben brauchen, und ausdruecklich NICHT mehr:

* Er versteht `PUT`, `GET`, `HEAD`, `LIST v2` und `COPY`.
* Er kennt **kein** `DELETE` — ein Loeschversuch bekommt `AccessDenied`,
  genau wie beim echten Anbieter mit der B0-Policy. So kann eine Zusicherung
  die Rechtelage nachstellen, ohne sie zu behaupten.
* Er kann auf Kommando Lagen erzeugen (500, 403, Schiefstand, Drosselung,
  ein gekipptes Byte) — die Fehlerpfade aus §18 brauchen einen Weg, sie zu
  erreichen.

Er prueft KEINE Signatur inhaltlich; die Signatur wird gegen die
eingefrorenen Vektoren geprueft (`test_offsite_transport.py`). Er prueft
aber, DASS ein `Authorization`-Kopf und `x-amz-content-sha256` da sind —
ein Client, der unsigniert hinausgeht, faellt hier auf.
"""
from __future__ import annotations

import hashlib
import http.server
import os
import threading
import xml.sax.saxutils as SU


def _xml_error(code: str, message: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><Error>'
            f'<Code>{SU.escape(code)}</Code>'
            f'<Message>{SU.escape(message)}</Message></Error>').encode()


class S3Stub:
    """Ein Objektspeicher im Arbeitsspeicher, mit Schaltern fuer Lagen."""

    def __init__(self) -> None:
        #: (bucket, key) -> bytes
        self.objects: dict[tuple[str, str], bytes] = {}
        #: Versions- und ETag-Buchhaltung, damit die Provider-Wahrheit
        #: ueberhaupt etwas zu sagen hat.
        self.versions: dict[tuple[str, str], str] = {}
        self.requests: list[dict] = []
        #: Lagen-Schalter. Jeder gilt fuer die naechste passende Anfrage.
        self.fail_next: str = ""
        self.fail_methods: tuple[str, ...] = ()
        self.fail_times: int = 1
        self.corrupt_on_get = False
        self.unsigned_seen = False
        self._counter = 0

    # -- Lagen --------------------------------------------------------------
    def arm(self, mode: str, *, methods: tuple[str, ...] = (),
            times: int = 1) -> None:
        """`mode` ist eine Lage: `server_error`, `denied`, `skew`,
        `throttled`, `quota`, `missing`. `methods` grenzt sie auf Verben ein.

        `times` sagt, wie oft sie zuschlaegt. Das ist kein Beiwerk: der
        Client versucht bei `remote_unavailable` mit Backoff erneut, und
        eine Lage, die nur einmal feuert, PRUEFT den Wiederholungsweg —
        eine dauerhafte Lage prueft, dass er aufgibt statt ewig zu ziehen.
        """
        self.fail_next = mode
        self.fail_methods = methods
        self.fail_times = times

    fail_times: int = 1

    def _armed_for(self, method: str) -> str:
        if not self.fail_next:
            return ""
        if self.fail_methods and method not in self.fail_methods:
            return ""
        mode = self.fail_next
        self.fail_times -= 1
        if self.fail_times <= 0:
            self.fail_next = ""
            self.fail_methods = ()
        return mode

    def next_version(self) -> str:
        self._counter += 1
        return f"v{self._counter:08d}"


class _Handler(http.server.BaseHTTPRequestHandler):
    stub: S3Stub = None  # type: ignore[assignment]

    # Stumm: ein Test soll nicht zwischen Serverzeilen suchen muessen.
    def log_message(self, *args) -> None:  # noqa: D102
        return

    # -- Hilfen -------------------------------------------------------------
    def _split(self) -> tuple[str, str, dict[str, str]]:
        from urllib.parse import parse_qs, urlsplit
        parts = urlsplit(self.path)
        segments = parts.path.lstrip("/").split("/", 1)
        bucket = segments[0]
        key = segments[1] if len(segments) > 1 else ""
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        return bucket, key, query

    def _record(self, method: str) -> None:
        self.stub.requests.append({
            "method": method, "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()}})
        if not self.headers.get("Authorization"):
            self.stub.unsigned_seen = True

    def _send(self, status: int, body: bytes = b"",
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _drain(self) -> None:
        """Den Anfragekoerper aufbrauchen, BEVOR ein Fehler geantwortet wird.

        Gemessen beim Bau: antwortet der Stub sofort mit 403, waehrend der
        Client noch 70 MB sendet, bricht die Verbindung — und der Client
        sieht einen TRANSPORTfehler statt der Absage. Sein Backoff versucht
        es dann erneut, die Lage ist schon verbraucht, und der Testfall
        misst genau nichts. Ein echter Server liest erst, dann antwortet er.
        """
        remaining = int(self.headers.get("Content-Length") or 0)
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            remaining -= len(chunk)

    def _lage(self, method: str) -> bool:
        """True, wenn eine armierte Lage die Anfrage beantwortet hat."""
        mode = self.stub._armed_for(method)
        if not mode:
            return False
        self._drain()
        table = {
            "server_error": (500, "InternalError", "we broke"),
            "denied": (403, "AccessDenied", "not allowed"),
            "skew": (403, "RequestTimeTooSkewed",
                     "the difference between the request time and the "
                     "current time is too large"),
            "throttled": (503, "SlowDown", "reduce your request rate"),
            "quota": (400, "QuotaExceeded", "no space left"),
            "missing": (404, "NoSuchKey", "gone"),
        }
        status, code, message = table[mode]
        self._send(status, _xml_error(code, message))
        return True

    # -- Verben -------------------------------------------------------------
    def do_PUT(self) -> None:  # noqa: N802
        self._record("PUT")
        if self._lage("PUT"):
            return
        bucket, key, _query = self._split()
        source = self.headers.get("x-amz-copy-source")
        if source:
            # CopyObject: serverseitig, ohne zweiten Upload.
            src = source.lstrip("/").split("/", 1)
            payload = self.stub.objects.get((src[0], src[1]))
            if payload is None:
                self._send(404, _xml_error("NoSuchKey", "copy source gone"))
                return
            version = self.stub.next_version()
            self.stub.objects[(bucket, key)] = payload
            self.stub.versions[(bucket, key)] = version
            etag = hashlib.md5(payload).hexdigest()
            body = (f'<?xml version="1.0" encoding="UTF-8"?>'
                    f'<CopyObjectResult><ETag>&quot;{etag}&quot;</ETag>'
                    f'</CopyObjectResult>').encode()
            self._send(200, body, {"x-amz-version-id": version})
            return
        # Wie ein Bucket MIT Object Lock (gemessen am echten AWS,
        # 2026-09-01): ohne Integritaetskopf wird der Put abgewiesen. Der
        # SigV4-Nutzlast-Hash zaehlt dafuer ausdruecklich NICHT.
        if not (self.headers.get("x-amz-checksum-sha256")
                or self.headers.get("Content-MD5")):
            self._drain()
            self._send(400, _xml_error(
                "InvalidRequest",
                "Content-MD5 OR x-amz-checksum- HTTP header is required for "
                "Put Object requests with Object Lock parameters"))
            return
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else b""
        checksum = self.headers.get("x-amz-checksum-sha256", "")
        if checksum:
            import base64
            expected = base64.b64encode(
                hashlib.sha256(payload).digest()).decode()
            if checksum != expected:
                self._send(400, _xml_error("BadDigest",
                                           "checksum does not match"))
                return
        declared = self.headers.get("x-amz-content-sha256", "")
        if declared and declared != hashlib.sha256(payload).hexdigest():
            # Der echte Anbieter prueft genau das — und wir wollen, dass ein
            # Client, der falsch rechnet, hier auffaellt und nicht erst im
            # Ruecklade-Vergleich.
            self._send(400, _xml_error("XAmzContentSHA256Mismatch",
                                       "payload hash does not match"))
            return
        version = self.stub.next_version()
        self.stub.objects[(bucket, key)] = payload
        self.stub.versions[(bucket, key)] = version
        self._send(200, b"", {
            "ETag": '"' + hashlib.md5(payload).hexdigest() + '"',
            "x-amz-version-id": version})

    def do_GET(self) -> None:  # noqa: N802
        self._record("GET")
        if self._lage("GET"):
            return
        bucket, key, query = self._split()
        if query.get("list-type") == "2":
            self._listing(bucket, query.get("prefix", ""))
            return
        payload = self.stub.objects.get((bucket, key))
        if payload is None:
            self._send(404, _xml_error("NoSuchKey", "gone"))
            return
        if self.stub.corrupt_on_get and payload:
            payload = bytearray(payload)
            payload[-1] ^= 0xFF          # genau ein gekipptes Byte
            payload = bytes(payload)
        self._send(200, payload, {
            "ETag": '"' + hashlib.md5(payload).hexdigest() + '"',
            "x-amz-version-id": self.stub.versions.get((bucket, key), "")})

    def do_HEAD(self) -> None:  # noqa: N802
        self._record("HEAD")
        if self._lage("HEAD"):
            return
        bucket, key, _query = self._split()
        payload = self.stub.objects.get((bucket, key))
        if payload is None:
            self._send(404)
            return
        self._send(200, b"", {
            "Content-Length": str(len(payload)),
            "ETag": '"' + hashlib.md5(payload).hexdigest() + '"',
            "x-amz-version-id": self.stub.versions.get((bucket, key), "")})

    def do_DELETE(self) -> None:  # noqa: N802
        """Es gibt kein Loeschrecht (§10) — auch nicht im Stub."""
        self._record("DELETE")
        self._send(403, _xml_error("AccessDenied",
                                   "the offsite principal cannot delete"))

    def _listing(self, bucket: str, prefix: str) -> None:
        keys = sorted(k for (b, k) in self.stub.objects if b == bucket
                      and k.startswith(prefix))
        parts = ['<?xml version="1.0" encoding="UTF-8"?>',
                 '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
                 f"<Name>{SU.escape(bucket)}</Name>",
                 f"<Prefix>{SU.escape(prefix)}</Prefix>",
                 f"<KeyCount>{len(keys)}</KeyCount>",
                 "<IsTruncated>false</IsTruncated>"]
        for key in keys:
            payload = self.stub.objects[(bucket, key)]
            parts.append(
                f"<Contents><Key>{SU.escape(key)}</Key>"
                f"<Size>{len(payload)}</Size>"
                f"<ETag>&quot;{hashlib.md5(payload).hexdigest()}&quot;</ETag>"
                f"<LastModified>2026-08-31T00:00:00.000Z</LastModified>"
                f"</Contents>")
        parts.append("</ListBucketResult>")
        self._send(200, "".join(parts).encode())


class RunningStub:
    """Startet den Stub in einem Thread und raeumt ihn wieder weg."""

    def __init__(self) -> None:
        self.stub = S3Stub()
        handler = type("_Bound", (_Handler,), {"stub": self.stub})
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever,
                                        daemon=True)

    def __enter__(self) -> "RunningStub":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> bool:
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(timeout=5)
        return False

    def client(self, **kwargs):
        """Ein `S3Client`, der auf diesen Stub zeigt (nur Rueckschleife)."""
        from solvio.storage.offsite import s3 as OS3
        return OS3.S3Client(region="eu-central-1",
                            endpoint_suffix="127.0.0.1", port=self.port,
                            insecure=True, **kwargs)
