"""
CLI principal de Radio Parra usando Typer.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import typer

app = typer.Typer(help="Radio Parra — radio casera con locutor IA")


def _check(label: str, ok: bool, warn: bool = False, msg: str = "") -> None:
    """Imprime una línea de diagnóstico con formato OK / WARN / ERROR."""
    if ok:
        status = typer.style("OK   ", fg=typer.colors.GREEN, bold=True)
    elif warn:
        status = typer.style("WARN ", fg=typer.colors.YELLOW, bold=True)
    else:
        status = typer.style("ERROR", fg=typer.colors.RED, bold=True)
    suffix = f" — {msg}" if msg else ""
    typer.echo(f"[{status}] {label}{suffix}")


def _cmd_exists(cmd: str) -> bool:
    """Comprueba si un comando está disponible en el PATH."""
    try:
        subprocess.run(
            [cmd, "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@app.command()
def doctor() -> None:
    """
    Diagnóstico del entorno: comprueba dependencias, dirs y configuración.
    """
    typer.echo("Radio Parra — doctor\n")

    # ffmpeg
    _check("ffmpeg", _cmd_exists("ffmpeg"))
    # mpv
    _check("mpv", _cmd_exists("mpv"))
    # Python version
    major, minor = sys.version_info[:2]
    _check(f"Python {major}.{minor}", major == 3 and minor >= 12, msg="requiere 3.12+")

    # Directorios de datos
    data_dir = Path("data")
    _check("data/", data_dir.exists(), warn=True, msg="ejecuta mkdir data/ si falta")

    # .env
    env_file = Path(".env")
    _check(".env", env_file.exists(), warn=True, msg="copia .env.example → .env")

    # Config válida
    config_dir = Path("config")
    try:
        from radio.core.config import RadioConfig  # noqa: PLC0415
        RadioConfig.load(config_dir)
        _check("config/", True)
    except Exception as exc:
        _check("config/", False, msg=str(exc))


@app.command()
def stock() -> None:
    """
    Lista los segmentos disponibles por tipo y estado.
    """
    from radio.core.store import DB  # noqa: PLC0415

    db = DB(Path("data") / "radio.db")
    segments = db.list_segments()

    if not segments:
        typer.echo("No hay segmentos en el stock.")
        return

    # Agrupar por kind
    by_kind: dict[str, list[dict]] = {}
    for seg in segments:
        by_kind.setdefault(seg["kind"], []).append(seg)

    for kind, items in sorted(by_kind.items()):
        typer.echo(f"\n{kind.upper()} ({len(items)})")
        status_counts: dict[str, int] = {}
        for item in items:
            status_counts[item["status"]] = status_counts.get(item["status"], 0) + 1
        for status, count in sorted(status_counts.items()):
            typer.echo(f"  {status}: {count}")
