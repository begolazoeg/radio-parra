"""
CLI principal de Radio Parra usando Typer.
"""

from __future__ import annotations

import os
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
def doctor(
    config_dir: Path = typer.Option(  # noqa: B008
        Path("config"), "--config-dir", help="Directorio de configuración"
    ),
) -> None:
    """
    Diagnóstico del entorno: comprueba dependencias, dirs, configuración y BD.
    """
    from radio.core.config import RadioConfig  # noqa: PLC0415
    from radio.core.paths import db_path  # noqa: PLC0415
    from radio.core.store import DB  # noqa: PLC0415

    typer.echo("Radio Parra — doctor\n")

    # ffmpeg
    _check("ffmpeg", _cmd_exists("ffmpeg"))
    # mpv
    _check("mpv", _cmd_exists("mpv"))
    # Python version
    major, minor = sys.version_info[:2]
    _check(f"Python {major}.{minor}", major == 3 and minor >= 12, msg="requiere 3.12+")

    # .env
    env_file = Path(".env")
    _check(".env", env_file.exists(), warn=True, msg="copia .env.example → .env")

    # Config válida
    data_dir = Path("data")
    try:
        config = RadioConfig.load(config_dir)
        data_dir = Path(config.station.data_dir)
        _check(f"{config_dir}/", True)
    except Exception as exc:
        _check(f"{config_dir}/", False, msg=str(exc))

    # Directorio de datos y BD
    _check(f"{data_dir}/", data_dir.is_dir(), warn=True, msg=f"ejecuta mkdir {data_dir}/ si falta")
    if data_dir.is_dir():
        writable = os.access(data_dir, os.W_OK)
        _check(f"{data_dir}/ escribible", writable, msg="sin permiso de escritura")
    state_db = db_path(data_dir)
    if not state_db.exists():
        _check(str(state_db), False, warn=True, msg="no existe; se crea al arrancar")
    else:
        try:
            with DB(state_db) as db:
                version = db.schema_version
            _check(str(state_db), True, msg=f"esquema v{version}")
        except Exception as exc:
            _check(str(state_db), False, msg=str(exc))


@app.command()
def stock(
    config_dir: Path = typer.Option(  # noqa: B008
        Path("config"), "--config-dir", help="Directorio de configuración"
    ),
    db_file: Path | None = typer.Option(  # noqa: B008
        None, "--db", help="Ruta de la BD (por defecto <data_dir>/state.db)"
    ),
) -> None:
    """
    Stock por kind frente a su objetivo (producers.yaml), estados y caducidades próximas.
    """
    from datetime import UTC, datetime  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415

    from radio.core.config import RadioConfig  # noqa: PLC0415
    from radio.core.paths import db_path  # noqa: PLC0415
    from radio.core.store import DB  # noqa: PLC0415
    from radio.producers.registry import PRODUCERS, build_producer  # noqa: PLC0415

    config = RadioConfig.load(config_dir)
    path = db_file or db_path(Path(config.station.data_dir))
    if not path.exists():
        typer.echo(f"No existe la BD {path}: todavía no hay stock.")
        return
    tz = ZoneInfo(config.station.timezone)
    now = datetime.now(UTC)

    # Objetivo por kind: suma de target_stock de los productores activos de ese kind
    targets: dict[str, int] = {}
    unknown: list[str] = []
    for name, settings in config.producers.producers.items():
        if not settings.active:
            continue
        if name not in PRODUCERS:
            unknown.append(name)
            continue
        producer = build_producer(name, config)
        targets[producer.kind] = targets.get(producer.kind, 0) + producer.target_stock

    with DB(path) as db:
        view = db.stock_view(now)
        counts = db.status_counts()
        upcoming = db.next_expirations(now, limit=10)

    kinds = sorted(set(targets) | set(counts))
    if not kinds:
        typer.echo("No hay segmentos en el stock.")
    else:
        typer.echo(f"{'kind':<16} {'ready':>6} {'objetivo':>9}  estados")
    for kind in kinds:
        ready = view.count(kind)
        target = targets.get(kind)
        target_txt = "-" if target is None else str(target)
        flag = " ⚠" if target is not None and ready < target else ""
        states = ", ".join(f"{st}: {n}" for st, n in sorted(counts.get(kind, {}).items()))
        typer.echo(f"{kind:<16} {ready:>6} {target_txt:>9}  {states or '-'}{flag}")
    for name in unknown:
        typer.echo(f"(aviso) productor activo sin implementar: {name}")

    typer.echo("\nPróximas caducidades:")
    if not upcoming:
        typer.echo("  (ninguna)")
    for seg in upcoming:
        assert seg.expires_at is not None
        when = seg.expires_at.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        typer.echo(f"  {when}  {seg.kind:<14} {seg.title}")


@app.command()
def produce(
    name: str | None = typer.Argument(None, help="Productor a ejecutar (producers.yaml)"),
    all_: bool = typer.Option(
        False, "--all", help="Todos los activos con cron pendiente o déficit (timer)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Solo muestra qué se ejecutaría; no produce ni registra"
    ),
    config_dir: Path = typer.Option(  # noqa: B008
        Path("config"), "--config-dir", help="Directorio de configuración"
    ),
    data_dir: Path | None = typer.Option(  # noqa: B008
        None, "--data-dir", help="Directorio de datos (por defecto station.yaml → data_dir)"
    ),
    prompts_dir: Path = typer.Option(  # noqa: B008
        Path("prompts"), "--prompts-dir", help="Directorio de plantillas de prompts"
    ),
) -> None:
    """
    Rellena huecos de stock ejecutando productores (job puntual, no la emisora).
    Sale con código 1 si alguna ejecución falla.
    """
    import logging  # noqa: PLC0415

    from radio.core.clock import SystemClock  # noqa: PLC0415
    from radio.core.config import RadioConfig  # noqa: PLC0415
    from radio.core.paths import db_path  # noqa: PLC0415
    from radio.core.store import DB  # noqa: PLC0415
    from radio.producers.post import NullPost  # noqa: PLC0415
    from radio.producers.runner import build_context  # noqa: PLC0415
    from radio.producers.runner import produce as run_produce  # noqa: PLC0415

    if (name is None) == (not all_):
        typer.echo("Indica un productor o --all (no ambos).", err=True)
        raise typer.Exit(2)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = RadioConfig.load(config_dir)
    data = data_dir or Path(config.station.data_dir)
    data.mkdir(parents=True, exist_ok=True)
    with DB(db_path(data)) as db:
        ctx = build_context(
            config, db, SystemClock(config.station.timezone), data,
            prompts_dir=prompts_dir, post=NullPost() if dry_run else None,
        )
        report = run_produce(ctx, None if all_ else [name or ""], dry_run=dry_run)
    typer.echo(report.to_text())
    if not report.ok:
        raise typer.Exit(1)


@app.command("import-music")
def import_music(
    directory: Path = typer.Argument(  # noqa: B008
        ..., exists=True, file_okay=False, dir_okay=True, help="Directorio con audios"
    ),
    db_path: Path = typer.Option(  # noqa: B008
        Path("data") / "state.db", "--db", help="Ruta de la base de datos SQLite"
    ),
) -> None:
    """
    [Solo desarrollo/offline] Importa audios locales como segmentos 'music'.

    Para probar sin red con archivos que ya tienes. En la radio real la música entra
    solo por el feed RSS oficial (§7): usa `radio produce music_tinydesk`.
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
        Path("data"), "--data-dir", help="Directorio de datos (state.db, stock/, tmp/)"
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
