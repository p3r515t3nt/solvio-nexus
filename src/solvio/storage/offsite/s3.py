"""Der Transportweg — und die EINZIGE Stelle, die den Anbieterzugang beruehrt.

Fuenf Verben, mehr nicht: `PUT`, `GET`, `HEAD`, `LIST` (v2), `COPY`. Es gibt
in dieser Datei **strukturell keine Loeschmethode** — nicht als Bequemlichkeit,
sondern weil auf dem Mac kein Loeschrecht existiert (Vertrag §10). Der Beweis
dafuer ist die AccessDenied-Probe beim Anbieter, nicht diese Abwesenheit; die
Abwesenheit ist Hygiene, damit niemand versehentlich eine baut.

**Die Credential-Grenze (§16, B2):** Der Zugang liegt als
`secret://offsite/s3` im Geheimnistresor. Der Tresor prueft den Modulnamen des
Aufrufers per Stack-Inspektion — deshalb steht `broker.use(...)` woertlich in
DIESER Datei und in keinem Helfer: `EXECUTOR_MODULES[ExecutorId.OFFSITE]` ist
genau `("solvio.storage.offsite.s3",)`. Weder `pack` (das verschluesselt ohne
Geheimnis) noch `job` (das nur orchestriert) kommen an den Wert. Der Wert lebt
je Anfrage im Kontextmanager des Brokers, wird zum Signieren benutzt und ist
danach fort; er steht in keinem Log, keinem Buch, keiner Ausnahme, keinem
`argv`. Nach aussen sichtbar ist hoechstens die **Access-Key-ID** — eine
Kennung, kein Geheimnis.

**Warum von Hand signiert und nicht `boto3`:** ein SDK bringt eine grosse
Flaeche und, schlimmer, saemtliche Loeschoperationen als Methodenaufruf mit.
Der Vertrag verlangt einen Minimalclient (§19), SigV4 ist ein Dutzend Zeilen
HMAC, und die Nutzlast-Pruefsumme haben wir ohnehin schon: `cipher_sha256`
IST der Wert von `x-amz-content-sha256`. Die Signatur ist gegen die
veroeffentlichten AWS-Testvektoren geprueft (§22) — und die Vektoren selbst
wurden einmal gegen eine unabhaengige Implementierung gemessen, statt aus dem
Gedaechtnis behauptet (`b3/reports/sigv4-vektoren-report.json`).

**Fehlersemantik (§18):** jede Absage bekommt genau eine Kategorie. `403` ist
`auth_failed` — AUSSER `RequestTimeTooSkewed`, das ist `clock_skew` und wird
mit NTP repariert, nie mit einer Rotation. `5xx`, Zeitueberschreitung, DNS und
`SlowDown` sind `remote_unavailable` (mit Backoff). Ein `QuotaExceeded` wird
nie von selbst gesund. Was hier nicht zugeordnet werden kann, faellt nicht
lautlos durch, sondern kommt als `unexpected` heraus.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
import socket
import ssl
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Iterator

from solvio.capabilities import policy as AP
from solvio.secret_vault import broker as B
from solvio.secret_vault import context as SC
from solvio.secret_vault import policy as VP
from solvio.secret_vault.broker import SecretBroker
from solvio.logging_setup import get_logger

log = get_logger("offsite")

SECRET_REF = "secret://offsite/s3"
CAPABILITY = "offsite_backup"
AUTOMATION_ID = "de.solvio.offsite"

SERVICE = "s3"
ALGORITHM = "AWS4-HMAC-SHA256"

#: Frist je Anfrage. Ein 60-MB-PUT ueber eine gewoehnliche Leitung braucht
#: Sekunden bis wenige Minuten; zehn Minuten trennen „langsam" von „haengt".
REQUEST_TIMEOUT = 600.0

#: Backoff fuer `remote_unavailable` und `SlowDown`. Drei Versuche, dann ist
#: es kein Zucken mehr, sondern eine Lage — und der naechste Stundentick des
#: Jobs versucht es ohnehin erneut (§11).
RETRY_DELAYS = (2.0, 8.0)

_CHUNK = 1 << 20

#: Der leere Koerper — SHA-256 von b"".
EMPTY_SHA256 = ("e3b0c44298fc1c149afbf4c8996fb924"
                "27ae41e4649b934ca495991b7852b855")


class S3Error(RuntimeError):
    """Der Transport ist gescheitert. Traegt nie einen Geheimniswert.

    `category` ist eine Kategorie aus §18 — genau eine, immer gesetzt.
    """

    category = "unexpected"

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        #: Kurzer, geheimnisfreier Zusatz fuers Buch (≤ 400 Zeichen, §17).
        self.detail = detail[:400]


class S3AuthError(S3Error):
    """403, fehlender oder abgewiesener Zugang. Kein Rueckfall, nie."""

    category = "auth_failed"


class S3ClockSkew(S3Error):
    """403 `RequestTimeTooSkewed` — die Uhr ist falsch, nicht der Schluessel."""

    category = "clock_skew"


class S3Unavailable(S3Error):
    """Netz, DNS, 5xx, Zeitueberschreitung, SlowDown. Mit Backoff versucht."""

    category = "remote_unavailable"


class S3QuotaExceeded(S3Error):
    """Kontingent oder Bucket voll. Wird nie von selbst gesund."""

    category = "quota_exceeded"


class S3NotFound(S3Error):
    """404. Ob das schlimm ist, entscheidet der Aufrufer, nicht der Client."""

    category = "integrity_failed"


# --------------------------------------------------------------------- SigV4
def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _uri_encode(value: str, *, encode_slash: bool) -> str:
    """AWS-URI-Kodierung: RFC 3986 unreserved bleibt, alles andere %XX.

    `urllib.parse.quote` allein reicht NICHT: seine Vorgabe laesst `/` stehen
    (richtig fuer Pfade, falsch fuer Query-Werte) und behandelt `~` je nach
    Fassung unterschiedlich. Beides sind genau die Stellen, an denen eine
    Signatur still falsch wird.
    """
    safe = "-_.~" + ("/" if encode_slash is False else "")
    return urllib.parse.quote(value, safe=safe)


def canonical_request(method: str, path: str, query: dict[str, str],
                      headers: dict[str, str], payload_sha256: str) -> str:
    """Die kanonische Anfrage nach AWS-Spezifikation.

    Oeffentlich, weil sie gegen die veroeffentlichten Testvektoren geprueft
    wird — eine Signaturfunktion, die man nur durch einen echten Aufruf
    pruefen kann, ist nicht pruefbar.
    """
    canonical_uri = _uri_encode(path, encode_slash=False) or "/"
    canonical_query = "&".join(
        f"{_uri_encode(k, encode_slash=True)}={_uri_encode(v, encode_slash=True)}"
        for k, v in sorted(query.items()))
    lowered = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    canonical_headers = "".join(f"{k}:{lowered[k]}\n" for k in sorted(lowered))
    signed_headers = ";".join(sorted(lowered))
    return "\n".join([method, canonical_uri, canonical_query,
                      canonical_headers, signed_headers, payload_sha256])


def signing_key(secret: str, date_stamp: str, region: str) -> bytes:
    key = _hmac(("AWS4" + secret).encode("utf-8"), date_stamp)
    key = _hmac(key, region)
    key = _hmac(key, SERVICE)
    return _hmac(key, "aws4_request")


def authorization_header(*, access_key_id: str, secret: str, region: str,
                         amz_date: str, method: str, path: str,
                         query: dict[str, str], headers: dict[str, str],
                         payload_sha256: str) -> str:
    """Der fertige `Authorization`-Wert. Der Schluessel bleibt hier drin."""
    date_stamp = amz_date[:8]
    scope = f"{date_stamp}/{region}/{SERVICE}/aws4_request"
    creq = canonical_request(method, path, query, headers, payload_sha256)
    string_to_sign = "\n".join([ALGORITHM, amz_date, scope,
                                _sha256_hex(creq.encode("utf-8"))])
    signature = hmac.new(signing_key(secret, date_stamp, region),
                         string_to_sign.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    signed_headers = ";".join(sorted(k.lower() for k in headers))
    return (f"{ALGORITHM} Credential={access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}")


# ------------------------------------------------------------------- Ergebnis
@dataclass(frozen=True)
class ObjectInfo:
    """Was der Anbieter ueber ein Objekt sagt. Provider-Wahrheit, nie Inhalt."""

    key: str
    bucket: str
    size: int = 0
    etag: str = ""
    version_id: str = ""
    last_modified: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "bucket": self.bucket, "size": self.size,
                "etag": self.etag, "version_id": self.version_id,
                "last_modified": self.last_modified}


def _clean_etag(value: str) -> str:
    return (value or "").strip().strip('"')


def _b64_of_hex(hex_digest: str) -> str:
    """Dieselbe Pruefsumme, andere Schreibweise: hex → base64.

    `x-amz-content-sha256` will hex, `x-amz-checksum-sha256` will base64 —
    dieselben 32 Bytes. Zwei Kodierungen desselben Wertes, weil AWS an zwei
    Stellen zwei Formate verlangt.
    """
    import base64
    return base64.b64encode(bytes.fromhex(hex_digest)).decode("ascii")


# --------------------------------------------------------------------- Client
class S3Client:
    """Der Minimalclient. Kennt fuenf Verben und kein Loeschen.

    Er haelt KEINEN Zugang: jede Anfrage leiht ihn beim Tresor, signiert und
    gibt ihn zurueck. Das kostet ein paar Millisekunden je Anfrage und spart
    die Frage, wie lange ein Anbieterschluessel im Speicher eines
    langlaufenden Prozesses steht.
    """

    def __init__(self, *, region: str, broker: SecretBroker | None = None,
                 endpoint_suffix: str = "amazonaws.com",
                 timeout: float = REQUEST_TIMEOUT,
                 port: int | None = None, insecure: bool = False) -> None:
        self.region = region
        self._broker = broker or SecretBroker()
        self._endpoint_suffix = endpoint_suffix
        self._timeout = timeout
        # `port`/`insecure` existieren fuer den lokalen Test-Stub (§22: kein
        # neuer Test-Dienst, ein kleiner HTTP-Stub). Produktiv sind beide
        # unbenutzt — und der Stub laeuft ausschliesslich auf 127.0.0.1.
        self._port = port
        self._insecure = insecure
        if insecure and not self._loopback_only():
            raise S3Error("insecure transport is only allowed on loopback")

    def _loopback_only(self) -> bool:
        return self._endpoint_suffix.startswith("127.0.0.1") or \
            self._endpoint_suffix.startswith("localhost")

    # -- Zugang ------------------------------------------------------------
    def policy_target(self, bucket: str) -> str:
        """Das Ziel, gegen das der TRESOR prueft — die KANONISCHE Bucket-URL.

        Bewusst NICHT der Transport-Host: der ist im Test die
        Rueckschleife, und eine Zweckbindung, die sich vom Testgeschirr
        verbiegen laesst, bindet nichts. Geprueft wird, WOHIN die Daten
        semantisch gehen (`solvio-offsite-daily` in eu-central-1) — und
        genau diese drei Formen stehen in der Policy des Tresoreintrags.
        """
        if not bucket:
            raise S3AuthError("no bucket for the vault target")
        return f"https://{bucket}.s3.{self.region}.amazonaws.com"

    def _credential(self) -> tuple[str, str]:
        """Leiht den Zugang beim Tresor. NUR hier, und nur fuer Sekunden.

        Der `use()`-Aufruf steht mit Absicht woertlich in dieser Datei: der
        Broker nimmt den Modulnamen des AUFRUFERS per Stack-Inspektion
        (`sys._getframe(1)`). In einem Helfer stuende dort der Helfer — und
        die Bindung, die diesen Zugang schuetzt, waere die auf ein anderes
        Modul.
        """
        target = self.policy_target(self._policy_bucket)
        try:
            with SC.bound(SC.UseContext(
                    origin=AP.OriginClass.BACKGROUND_AUTOMATION,
                    capability=CAPABILITY, automation_id=AUTOMATION_ID)):
                with self._broker.use(SECRET_REF,
                                      executor=VP.ExecutorId.OFFSITE,
                                      target=target) as material:
                    payload = json.loads(material.plaintext())
        except B.SecretDenied as exc:
            raise S3AuthError("offsite credential denied",
                              detail=f"secret_denied:{exc.reason.value}") from None
        except B.SecretUnavailable:
            raise S3AuthError("offsite credential unavailable",
                              detail="vault_unavailable") from None
        except (ValueError, TypeError):
            raise S3AuthError("offsite credential is malformed") from None
        access = str(payload.get("access_key_id") or "")
        secret = str(payload.get("secret_access_key") or "")
        if not access or not secret:
            raise S3AuthError("offsite credential is incomplete")
        return access, secret

    def access_key_id(self, bucket: str) -> str:
        """Die Kennung — kein Geheimnis (§16: sie darf ins Buch).

        Der Bucket ist Pflicht: dieser Aufruf leiht den Zugang wirklich und
        prueft damit die Zweckbindung. Ein Aufruf „ohne Ziel" waere eine
        Abkuerzung um genau die Schranke herum, die hier schuetzt.
        """
        self._policy_bucket = bucket
        access, secret = self._credential()
        del secret
        return access

    # -- Adressierung -------------------------------------------------------
    #: Gegen welches Ziel der Tresor prueft. Wird je Aufruf gesetzt, damit die
    #: Policy-Bindung „nur diese drei Buckets" wirklich greift.
    _policy_bucket = ""

    def _bucket_host(self, bucket: str) -> str:
        if self._loopback_only():
            return self._endpoint_suffix
        return f"{bucket}.s3.{self.region}.{self._endpoint_suffix}"

    def _connect(self, host: str) -> http.client.HTTPConnection:
        if self._insecure:
            return http.client.HTTPConnection(host, port=self._port,
                                              timeout=self._timeout)
        return http.client.HTTPSConnection(
            host, port=self._port, timeout=self._timeout,
            context=ssl.create_default_context())

    # -- Der eine Weg nach draussen ----------------------------------------
    def _request(self, method: str, bucket: str, key: str, *,
                 query: dict[str, str] | None = None,
                 extra_headers: dict[str, str] | None = None,
                 body_path: str = "", body_sha256: str = "",
                 body_length: int = 0,
                 sink_path: str = "") -> tuple[int, dict[str, str], bytes]:
        """Signiert, sendet, ordnet den Fehler ein. Mit Backoff bei Lagen.

        Die Statusauswertung liegt INNERHALB der Wiederholungsschleife — und
        das ist kein Aufbaudetail: stuende sie ausserhalb (wie in der ersten
        Fassung), wuerden nur Transportfehler wiederholt, ein `503`/`SlowDown`
        des Anbieters dagegen nie. §18 verlangt fuer beide denselben Backoff,
        und der eigene Test hat den Unterschied gefunden.
        """
        last: S3Error | None = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                status, headers, body = self._request_once(
                    method, bucket, key, query=query or {},
                    extra_headers=extra_headers or {}, body_path=body_path,
                    body_sha256=body_sha256, body_length=body_length,
                    sink_path=sink_path)
                self._raise_for_status(status, body, method=method, key=key)
                return status, headers, body
            except S3Unavailable as exc:
                last = exc
                if attempt < len(RETRY_DELAYS):
                    delay = RETRY_DELAYS[attempt]
                    log.info("offsite.s3_retry", method=method,
                             attempt=attempt + 1, delay=delay,
                             reason=exc.detail or "unavailable")
                    time.sleep(delay)
                    continue
                raise
        raise last or S3Error("request did not run")

    def _request_once(self, method: str, bucket: str, key: str, *,
                      query: dict[str, str], extra_headers: dict[str, str],
                      body_path: str, body_sha256: str, body_length: int,
                      sink_path: str) -> tuple[int, dict[str, str], bytes]:
        self._policy_bucket = bucket
        host = self._bucket_host(bucket)
        path = "/" + key.lstrip("/") if key else "/"
        if self._loopback_only():
            # Der Stub adressiert Buckets im Pfad, nicht im Namen — sonst
            # braeuchte der Test DNS-Eintraege.
            path = f"/{bucket}" + path
        payload_sha = body_sha256 or EMPTY_SHA256
        amz_date = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        headers = {"Host": host, "x-amz-date": amz_date,
                   "x-amz-content-sha256": payload_sha}
        headers.update(extra_headers)
        if body_length:
            headers["Content-Length"] = str(body_length)

        access, secret = self._credential()
        try:
            headers["Authorization"] = authorization_header(
                access_key_id=access, secret=secret, region=self.region,
                amz_date=amz_date, method=method, path=path, query=query,
                headers=headers, payload_sha256=payload_sha)
        finally:
            # Der Wert lebt genau so lange wie die Signatur ihn braucht.
            del secret

        url = path + (("?" + "&".join(
            f"{_uri_encode(k, encode_slash=True)}={_uri_encode(v, encode_slash=True)}"
            for k, v in sorted(query.items()))) if query else "")

        conn = self._connect(host)
        handle = None
        try:
            handle = open(body_path, "rb") if body_path else None
            try:
                conn.request(method, url, body=handle, headers=headers)
            except (BrokenPipeError, ConnectionResetError):
                # GEMESSEN am 2026-09-01: lehnt S3 eine Anfrage frueh ab (etwa
                # den fehlenden Object-Lock-Integritaetskopf), schickt es die
                # Absage und SCHLIESST — waehrend wir noch 70 MB schreiben.
                # Ein Client, der hier nur den Schreibfehler sieht, meldet
                # `remote_unavailable` und wiederholt eine Anfrage, die
                # NIEMALS gelingen kann. Die Antwort liegt schon im Puffer;
                # wer sie nicht liest, tauscht eine praezise Absage gegen
                # eine falsche Diagnose.
                response = conn.getresponse()
                status = response.status
                resp_headers = {k.lower(): v for k, v in response.getheaders()}
                return status, resp_headers, response.read(1 << 20)
            response = conn.getresponse()
            status = response.status
            resp_headers = {k.lower(): v for k, v in response.getheaders()}
            payload = b""
            if sink_path and 200 <= status < 300:
                # Streamen: eine Generation ist ~60 MB und gehoert nicht in
                # den Arbeitsspeicher (DEBT-0055).
                tmp = sink_path + ".incoming"
                os.makedirs(os.path.dirname(os.path.abspath(tmp)), exist_ok=True)
                with open(tmp, "wb") as sink:
                    while True:
                        chunk = response.read(_CHUNK)
                        if not chunk:
                            break
                        sink.write(chunk)
                os.chmod(tmp, 0o600)
                os.replace(tmp, sink_path)
            else:
                payload = response.read(1 << 20)
            return status, resp_headers, payload
        except (socket.timeout, TimeoutError) as exc:
            raise S3Unavailable("request timed out",
                                detail=type(exc).__name__) from None
        except (socket.gaierror, ConnectionError, ssl.SSLError,
                http.client.HTTPException, OSError) as exc:
            raise S3Unavailable("transport failed",
                                detail=type(exc).__name__) from None
        finally:
            if handle is not None:
                handle.close()
            conn.close()

    def _raise_for_status(self, status: int, body: bytes, *,
                          method: str, key: str) -> None:
        if 200 <= status < 300:
            return
        code = _error_code(body)
        detail = f"{status} {code}" if code else str(status)
        if status == 404 or code in ("NoSuchKey", "NoSuchBucket"):
            raise S3NotFound(f"{method} {key}: not found", detail=detail)
        if code == "RequestTimeTooSkewed":
            raise S3ClockSkew(
                "die Uhr dieses Rechners weicht zu stark ab — NTP pruefen, "
                "NICHT den Zugang rotieren", detail=detail)
        if status in (401, 403) or code in ("AccessDenied", "InvalidAccessKeyId",
                                            "SignatureDoesNotMatch",
                                            "ExpiredToken",
                                            "InvalidClientTokenId"):
            raise S3AuthError(f"{method} {key}: denied", detail=detail)
        if code in ("QuotaExceeded", "AccountProblem", "TooManyBuckets"):
            raise S3QuotaExceeded(f"{method} {key}: quota", detail=detail)
        if status == 429 or code in ("SlowDown", "RequestLimitExceeded"):
            raise S3Unavailable(f"{method} {key}: throttled", detail=detail)
        if status >= 500:
            raise S3Unavailable(f"{method} {key}: server error", detail=detail)
        raise S3Error(f"{method} {key}: unexpected status", detail=detail)

    # -- Die fuenf Verben ---------------------------------------------------
    def put_object(self, bucket: str, key: str, *, file_path: str,
                   sha256: str) -> ObjectInfo:
        """Legt EIN Objekt ab. Kein Multipart — ein PUT ist atomar (§11).

        `sha256` ist nicht optional: er ist die Nutzlast-Pruefsumme der
        Signatur und damit AWS' eigener Integritaetsschutz auf dem Weg.
        Wir haben ihn ohnehin, weil `pack` ihn beim Schreiben rechnet.
        """
        size = os.path.getsize(file_path)
        status, headers, body = self._request(
            "PUT", bucket, key, body_path=file_path, body_sha256=sha256,
            body_length=size,
            extra_headers={
                "Content-Type": "application/octet-stream",
                # GEMESSEN am 2026-09-01 gegen das echte AWS: ein Bucket mit
                # Object Lock VERLANGT bei PutObject einen Integritaetskopf
                # („Content-MD5 OR x-amz-checksum- HTTP header is required for
                # Put Object requests with Object Lock parameters"). Der
                # SigV4-Nutzlast-Hash `x-amz-content-sha256` zaehlt dafuer
                # ausdruecklich NICHT. Wir haben die Pruefsumme ohnehin —
                # sie geht hier ein zweites Mal hinaus, base64 statt hex.
                "x-amz-checksum-sha256": _b64_of_hex(sha256)})
        return ObjectInfo(key=key, bucket=bucket, size=size,
                          etag=_clean_etag(headers.get("etag", "")),
                          version_id=headers.get("x-amz-version-id", ""))

    def get_object(self, bucket: str, key: str, *, dest_path: str) -> ObjectInfo:
        """Holt EIN Objekt in eine Datei. Streamend, nie in den Speicher."""
        status, headers, body = self._request(
            "GET", bucket, key, sink_path=dest_path)
        return ObjectInfo(key=key, bucket=bucket,
                          size=os.path.getsize(dest_path),
                          etag=_clean_etag(headers.get("etag", "")),
                          version_id=headers.get("x-amz-version-id", ""),
                          last_modified=headers.get("last-modified", ""))

    def head_object(self, bucket: str, key: str) -> ObjectInfo:
        """Fragt nach Existenz und Kopfdaten.

        Object-Lock-Felder liefert AWS hier bewusst NICHT: dafuer braeuchte
        der Principal `s3:GetObjectRetention`, und den bekommt die Laufzeit
        nie (B0-Messung). Unsichtbarkeit ist NICHT „nicht angewendet" — die
        Vererbung der Bucket-Default-Retention wurde in B0 mit einem
        Audit-Principal bewiesen.
        """
        status, headers, body = self._request("HEAD", bucket, key)
        return ObjectInfo(key=key, bucket=bucket,
                          size=int(headers.get("content-length") or 0),
                          etag=_clean_etag(headers.get("etag", "")),
                          version_id=headers.get("x-amz-version-id", ""),
                          last_modified=headers.get("last-modified", ""))

    def list_objects(self, bucket: str, *, prefix: str = "",
                     max_keys: int = 1000) -> list[ObjectInfo]:
        """LIST v2, seitenweise bis zum Ende. Fuer den Retention-Abgleich."""
        found: list[ObjectInfo] = []
        token = ""
        while True:
            query = {"list-type": "2", "max-keys": str(max_keys)}
            if prefix:
                query["prefix"] = prefix
            if token:
                query["continuation-token"] = token
            status, _headers, body = self._request("GET", bucket, "",
                                                   query=query)
            page, token = _parse_listing(body, bucket)
            found.extend(page)
            if not token:
                return found

    def copy_object(self, *, src_bucket: str, src_key: str, dst_bucket: str,
                    dst_key: str) -> ObjectInfo:
        """Kopiert serverseitig in einen anderen Klassen-Bucket (§9).

        Kein zweiter Upload: die Kopie entsteht beim Anbieter und **erbt die
        Default-Retention ihres ZIEL-Buckets** — genau dafuer gibt es drei
        Buckets statt einem (§6).
        """
        source = f"/{src_bucket}/{src_key.lstrip('/')}"
        status, headers, body = self._request(
            "PUT", dst_bucket, dst_key,
            extra_headers={"x-amz-copy-source": _uri_encode(
                source, encode_slash=False)})
        version = headers.get("x-amz-version-id", "")
        etag = _clean_etag(headers.get("etag", ""))
        if not etag:
            # Bei CopyObject steht das ETag im XML-Koerper, nicht im Kopf.
            etag = _clean_etag(_first_text(body, "ETag"))
        return ObjectInfo(key=dst_key, bucket=dst_bucket, etag=etag,
                          version_id=version)


# --------------------------------------------------------------- XML-Lesehilfe
def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _first_text(body: bytes, name: str) -> str:
    for element in _iter_elements(body):
        if _strip_ns(element.tag) == name and element.text:
            return element.text.strip()
    return ""


def _iter_elements(body: bytes) -> Iterator[ET.Element]:
    try:
        root = ET.fromstring(body.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return iter(())
    return root.iter()


def _error_code(body: bytes) -> str:
    """Der `<Code>` einer S3-Fehlerantwort — oder leer. Nie eine Ausgabe."""
    if not body:
        return ""
    return _first_text(body, "Code")


def _parse_listing(body: bytes, bucket: str) -> tuple[list[ObjectInfo], str]:
    try:
        root = ET.fromstring(body.decode("utf-8", errors="replace"))
    except ET.ParseError:
        raise S3Error("listing is not valid XML") from None
    items: list[ObjectInfo] = []
    token = ""
    for element in root:
        tag = _strip_ns(element.tag)
        if tag == "NextContinuationToken" and element.text:
            token = element.text.strip()
        elif tag == "Contents":
            fields = {_strip_ns(child.tag): (child.text or "").strip()
                      for child in element}
            items.append(ObjectInfo(
                key=fields.get("Key", ""), bucket=bucket,
                size=int(fields.get("Size") or 0),
                etag=_clean_etag(fields.get("ETag", "")),
                last_modified=fields.get("LastModified", "")))
    return items, token
