"""
Backend de audio real: un único proceso ``mpv`` controlado por JSON-IPC.

Pensado para la Raspberry Pi (ARCHITECTURE.md §4.4): mpv arranca una vez en modo
``--idle`` con ``--input-ipc-server=<socket>``; la emisora le va añadiendo archivos a
su playlist (``loadfile <ruta> append-play``) para mantener el *lookahead* sin huecos
(``--gapless-audio=weak``), y un hilo lector traduce los eventos de mpv
(``start-file``/``end-file``) en ``Started``/``Ended`` para escribir ``play_log``.
El reproductor es tonto (§1 inv. 1): solo conoce rutas de archivo.

Uso típico (lo que hará la emisora)::

    backend = MpvIpcBackend(clock=SystemClock(tz))
    backend.add_listener(on_event)        # Started / Ended → play_log
    backend.enqueue(unit_a_path)          # lookahead: mantener queued() >= 2
    backend.enqueue(unit_b_path)
    ...
    backend.skip()                        # POST /skip
    backend.close()                       # al parar la emisora

``play(path)`` (bloqueante) sigue disponible para quien use la interfaz mínima de §4.1.

Hilos y locks
-------------
- Un ``threading.Condition`` protege todo el estado (cola, archivo en curso, socket).
  Nunca se llama a oyentes ni se espera a mpv con él tomado; ``play()`` espera sobre
  la condición (sin espera activa).
- Hilo lector (uno por conexión): lee líneas JSON del socket, resuelve respuestas a
  peticiones y procesa eventos. Los oyentes se llaman desde aquí, así que **un oyente
  no debe llamar a ``play()``** (bloquearía al propio lector); ``enqueue()``, ``skip()``,
  ``queued()`` y ``current()`` sí se pueden llamar.
- Hilo watchdog: duerme sobre un ``Event`` hasta que la conexión cae o mpv muere.

Correspondencia archivo ↔ evento
--------------------------------
Nunca se reordena la playlist de mpv: cada ``start-file`` corresponde al primer
elemento pendiente (FIFO) y cada ``end-file`` al que suena. Así funciona con mpv
antiguos (Raspberry Pi OS) que no devuelven ``playlist_entry_id`` en ``loadfile``.
Para que la playlist no crezca durante meses, al empezar cada archivo se eliminan
(``playlist-remove 0``) las entradas ya terminadas que quedan delante.

Watchdog (decisión documentada)
-------------------------------
Si mpv muere o el socket se cae:

1. El archivo que sonaba se da por terminado con ``Ended(reason="error")``; **no** se
   reanuda ni se repite (una canción cortada no vuelve a empezar desde cero y, si el
   archivo era el culpable del fallo, no entra en bucle). Si no sonaba nada, se
   descarta igual el primer pendiente (posible culpable: mpv murió al cargarlo), con
   ``Ended(error)`` sin ``Started``.
2. Se relanza mpv tras una espera exponencial (``backoff_initial`` · 2ⁿ, tope
   ``backoff_max``); la racha se reinicia cuando un archivo termina con ``eof``.
   Si el relanzamiento falla, se reintenta indefinidamente con la misma política.
3. Los demás pendientes se vuelven a encolar en el mismo orden. ``restarts`` cuenta
   los relanzamientos con éxito.

Los ``play()`` bloqueados sobre el archivo descartado vuelven; los de pendientes
siguen esperando a que suenen en el mpv relanzado.

Ganancia por archivo (normalización en reproducción)
---------------------------------------------------
``enqueue(path, gain_db=g)`` con ``g ≠ 0`` pasa a ``loadfile`` **opciones por archivo**:
``af=[lavfi-volume=volume=<g>dB]`` (filtro ``volume`` de libavfilter entre corchetes,
que es como mpv acepta ``=`` dentro de un valor). mpv aplica las opciones por archivo
solo mientras suena ese archivo y restaura las globales al acabar, así que la ganancia
no pasa al siguiente. El archivo en disco no se toca (la música de Tiny Desk no se
puede modificar). Si ``extra_args`` trae un ``--af=...`` global, se antepone para no
perderlo durante ese archivo. Con ``g = 0`` no se envían opciones (vale en cualquier mpv).

La lista de argumentos de ``loadfile`` cambió en **mpv 0.38**:

- mpv ≥ 0.38: ``loadfile <url> <flags> <index> <options>`` (``index`` = −1: sin uso con
  ``append-play``).
- mpv < 0.38 (p. ej. el de Raspberry Pi OS bookworm, 0.35): ``loadfile <url> <flags>
  <options>``.

Al conectar (también tras cada relanzamiento) se pregunta ``get_property mpv-version``
**antes** de arrancar el hilo lector y se elige la forma. Si la versión no se puede
leer o interpretar, se avisa y se encola **sin ganancia** (mejor 0 dB que un
``loadfile`` rechazado y silencio).

Cierre
------
``close()`` pide ``quit`` a mpv (y si no sale, ``terminate``/``kill``), recoge el proceso
(sin zombis), cierra el socket, borra el archivo del socket y su directorio temporal,
y detiene los hilos. El archivo en curso recibe ``Ended(reason="skipped")``; los
pendientes que no llegaron a sonar se descartan sin eventos. Los ``play()`` bloqueados
vuelven.
"""

from __future__ import annotations

import atexit
import json
import logging
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal

from radio.core.clock import Clock, SystemClock
from radio.providers.audio.events import (
    Ended,
    EndReason,
    EventListener,
    ListenerSet,
    PlayerEvent,
    Started,
)

log = logging.getLogger(__name__)

# Motivos de ``end-file`` de mpv → motivo de ``Ended``
_REASONS: dict[str, EndReason] = {
    "eof": "eof",
    "stop": "skipped",  # playlist-next / stop
    "quit": "skipped",
    "error": "error",
}

# Intervalo de sondeo mientras mpv crea el socket (solo al arrancar, acotado)
_CONNECT_POLL_S = 0.02

# request_id reservado para la pregunta de versión (el resto empieza en 1)
_VERSION_REQUEST_ID = 0

# Primera versión con ``loadfile <url> <flags> <index> <options>``
LOADFILE_INDEX_VERSION = (0, 38)

# Forma de ``loadfile``: con índice (≥ 0.38), sin él (< 0.38) o desconocida (sin opciones)
LoadfileStyle = Literal["index", "legacy", "unknown"]

# Tope de lo que se mira de golpe al leer la respuesta de versión
_PEEK_BYTES = 65536

_VERSION_RE = re.compile(r"(\d+)\.(\d+)")

# Por debajo de esto no se envían opciones por archivo (0 dB)
_GAIN_EPSILON_DB = 0.005


def parse_mpv_version(text: str | None) -> tuple[int, int] | None:
    """``"mpv 0.35.1"`` / ``"mpv v0.38.0-dirty"`` → ``(0, 35)`` / ``(0, 38)``; None si no se entiende."""
    if not text:
        return None
    match = _VERSION_RE.search(text)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def loadfile_style(version: str | None) -> LoadfileStyle:
    """Forma de ``loadfile`` para la versión que informa mpv."""
    parsed = parse_mpv_version(version)
    if parsed is None:
        return "unknown"
    return "index" if parsed >= LOADFILE_INDEX_VERSION else "legacy"


def gain_filter(gain_db: float, base_af: str | None = None) -> str:
    """Valor de la opción ``af`` para una ganancia (con el ``--af`` global delante)."""
    volume = f"lavfi-volume=volume={gain_db:.2f}dB"
    return f"[{base_af},{volume}]" if base_af else f"[{volume}]"


class MpvError(RuntimeError):
    """mpv no arranca, no responde o el backend está cerrado."""


@dataclass(eq=False)
class _Item:
    """Un archivo encolado (identidad por objeto: el mismo path puede ir dos veces)."""

    path: Path
    gain_db: float = 0.0
    done: bool = False


class MpvIpcBackend:
    """
    Backend mpv por JSON-IPC. Implementa ``AudioBackend`` y ``QueueingAudioBackend``.

    API pública
    -----------
    - ``start()``: lanza mpv y conecta (opcional: cualquier ``enqueue``/``play`` lo hace).
    - ``enqueue(path, gain_db=0.0)``: añade a la playlist de mpv (``append-play``), con
      la ganancia como opción por archivo (ver *Ganancia por archivo*).
    - ``play(path)``: ``enqueue`` + bloquear hasta que ese archivo termine.
    - ``skip()``: corta el archivo en curso (``playlist-next force``).
    - ``clear_pending()``: descarta lo encolado que no ha empezado (``playlist-clear``).
    - ``add_listener(cb)`` / ``remove_listener(cb)``: eventos ``Started``/``Ended``.
    - ``queued()``, ``current()``, ``alive()``, ``idle()``, ``restarts``.
    - ``mpv_playlist()``: playlist tal y como la ve mpv (diagnóstico).
    - ``mpv_version`` / ``loadfile_style``: versión detectada y forma de ``loadfile``.
    - ``loadfile_command(path, gain_db)``: el ``loadfile`` que se enviaría.
    - ``close()``: parada limpia. También sirve como gestor de contexto.

    Parámetros
    ----------
    mpv_bin: ejecutable, o lista de argumentos (p. ej. ``[sys.executable, "fake_mpv.py"]``).
    extra_args: argumentos adicionales para mpv (p. ej. ``["--audio-device=alsa/..."]``).
    clock: reloj para las marcas de tiempo de los eventos (inyectable en tests).
    socket_path: ruta del socket IPC; por defecto, uno en un directorio temporal propio.
    gapless: añade ``--gapless-audio=weak``.
    """

    def __init__(
        self,
        mpv_bin: str | Sequence[str] = "mpv",
        extra_args: Sequence[str] | None = None,
        *,
        clock: Clock | None = None,
        socket_path: Path | None = None,
        gapless: bool = True,
        connect_timeout: float = 5.0,
        request_timeout: float = 2.0,
        backoff_initial: float = 0.5,
        backoff_max: float = 30.0,
    ) -> None:
        self._base_cmd = [mpv_bin] if isinstance(mpv_bin, str) else list(mpv_bin)
        self.extra_args = list(extra_args or [])
        self.gapless = gapless
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self._clock: Clock = clock or SystemClock()

        self._socket_dir: Path | None = None
        self._socket_path_arg = socket_path
        self._socket_path: Path | None = socket_path

        self._cond = threading.Condition()
        self._start_lock = threading.Lock()
        self._listeners = ListenerSet()
        self._pending: deque[_Item] = deque()  # enviados (o por enviar), sin empezar
        self._current: _Item | None = None
        self._finished_in_playlist = 0  # entradas terminadas que siguen en la playlist
        self._proc: subprocess.Popen[bytes] | None = None
        self._sock: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._supervisor: threading.Thread | None = None
        self._generation = 0
        self._connected = False
        self._idle = True
        self._started = False
        self._closed = False
        self._restarts = 0
        self._crash_streak = 0
        self._next_request_id = 1
        self._waiting: set[int] = set()
        self._replies: dict[int, dict[str, Any]] = {}
        self._wake = threading.Event()  # despierta al watchdog (caída o cierre)
        self._closing = threading.Event()
        self._version: str | None = None
        self._style: LoadfileStyle = "unknown"
        self._warned_style = False

    # ── Ciclo de vida ────────────────────────────────────────────────────────

    @property
    def socket_path(self) -> Path | None:
        """Ruta del socket IPC (``None`` hasta ``start()`` si no se indicó)."""
        return self._socket_path

    def command(self) -> list[str]:
        """Línea de comandos con la que se lanza mpv."""
        if self._socket_path is None:
            raise MpvError("socket IPC sin decidir: llama a start() primero")
        cmd = [
            *self._base_cmd,
            "--idle=yes",
            "--no-video",
            "--no-terminal",
            "--audio-display=no",
            f"--input-ipc-server={self._socket_path}",
        ]
        if self.gapless:
            cmd.append("--gapless-audio=weak")
        return [*cmd, *self.extra_args]

    def start(self) -> None:
        """Lanza mpv y conecta al socket. Idempotente. Lanza ``MpvError`` si no arranca."""
        with self._start_lock:
            with self._cond:
                if self._closed:
                    raise MpvError("el backend mpv está cerrado")
                if self._started:
                    return
            if self._socket_path is None:
                self._socket_dir = Path(tempfile.mkdtemp(prefix="radio-mpv-"))
                self._socket_path = self._socket_dir / "mpv.sock"
            proc, sock, version = self._spawn_and_connect()
            with self._cond:
                self._install_locked(proc, sock, version)
                self._started = True
            self._supervisor = threading.Thread(
                target=self._supervise, name="mpv-watchdog", daemon=True
            )
            self._supervisor.start()
            atexit.register(self.close)
            log.info("mpv en marcha (pid %s, socket %s)", proc.pid, self._socket_path)

    def close(self) -> None:
        """Para mpv y los hilos, borra el socket. Idempotente."""
        events: list[PlayerEvent] = []
        with self._cond:
            if self._closed:
                return
            self._closed = True
            proc, sock = self._proc, self._sock
            if self._connected:
                self._send_locked(["quit"])
            self._connected = False
            self._generation += 1
            self._proc = self._sock = None
            if self._current is not None:
                self._current.done = True
                events.append(Ended(self._current.path, self._clock.now(), "skipped"))
                self._current = None
            for item in self._pending:
                item.done = True
            self._pending.clear()
            self._cond.notify_all()
        self._closing.set()
        self._wake.set()
        self._listeners.emit(events)
        self._dispose(proc, sock, grace=2.0)
        me = threading.current_thread()
        for thread in (self._reader, self._supervisor):
            if thread is not None and thread is not me:
                thread.join(timeout=5)
        self._remove_socket_files()
        atexit.unregister(self.close)

    def __enter__(self) -> MpvIpcBackend:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # ── API de reproducción ──────────────────────────────────────────────────

    def enqueue(self, path: Path, *, gain_db: float = 0.0) -> None:
        """
        Añade ``path`` al final de la playlist; si mpv está parado, empieza ya.
        ``gain_db`` se aplica solo a este archivo (opción por archivo de ``loadfile``).
        """
        self._enqueue_item(path, gain_db)

    def play(self, path: Path) -> None:
        """
        Encola ``path`` y bloquea hasta que *ese* archivo termine (fin, salto, error o
        cierre del backend). Lo que ya estuviera en cola suena antes.
        """
        item = self._enqueue_item(path)
        with self._cond:
            self._cond.wait_for(lambda: item.done or self._closed)

    def skip(self) -> None:
        """Corta el archivo en curso y pasa al siguiente (o a silencio si no hay)."""
        with self._cond:
            if self._current is None or not self._connected:
                return
            self._send_locked(["playlist-next", "force"])

    def clear_pending(self) -> int:
        """
        Descarta los archivos pendientes (sin eventos) y devuelve cuántos eran.

        Con un archivo sonando se usa ``playlist-clear`` (mpv conserva el actual y
        borra el resto, también las entradas ya terminadas que quedaban delante). Sin
        nada sonando se usa ``stop``, que vacía la playlist. Los ``play()`` que
        esperaban a un pendiente descartado vuelven.
        """
        with self._cond:
            n = len(self._pending)
            if n == 0:
                return 0
            if self._connected:
                self._send_locked(["playlist-clear" if self._current is not None else "stop"])
                self._finished_in_playlist = 0
            for item in self._pending:
                item.done = True
            self._pending.clear()
            self._cond.notify_all()
            return n

    def add_listener(self, listener: EventListener) -> None:
        self._listeners.add(listener)

    def remove_listener(self, listener: EventListener) -> None:
        self._listeners.remove(listener)

    def queued(self) -> int:
        """Archivos pendientes después del que suena ahora."""
        with self._cond:
            return len(self._pending)

    def current(self) -> Path | None:
        """Archivo que suena ahora, o ``None``."""
        with self._cond:
            return self._current.path if self._current is not None else None

    def alive(self) -> bool:
        """True si mpv está vivo y el socket conectado."""
        with self._cond:
            return self._connected and self._proc is not None and self._proc.poll() is None

    def idle(self) -> bool:
        """True si mpv no está reproduciendo nada (propiedad ``idle-active``)."""
        with self._cond:
            return self._idle

    @property
    def restarts(self) -> int:
        """Relanzamientos de mpv hechos por el watchdog."""
        with self._cond:
            return self._restarts

    @property
    def pid(self) -> int | None:
        """PID del proceso mpv actual (diagnóstico y tests)."""
        with self._cond:
            return self._proc.pid if self._proc is not None else None

    @property
    def mpv_version(self) -> str | None:
        """Versión que informó mpv al conectar (``None`` si no se pudo leer)."""
        with self._cond:
            return self._version

    @property
    def loadfile_style(self) -> LoadfileStyle:
        """``index`` (mpv ≥ 0.38), ``legacy`` (< 0.38) o ``unknown`` (sin ganancia)."""
        with self._cond:
            return self._style

    def global_af(self) -> str | None:
        """Último ``--af=...`` de ``extra_args`` (se conserva al aplicar la ganancia)."""
        value = None
        for arg in self.extra_args:
            if arg.startswith("--af="):
                value = arg.split("=", 1)[1] or None
        return value

    def loadfile_command(self, path: Path, gain_db: float = 0.0) -> list[Any]:
        """
        Comando ``loadfile`` para ``path`` con su ganancia, en la forma de la versión
        de mpv conectada (ver *Ganancia por archivo* en el docstring del módulo).
        """
        with self._cond:
            return self._loadfile_locked(_Item(Path(path), gain_db))

    def mpv_playlist(self) -> list[dict[str, Any]]:
        """Playlist según mpv (``get_property playlist``)."""
        data = self._request(["get_property", "playlist"])
        return list(data) if isinstance(data, list) else []

    # ── Internos: cola ───────────────────────────────────────────────────────

    def _enqueue_item(self, path: Path, gain_db: float = 0.0) -> _Item:
        self.start()
        with self._cond:
            if self._closed:
                raise MpvError("el backend mpv está cerrado")
            item = _Item(Path(path), float(gain_db))
            self._pending.append(item)
            if self._connected:
                self._send_locked(self._loadfile_locked(item))
            # Si no hay conexión, el watchdog lo enviará al relanzar mpv
            return item

    # ── Internos: proceso y socket ───────────────────────────────────────────

    def _spawn_and_connect(
        self,
    ) -> tuple[subprocess.Popen[bytes], socket.socket, str | None]:
        """
        Lanza mpv, espera (acotado) a que acepte conexiones en el socket y pregunta su
        versión (``None`` si no contesta a tiempo).
        """
        assert self._socket_path is not None
        self._socket_path.unlink(missing_ok=True)
        cmd = self.command()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise MpvError(f"no se pudo lanzar mpv ({cmd[0]}): {exc}") from exc
        deadline = time.monotonic() + self.connect_timeout
        while True:
            code = proc.poll()
            if code is not None:
                raise MpvError(f"mpv salió al arrancar con código {code}")
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(str(self._socket_path))
            except OSError:
                sock.close()
            else:
                return proc, sock, self._probe_version(sock)
            if time.monotonic() > deadline:
                self._dispose(proc, None, grace=0.0)
                raise MpvError(f"mpv no abrió el socket {self._socket_path} a tiempo")
            if self._closing.wait(_CONNECT_POLL_S):
                self._dispose(proc, None, grace=0.0)
                raise MpvError("backend cerrado mientras arrancaba mpv")

    def _probe_version(self, sock: socket.socket) -> str | None:
        """
        ``get_property mpv-version`` antes de que exista el hilo lector. Se lee del
        socket **línea a línea** (``MSG_PEEK`` hasta el salto de línea y luego solo esos
        bytes) para no consumir eventos que lleguen detrás. Los mensajes anteriores a
        la respuesta (eventos de arranque) se descartan: mpv está vacío e inactivo.
        """
        request = {"command": ["get_property", "mpv-version"], "request_id": _VERSION_REQUEST_ID}
        deadline = time.monotonic() + self.request_timeout
        try:
            sock.sendall((json.dumps(request) + "\n").encode())
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    log.warning("mpv no dijo su versión a tiempo")
                    return None
                sock.settimeout(left)
                peek = sock.recv(_PEEK_BYTES, socket.MSG_PEEK)
                if not peek:
                    return None
                end = peek.find(b"\n")
                if end < 0:
                    if len(peek) >= _PEEK_BYTES:
                        return None
                    time.sleep(_CONNECT_POLL_S)     # línea a medias: que llegue el resto
                    continue
                line = sock.recv(end + 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if isinstance(msg, dict) and msg.get("request_id") == _VERSION_REQUEST_ID:
                    data = msg.get("data")
                    if msg.get("error") != "success" or not isinstance(data, str):
                        return None
                    return data
        except OSError as exc:
            log.warning("No se pudo preguntar la versión a mpv: %s", exc)
            return None
        finally:
            try:
                sock.settimeout(None)
            except OSError:
                pass

    def _install_locked(
        self, proc: subprocess.Popen[bytes], sock: socket.socket, version: str | None
    ) -> None:
        """Adopta una conexión nueva y reenvía los pendientes en orden."""
        style = loadfile_style(version)
        if style != self._style or version != self._version:
            log.info("mpv %s: loadfile %s", version or "(versión desconocida)", {
                "index": "con índice (≥ 0.38)", "legacy": "en la forma antigua (< 0.38)",
                "unknown": "sin opciones por archivo",
            }[style])
        self._version = version
        self._style = style
        self._generation += 1
        gen = self._generation
        self._proc, self._sock = proc, sock
        self._connected = True
        self._idle = True
        self._finished_in_playlist = 0
        self._reader = threading.Thread(
            target=self._read_loop, args=(gen, sock), name=f"mpv-ipc-{gen}", daemon=True
        )
        self._reader.start()
        self._send_locked(["observe_property", 1, "idle-active"])
        for item in self._pending:
            self._send_locked(self._loadfile_locked(item))

    def _loadfile_locked(self, item: _Item) -> list[Any]:
        cmd: list[Any] = ["loadfile", str(item.path), "append-play"]
        if abs(item.gain_db) < _GAIN_EPSILON_DB:
            return cmd
        if self._style == "unknown":
            if not self._warned_style:
                self._warned_style = True
                log.warning("Versión de mpv desconocida (%r): se reproduce sin ganancia "
                            "por archivo", self._version)
            return cmd
        options = f"af={gain_filter(item.gain_db, self.global_af())}"
        if self._style == "index":
            return [*cmd, -1, options]
        return [*cmd, options]

    def _dispose(
        self,
        proc: subprocess.Popen[bytes] | None,
        sock: socket.socket | None,
        *,
        grace: float,
    ) -> None:
        """Cierra el socket y recoge el proceso (terminate/kill si hace falta)."""
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        if proc is None:
            return
        if grace > 0:
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def _remove_socket_files(self) -> None:
        if self._socket_path is not None:
            self._socket_path.unlink(missing_ok=True)
        if self._socket_dir is not None:
            shutil.rmtree(self._socket_dir, ignore_errors=True)

    # ── Internos: protocolo ──────────────────────────────────────────────────

    def _send_locked(self, command: list[Any]) -> int:
        """Envía un comando sin esperar respuesta. Un fallo de socket marca la caída."""
        rid = self._next_request_id
        self._next_request_id += 1
        if self._sock is None or not self._connected:
            return rid
        line = json.dumps({"command": command, "request_id": rid}) + "\n"
        try:
            self._sock.sendall(line.encode())
        except OSError as exc:
            log.warning("Fallo escribiendo en el socket de mpv: %s", exc)
            self._mark_dead_locked()
        return rid

    def _request(self, command: list[Any]) -> Any:
        """Envía un comando y espera su respuesta (``data``)."""
        self.start()
        with self._cond:
            if not self._connected:
                raise MpvError("mpv no está conectado")
            rid = self._next_request_id
            self._waiting.add(rid)
            self._send_locked(command)
            try:
                ok = self._cond.wait_for(
                    lambda: rid in self._replies or not self._connected,
                    timeout=self.request_timeout,
                )
                reply = self._replies.pop(rid, None)
            finally:
                self._waiting.discard(rid)
        if not ok or reply is None:
            raise MpvError(f"mpv no respondió a {command[0]}")
        if reply.get("error") != "success":
            raise MpvError(f"mpv rechazó {command[0]}: {reply.get('error')}")
        return reply.get("data")

    def _read_loop(self, gen: int, sock: socket.socket) -> None:
        """Hilo lector: una línea JSON por mensaje; termina al cerrarse el socket."""
        try:
            with sock.makefile("rb") as stream:
                for raw in stream:
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        log.debug("Línea IPC ilegible: %r", raw)
                        continue
                    if not isinstance(msg, dict):
                        continue
                    with self._cond:
                        if gen != self._generation:
                            return
                        events = self._handle_locked(msg)
                    self._listeners.emit(events)
        except (OSError, ValueError):
            pass
        finally:
            with self._cond:
                if gen == self._generation:
                    log.warning("Conexión con mpv perdida")
                    self._mark_dead_locked()

    def _handle_locked(self, msg: dict[str, Any]) -> list[PlayerEvent]:
        event = msg.get("event")
        if event is None:
            rid = msg.get("request_id")
            if isinstance(rid, int) and rid in self._waiting:
                self._replies[rid] = msg
                self._cond.notify_all()
            return []
        if event == "start-file":
            return self._on_start_locked()
        if event == "end-file":
            return self._on_end_locked(str(msg.get("reason", "error")))
        if event == "property-change" and msg.get("name") == "idle-active":
            self._idle = bool(msg.get("data"))
        elif event == "idle":
            self._idle = True
        return []

    def _on_start_locked(self) -> list[PlayerEvent]:
        now = self._clock.now()
        events: list[PlayerEvent] = []
        if self._current is not None:
            # No debería pasar (falta su end-file): se da por terminado
            log.warning("start-file sin end-file previo para %s", self._current.path)
            events.append(self._finish_locked(self._current, "eof", now))
        if not self._pending:
            log.warning("mpv empezó un archivo que no está en la cola")
            return events
        item = self._pending.popleft()
        self._current = item
        self._idle = False
        # Poda: las entradas terminadas están delante de la actual
        for _ in range(self._finished_in_playlist):
            self._send_locked(["playlist-remove", 0])
        self._finished_in_playlist = 0
        events.append(Started(item.path, now))
        return events

    def _on_end_locked(self, mpv_reason: str) -> list[PlayerEvent]:
        if mpv_reason == "redirect":
            return []
        now = self._clock.now()
        reason = _REASONS.get(mpv_reason, "error")
        item = self._current
        if item is None:
            if reason != "error" or not self._pending:
                return []
            item = self._pending.popleft()  # falló antes de empezar
        self._finished_in_playlist += 1
        if reason == "eof":
            self._crash_streak = 0
        return [self._finish_locked(item, reason, now)]

    def _finish_locked(self, item: _Item, reason: EndReason, now: datetime) -> Ended:
        if item is self._current:
            self._current = None
        item.done = True
        self._cond.notify_all()
        if reason == "error":
            log.warning("mpv no pudo reproducir %s", item.path)
        return Ended(item.path, now, reason)

    def _mark_dead_locked(self) -> None:
        self._connected = False
        self._generation += 1  # ignora lo que aún lea el hilo lector viejo
        self._cond.notify_all()
        self._wake.set()

    # ── Watchdog ─────────────────────────────────────────────────────────────

    def _supervise(self) -> None:
        while True:
            self._wake.wait()
            with self._cond:
                if self._closed:
                    return
                self._wake.clear()
                if self._connected:
                    continue
                proc, sock, reader = self._proc, self._sock, self._reader
                self._proc = self._sock = None
                events: list[PlayerEvent] = []
                culprit = self._current
                if culprit is None and self._pending:
                    culprit = self._pending.popleft()
                if culprit is not None:
                    events.append(self._finish_locked(culprit, "error", self._clock.now()))
                self._crash_streak += 1
            log.error("mpv caído; se relanza (pendientes: %d)", self.queued())
            self._dispose(proc, sock, grace=0.0)
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=5)
            self._listeners.emit(events)
            if not self._relaunch():
                return

    def _relaunch(self) -> bool:
        """Relanza mpv con espera exponencial. False si el backend se cerró antes."""
        while True:
            with self._cond:
                streak = max(self._crash_streak, 1)
            delay = min(self.backoff_initial * 2 ** (streak - 1), self.backoff_max)
            if self._closing.wait(delay):
                return False
            try:
                proc, sock, version = self._spawn_and_connect()
            except MpvError as exc:
                log.error("No se pudo relanzar mpv: %s", exc)
                with self._cond:
                    self._crash_streak += 1
                continue
            with self._cond:
                if self._closed:
                    closed = True
                else:
                    closed = False
                    self._restarts += 1
                    self._install_locked(proc, sock, version)
            if closed:
                self._dispose(proc, sock, grace=0.0)
                return False
            log.warning("mpv relanzado (reinicio nº %d, pid %s)", self._restarts, proc.pid)
            return True
