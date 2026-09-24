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
    by_kind: dict[str, list[dict[str, object]]] = {}
    for seg in segments:
        by_kind.setdefault(seg["kind"], []).append(seg)

    for kind, items in sorted(by_kind.items()):
        typer.echo(f"\n{kind.upper()} ({len(items)})")
        status_counts: dict[str, int] = {}
        for item in items:
            status = str(item["status"])
            status_counts[status] = status_counts.get(status, 0) + 1
        for status, count in sorted(status_counts.items()):
            typer.echo(f"  {status}: {count}")


@app.command("import-music")
def import_music(
    directory: Path = typer.Argument(  # noqa: B008
        ..., exists=True, file_okay=False, dir_okay=True, help="Directorio con audios"
    ),
    db_path: Path = typer.Option(  # noqa: B008
        Path("data") / "radio.db", "--db", help="Ruta de la base de datos SQLite"
    ),
) -> None:
    """
    Importa una biblioteca musical local (recursiva) como segmentos 'music' listos.
    """
    from radio.core.store import DB  # noqa: PLC0415
    from radio.music.library import import_directory  # noqa: PLC0415

    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = DB(db_path)
    try:
        report = import_directory(db, directory)
    finally:
        db.close()

    typer.echo(f"Añadidas: {report.added}")
    typer.echo(f"Ya existentes: {report.skipped_existing}")
    typer.echo(f"Fallidas: {len(report.failed)}")
    for failed in report.failed:
        typer.echo(f"  - {failed}")


@app.command()
def simulate(
    hours: float = typer.Option(24.0, "--hours", help="Horas de emisión simuladas"),
    seed: int = typer.Option(1, "--seed", help="Semilla (misma semilla → mismo informe)"),
    json_out: bool = typer.Option(False, "--json", help="Informe en JSON"),
    config_dir: Path = typer.Option(  # noqa: B008
        Path("config"), "--config-dir", help="Directorio de configuración"
    ),
    prompts_dir: Path = typer.Option(  # noqa: B008
        Path("prompts"), "--prompts-dir", help="Directorio de plantillas de prompts"
    ),
    start: str | None = typer.Option(
        None, "--start", help="Inicio ISO 8601 (naive = hora local de la emisora)"
    ),
) -> None:
    """
    Simula N horas de emisión en memoria (reloj falso, sin audio) y comprueba invariantes.
    """
    from datetime import datetime  # noqa: PLC0415

    from radio.core.config import RadioConfig  # noqa: PLC0415
    from radio.sim import SIM_START, run_simulation  # noqa: PLC0415

    config = RadioConfig.load(config_dir)
    start_dt = datetime.fromisoformat(start) if start else SIM_START
    report = run_simulation(
        hours=hours, seed=seed, config=config, prompts_dir=prompts_dir, start=start_dt
    )
    typer.echo(report.to_json() if json_out else report.to_text())
    if not report.passed:
        raise typer.Exit(1)


@app.command()
def station(
    config_dir: Path = typer.Option(  # noqa: B008
        Path("config"), "--config-dir", help="Directorio de configuración"
    ),
    data_dir: Path = typer.Option(  # noqa: B008
        Path("data"), "--data-dir", help="Directorio de datos (radio.db, audios generados)"
    ),
    prompts_dir: Path = typer.Option(  # noqa: B008
        Path("prompts"), "--prompts-dir", help="Directorio de plantillas de prompts"
    ),
    emergency_dir: Path = typer.Option(  # noqa: B008
        Path("assets/emergency"), "--emergency-dir", help="Audios de emergencia"
    ),
) -> None:
    """
    Arranca la emisora real (mpv + reloj del sistema) hasta Ctrl+C / SIGTERM.
    """
    import logging  # noqa: PLC0415

    from radio.station import run_station  # noqa: PLC0415

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    run_station(
        config_dir=config_dir,
        data_dir=data_dir,
        prompts_dir=prompts_dir,
        emergency_dir=emergency_dir,
    )
