#!/usr/bin/env python3
"""
mpv falso para tests: sirve un subconjunto del protocolo JSON-IPC de mpv.

Se lanza como ``[sys.executable, fake_mpv.py, --idle=yes, ..., --input-ipc-server=S]``.

- "Reproduce" archivos durmiendo su duración: WAV → la de su cabecera (``wave``);
  cualquier otro archivo → un número en texto (segundos); si no se entiende, 0.2 s.
- Un archivo inexistente produce ``end-file`` con ``reason: error``.
- Un archivo cuyo nombre contiene ``crash`` mata el proceso (``os._exit``) justo
  después de emitir ``start-file``: sirve para probar el watchdog.
- Comandos: ``loadfile <f> [append-play|append|replace] [...]``, ``playlist-next [weak|force]``,
  ``playlist-remove <i>``, ``playlist-clear``, ``stop``, ``quit``, ``get_property <playlist|idle-active|
  playlist-pos|path|mpv-version|af>``, ``observe_property <id> idle-active``.
- Versión: ``FAKE_MPV_VERSION`` (por defecto ``mpv 0.35.1``, como Raspberry Pi OS);
  ``none`` hace que ``get_property mpv-version`` falle. Según la versión, ``loadfile``
  acepta la forma antigua ``<f> <flags> <options>`` (< 0.38) o la nueva
  ``<f> <flags> <index> <options>`` (≥ 0.38); la otra forma se rechaza con
  ``invalid parameter``, como haría mpv.
- Opciones por archivo (``af=...``): valen solo mientras suena ese archivo; al acabar
  se vuelve al ``--af`` global de la línea de comandos. ``get_property af`` da el
  filtro en vigor.
- Si existe ``FAKE_MPV_LOG``, añade ahí una línea JSON por ``loadfile``
  (``{"loadfile": [...], "options": {...}}``) y por archivo que empieza
  (``{"start": <f>, "af": <filtro en vigor>}``).
- Eventos: ``start-file``, ``file-loaded``, ``end-file`` (eof/stop/quit/error), ``idle``
  y ``property-change`` de ``idle-active``.
- Si existe la variable ``FAKE_MPV_ARGV_LOG``, añade ahí una línea JSON con su argv.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import wave
from pathlib import Path
from typing import Any

DEFAULT_DURATION = 0.2


def duration_of(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, EOFError, OSError):
        pass
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError, UnicodeDecodeError):
        return DEFAULT_DURATION


LOADFILE_INDEX_VERSION = (0, 38)


def log_line(record: dict[str, Any]) -> None:
    path = os.environ.get("FAKE_MPV_LOG")
    if path:
        with open(path, "a") as fh:
            fh.write(json.dumps(record) + "\n")


def version_tuple(version: str | None) -> tuple[int, int] | None:
    match = re.search(r"(\d+)\.(\d+)", version or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


def split_top(text: str, sep: str) -> list[str]:
    """Parte por ``sep`` fuera de corchetes."""
    parts, depth, cur = [], 0, ""
    for ch in text:
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    return parts


def parse_options(raw: Any) -> dict[str, str] | None:
    """``"af=[x=1],volume=50"`` → ``{"af": "x=1", "volume": "50"}``; None si no se entiende."""
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if not isinstance(raw, str):
        return None
    options: dict[str, str] = {}
    for part in split_top(raw, ","):
        if not part:
            continue
        if "=" not in part:
            return None
        key, value = part.split("=", 1)
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1]
        elif "=" in value:
            return None            # mpv corta el valor en el "=": hay que ir entre corchetes
        options[key] = value
    return options


class Entry:
    _ids = 0

    def __init__(self, filename: str, options: dict[str, str] | None = None) -> None:
        Entry._ids += 1
        self.id = Entry._ids
        self.filename = filename
        self.options = options or {}


class FakeMpv:
    def __init__(self, version: str | None = "mpv 0.35.1", global_af: str = "") -> None:
        self.version = version
        self.global_af = global_af
        self.af = global_af          # filtro en vigor (opciones por archivo incluidas)
        self.cond = threading.Condition()
        self.playlist: list[Entry] = []
        self.playing: Entry | None = None
        self.to_play: Entry | None = None
        self.stop_evt = threading.Event()
        self.end_reason: str | None = None
        self.quitting = False
        self.observed: dict[str, int] = {}
        self.clients: list[socket.socket] = []
        self.send_lock = threading.Lock()

    # ── Salida ───────────────────────────────────────────────────────────────

    def send(self, msg: dict[str, Any]) -> None:
        data = (json.dumps(msg) + "\n").encode()
        with self.send_lock:
            for client in list(self.clients):
                try:
                    client.sendall(data)
                except OSError:
                    self.clients.remove(client)

    def set_idle(self, idle: bool) -> None:
        if "idle-active" in self.observed:
            self.send(
                {
                    "event": "property-change",
                    "id": self.observed["idle-active"],
                    "name": "idle-active",
                    "data": idle,
                }
            )
        if idle:
            self.send({"event": "idle"})

    # ── Reproducción ─────────────────────────────────────────────────────────

    def player_loop(self) -> None:
        while True:
            with self.cond:
                while not self.quitting and self.to_play is None:
                    self.cond.wait()
                if self.quitting:
                    return
                entry = self.to_play
                assert entry is not None
                self.to_play = None
                self.playing = entry
                self.end_reason = None
                self.stop_evt.clear()
                self.af = entry.options.get("af", self.global_af)
            log_line({"start": entry.filename, "af": self.af})
            self.send({"event": "start-file", "playlist_entry_id": entry.id})
            path = Path(entry.filename)
            if "crash" in path.name:
                os._exit(3)
            if not path.exists():
                reason = "error"
            else:
                self.send({"event": "file-loaded"})
                self.stop_evt.wait(duration_of(path))
                with self.cond:
                    reason = self.end_reason or "eof"
            msg: dict[str, Any] = {
                "event": "end-file",
                "reason": reason,
                "playlist_entry_id": entry.id,
            }
            if reason == "error":
                msg["file_error"] = "loading failed"
            self.send(msg)
            with self.cond:
                self.playing = None
                self.af = self.global_af     # las opciones por archivo se restauran
                if self.quitting:
                    return
                nxt = None
                if entry in self.playlist:
                    i = self.playlist.index(entry)
                    if i + 1 < len(self.playlist):
                        nxt = self.playlist[i + 1]
                if self.to_play is None:
                    self.to_play = nxt
                idle = self.to_play is None
                self.cond.notify_all()
            if idle:
                self.set_idle(True)

    def _stop_current_locked(self, reason: str) -> None:
        if self.playing is not None:
            self.end_reason = reason
            self.stop_evt.set()

    # ── Comandos ─────────────────────────────────────────────────────────────

    def handle(self, cmd: list[Any]) -> tuple[str, Any]:
        name = cmd[0] if cmd else ""
        with self.cond:
            if name == "loadfile":
                options = self._loadfile_options(cmd)
                if options is None:
                    return "invalid parameter", None
                entry = Entry(str(cmd[1]), options)
                log_line({"loadfile": cmd, "options": options})
                mode = cmd[2] if len(cmd) > 2 else "replace"
                if mode == "replace":
                    self.playlist = [entry]
                    self.to_play = entry
                    self._stop_current_locked("stop")
                else:
                    self.playlist.append(entry)
                    if mode == "append-play" and self.playing is None and self.to_play is None:
                        self.to_play = entry
                self.cond.notify_all()
                return "success", {"playlist_entry_id": entry.id}
            if name == "playlist-next":
                flag = cmd[1] if len(cmd) > 1 else "weak"
                if self.playing is None:
                    return "error", None
                i = self.playlist.index(self.playing) if self.playing in self.playlist else -1
                if i + 1 >= len(self.playlist) and flag != "force":
                    return "error", None
                self._stop_current_locked("stop")
                return "success", None
            if name == "playlist-remove":
                i = int(cmd[1])
                if not 0 <= i < len(self.playlist) or self.playlist[i] is self.playing:
                    return "error", None
                del self.playlist[i]
                return "success", None
            if name == "playlist-clear":
                # Como mpv: todo fuera salvo el archivo en curso
                self.playlist = [self.playing] if self.playing is not None else []
                if self.to_play is not None and self.to_play is not self.playing:
                    self.to_play = None
                return "success", None
            if name == "stop":
                self.playlist = []
                self.to_play = None
                self._stop_current_locked("stop")
                return "success", None
            if name == "quit":
                self.quitting = True
                self._stop_current_locked("quit")
                self.cond.notify_all()
                return "success", None
            if name == "get_property":
                if cmd[1] == "mpv-version":
                    if self.version is None:
                        return "property unavailable", None
                    return "success", self.version
                return "success", self._property_locked(str(cmd[1]))
            if name == "observe_property":
                self.observed[str(cmd[2])] = int(cmd[1])
                return "success", None
        return "invalid parameter", None

    def _loadfile_options(self, cmd: list[Any]) -> dict[str, str] | None:
        """Opciones por archivo según la forma de ``loadfile`` de esta versión (None = error)."""
        extra = cmd[3:]
        if not extra:
            return {}
        new_form = (version_tuple(self.version) or (0, 0)) >= LOADFILE_INDEX_VERSION
        if new_form:
            if len(extra) > 2 or isinstance(extra[0], str) and not extra[0].lstrip("-").isdigit():
                return None
            return parse_options(extra[1]) if len(extra) == 2 else {}
        if len(extra) > 1:
            return None               # mpv antiguo: demasiados argumentos
        return parse_options(extra[0])

    def _property_locked(self, prop: str) -> Any:
        if prop == "playlist":
            return [
                {"filename": e.filename, "id": e.id, **({"current": True} if e is self.playing else {})}
                for e in self.playlist
            ]
        if prop == "idle-active":
            return self.playing is None and self.to_play is None
        if prop == "playlist-pos":
            return self.playlist.index(self.playing) if self.playing in self.playlist else -1
        if prop == "path":
            return self.playing.filename if self.playing else None
        if prop == "af":
            return self.af
        return None

    def client_loop(self, conn: socket.socket) -> None:
        with self.send_lock:
            self.clients.append(conn)
        with conn.makefile("rb") as stream:
            for raw in stream:
                try:
                    req = json.loads(raw)
                except ValueError:
                    continue
                error, data = self.handle(req.get("command", []))
                reply: dict[str, Any] = {"error": error, "data": data}
                if "request_id" in req:
                    reply["request_id"] = req["request_id"]
                self.send(reply)
                if req.get("command", [None])[0] == "quit":
                    # Deja que salga el end-file(quit) y termina
                    threading.Timer(0.05, lambda: os._exit(0)).start()
                    return


def main(argv: list[str]) -> int:
    log_path = os.environ.get("FAKE_MPV_ARGV_LOG")
    if log_path:
        with open(log_path, "a") as fh:
            fh.write(json.dumps(argv) + "\n")
    sock_path = next(
        (a.split("=", 1)[1] for a in argv if a.startswith("--input-ipc-server=")), None
    )
    if sock_path is None:
        print("fake_mpv: falta --input-ipc-server", file=sys.stderr)
        return 2
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(4)
    version = os.environ.get("FAKE_MPV_VERSION", "mpv 0.35.1")
    global_af = next((a.split("=", 1)[1] for a in argv if a.startswith("--af=")), "")
    mpv = FakeMpv(None if version == "none" else version, global_af)
    threading.Thread(target=mpv.player_loop, daemon=True).start()
    while True:
        conn, _ = server.accept()
        threading.Thread(target=mpv.client_loop, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
