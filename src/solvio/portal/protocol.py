"""Die schmale Naht zwischen Core und Portal-Arbeiter.

Zwei Prozesse, zwei Unix-Kennungen, ein Socket. Bewusst kein HTTP auf
`localhost`: ein lauschender HTTP-Port ist fuer jeden lokalen Prozess erreichbar
und kennt seinen Anrufer nicht. Ein Unix-Socket kennt ihn — der Kern haelt die
Kennung des Verbindenden fest, und zwar beim `connect`, nicht auf Zuruf.

Auf macOS heisst diese Auskunft nicht `SO_PEERCRED` (das ist Linux), sondern
`LOCAL_PEERCRED` unter `SOL_LOCAL`, und Python kennt zwar die eine Konstante,
aber nicht die andere — deshalb steht die Null hier ausgeschrieben. Geprueft
wird **einmal beim Annehmen**, bevor ein einziges Byte der Anfrage gelesen wird.
Wer nicht der Core ist, kommt gar nicht erst zum Reden.

Zwei Fallen, beide gemessen und beide hier beantwortet:

* `bind()` legt den Socket unter der `umask` an — mit der ueblichen `022` wird er
  `0755`, und dann fehlt dem anderen Nutzer das Schreibrecht, das `connect(2)`
  verlangt. Also: im engen Verzeichnis binden, dann `chmod`, dann `listen`.
* Die Rechtepruefung faellt bei Unix-Sockets **nicht** von der Eigentuemer- auf
  die Gruppenklasse durch. `0070` verweigert dem Eigentuemer selbst.
"""
from __future__ import annotations

import ctypes
import ctypes.util
import json
import os
import socket
import struct
from typing import Any

#: macOS-Konstanten. `socket.SOL_LOCAL` gibt es in Python nicht — die Null ist
#: aus `sys/un.h` und heisst dort so.
SOL_LOCAL = 0
LOCAL_PEERCRED = 0x001
LOCAL_PEERPID = 0x002

_NGROUPS = 16
_XUCRED_VERSION = 0
_XUCRED_FMT = "=IIh2x" + "I" * _NGROUPS      # 76 Byte, wie sys/ucred.h
_XUCRED_SIZE = struct.calcsize(_XUCRED_FMT)

#: Ein Unix-Socket-Pfad passt in `sun_path` und das ist auf macOS 104 Byte
#: gross. Wird es laenger, meldet `bind` „path too long" — eine Fehlermeldung,
#: die nach einem Rechteproblem klingt und ein Laengenproblem ist. Deshalb wird
#: hier vorher und mit klarem Namen abgelehnt.
MAX_SOCKET_PATH = 104

#: Groessengrenze einer einzelnen Nachricht. Eine Seite kann viel Text
#: produzieren; ein Arbeiter darf den Core damit nicht fluten.
MAX_MESSAGE = 4 * 1024 * 1024

PROTOCOL_VERSION = 1

# -- Operationen ------------------------------------------------------------
PING = "ping"
OPEN_SESSION = "open_session"
OPEN_STATUS = "open_status"
NAVIGATE = "navigate"
READ = "read"
LIST_SESSIONS = "list_sessions"
PROBE = "probe_action"
EXECUTE = "execute"
CLOSE_SESSION = "close_session"
#: Beenden, damit launchd den Dienst auf dem neuen Baum wieder hochbringt.
#: Kein Privileg: der Anrufer ist bereits als der Core ausgewiesen, und ein
#: Neustart gibt ihm nichts, was er nicht ohnehin haette. Der Nutzen ist, dass
#: eine ausgelieferte Policy-Korrektur ohne Administratorrechte wirksam wird —
#: sonst bliebe der Arbeiter aus Bequemlichkeit auf altem Code stehen.
RESTART = "restart"
OPERATIONS = frozenset({PING, OPEN_SESSION, OPEN_STATUS, NAVIGATE, READ, LIST_SESSIONS, PROBE, EXECUTE,
                        CLOSE_SESSION, RESTART})


class ProtocolError(RuntimeError):
    """Die Gegenseite hat etwas geschickt, das nicht zum Vertrag passt."""


def peer_uid(sock: socket.socket) -> int:
    """Die Unix-Kennung des Verbindenden — vom Kern, nicht von der Gegenseite.

    Sie laesst sich nicht faelschen: sie wird beim `connect` festgehalten und
    haengt am angenommenen Socket, nicht an den Daten.
    """
    raw = sock.getsockopt(SOL_LOCAL, LOCAL_PEERCRED, _XUCRED_SIZE)
    fields = struct.unpack(_XUCRED_FMT, raw)
    version, uid = fields[0], fields[1]
    if version != _XUCRED_VERSION:
        raise ProtocolError(f"unexpected xucred version {version}")
    return int(uid)


def peer_pid(sock: socket.socket) -> int:
    """Nur fuers Protokoll. Prozesskennungen werden wiederverwendet."""
    try:
        return int(struct.unpack("=i", sock.getsockopt(SOL_LOCAL, LOCAL_PEERPID, 4))[0])
    except OSError:
        return 0


_libc = None


def peer_euid(sock: socket.socket) -> int:
    """Zweite, unabhaengige Auskunft ueber dieselbe Frage.

    `getpeereid` und `LOCAL_PEERCRED` kommen aus derselben Quelle im Kern, aber
    ueber verschiedene Wege. Beide zu fragen kostet nichts und faengt einen
    Strukturfehler in der Entpackung, der sonst still eine falsche Kennung
    liefern wuerde.
    """
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        _libc.getpeereid.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint32),
                                     ctypes.POINTER(ctypes.c_uint32)]
        _libc.getpeereid.restype = ctypes.c_int
    uid, gid = ctypes.c_uint32(), ctypes.c_uint32()
    if _libc.getpeereid(sock.fileno(), ctypes.byref(uid), ctypes.byref(gid)) != 0:
        errno = ctypes.get_errno()
        raise ProtocolError(f"getpeereid failed: {os.strerror(errno)}")
    return int(uid.value)


def authenticate(sock: socket.socket, *, allowed_uid: int) -> int:
    """Laesst nur den erwarteten Anrufer durch. Fail-closed, vor dem ersten Byte."""
    first = peer_uid(sock)
    second = peer_euid(sock)
    if first != second:
        raise ProtocolError("peer credentials disagree")
    if first != allowed_uid:
        raise ProtocolError(f"peer uid {first} is not permitted")
    return first


def bind_listener(path: str, *, mode: int = 0o660) -> socket.socket:
    """Legt den Socket an — in dieser Reihenfolge, aus dem Grund oben."""
    if len(path.encode("utf-8")) >= MAX_SOCKET_PATH:
        raise ProtocolError(
            f"socket path is {len(path)} bytes, the limit is {MAX_SOCKET_PATH}")
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    if os.path.exists(path):
        os.unlink(path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    os.chmod(path, mode)          # NACH bind, VOR listen — sonst gilt die umask
    server.listen(8)
    return server


# -- Rahmung ----------------------------------------------------------------
def encode(message: dict[str, Any]) -> bytes:
    payload = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(payload) > MAX_MESSAGE:
        raise ProtocolError("message too large")
    return struct.pack("!I", len(payload)) + payload


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining > 0:
        chunk = sock.recv(min(remaining, 65536))
        if not chunk:
            raise ProtocolError("connection closed mid-message")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def decode(sock: socket.socket) -> dict[str, Any]:
    header = _recv_exactly(sock, 4)
    length = struct.unpack("!I", header)[0]
    if length > MAX_MESSAGE:
        raise ProtocolError("declared message too large")
    body = _recv_exactly(sock, length)
    try:
        message = json.loads(body.decode("utf-8"))
    except ValueError as exc:
        raise ProtocolError("message is not valid json") from exc
    if not isinstance(message, dict):
        raise ProtocolError("message is not an object")
    return message


def failure(reason: str, detail: str = "") -> dict[str, Any]:
    """Ein Fehlschlag ohne Innereien. Der Grund ist stabil, der Rest bleibt hier."""
    return {"ok": False, "reason": reason, "detail": detail[:200]}
