"""
CLI principal de Radio Parra usando Typer.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    from radio.core.config import RadioConfig

app = typer.Typer(help="Radio Parra — radio casera con locutor IA")


def _check(label: str, ok: bool, warn: bool = False, msg: str = "", info: str = "") -> None:
    """
    Imprime una línea de diagnóstico con formato OK / WARN / ERROR. ``msg`` se muestra
    si falla; ``info``, si va bien (o si falla y no hay ``msg``).
    """
    if ok:
        status = typer.style("OK   ", fg=typer.colors.GREEN, bold=True)
    elif warn:
        status = typer.style("WARN ", fg=typer.colors.YELLOW, bold=True)
    else:
        status = typer.style("ERROR", fg=typer.colors.RED, bold=True)
    text = info if ok else (msg or info)
    suffix = f" — {text}" if text else ""
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


# Espacio libre mínimo en data/ antes de avisar / dar error (MB)
DOCTOR_FREE_WARN_MB = 1024
DOCTOR_FREE_ERROR_MB = 200


@app.command()
def doctor(
    config_dir: Path = typer.Option(  # noqa: B008
        Path("config"), "--config-dir", help="Directorio de configuración"
    ),
    network: bool = typer.Option(
        False, "--network", help="Comprueba también que el feed de música responde (HEAD)"
    ),
) -> None:
    """
    Diagnóstico del entorno (§9): mpv, ffmpeg, configuración, datos, BD, bucle de
    emergencia, feed de música y productores. Sin red salvo con --network.
    """
    import shutil  # noqa: PLC0415

    from radio.core.config import RadioConfig  # noqa: PLC0415
    from radio.core.paths import db_path  # noqa: PLC0415
    from radio.core.store import DB, SCHEMA_VERSION  # noqa: PLC0415
    from radio.music.library import AUDIO_EXTENSIONS  # noqa: PLC0415
    from radio.producers.registry import PRODUCERS  # noqa: PLC0415
    from radio.station.engine import resolve_emergency_dir  # noqa: PLC0415

    typer.echo("Radio Parra — doctor\n")

    # Python
    major, minor = sys.version_info[:2]
    _check(f"Python {major}.{minor}", major == 3 and minor >= 12, msg="requiere 3.12+")

    # Configuración
    config = None
    try:
        config = RadioConfig.load(config_dir)
        _check(f"{config_dir}/", True)
    except Exception as exc:
        _check(f"{config_dir}/", False, msg=str(exc))

    # Reproductor y postproducción
    mpv_bin = config.station.audio.mpv_bin if config else "mpv"
    _check(f"mpv ({mpv_bin})", _cmd_exists(mpv_bin),
           msg="la emisora no puede sonar sin mpv (apt install mpv)")
    _check("ffmpeg", _cmd_exists("ffmpeg"), warn=True,
           msg="sin ffmpeg no hay normalización de volumen (post)")

    # .env (claves; en Fase 1 solo hay proveedores fake)
    _check(".env", Path(".env").exists(), warn=True, msg="copia .env.example → .env")

    # Directorio de datos: existe, escribible y con espacio
    data_dir = Path(config.station.data_dir) if config else Path("data")
    if not data_dir.is_dir():
        _check(f"{data_dir}/", False, warn=True, msg="no existe; se crea al arrancar")
    else:
        _check(f"{data_dir}/ escribible", os.access(data_dir, os.W_OK),
               msg="sin permiso de escritura")
        free_mb = shutil.disk_usage(data_dir).free / 1024 / 1024
        _check(
            f"{data_dir}/ espacio libre", free_mb >= DOCTOR_FREE_WARN_MB,
            warn=free_mb >= DOCTOR_FREE_ERROR_MB, info=f"{free_mb:.0f} MB",
            msg=f"{free_mb:.0f} MB (la caché de Tiny Desk necesita espacio)",
        )

    # BD y versión de esquema
    state_db = db_path(data_dir)
    if not state_db.exists():
        _check(str(state_db), False, warn=True, msg="no existe; se crea al arrancar")
    else:
        try:
            with DB(state_db) as db:
                version = db.schema_version
            _check(str(state_db), version == SCHEMA_VERSION, info=f"esquema v{version}",
                   msg=f"esquema v{version}, se espera v{SCHEMA_VERSION}")
        except Exception as exc:
            _check(str(state_db), False, msg=str(exc))

    if config is None:
        return

    # Bucle de emergencia (§8, inv. 3)
    emergency = resolve_emergency_dir(config)
    files = sorted(p.name for p in emergency.glob("*") if p.suffix.lower() in AUDIO_EXTENSIONS) \
        if emergency.is_dir() else []
    _check(f"bucle de emergencia ({emergency})", bool(files), info=", ".join(files),
           msg="sin audio: la radio podría quedar muda")

    # Productores (los ejecuta el timer `radio produce --all`, no la emisora)
    active = [n for n, st in config.producers.producers.items() if st.active]
    unknown = [n for n in active if n not in PRODUCERS]
    _check("productores activos", bool(active) and not unknown, warn=not unknown,
           info=", ".join(active),
           msg=f"sin implementar: {', '.join(unknown)}" if unknown
           else "ninguno: no entrará stock nuevo")

    # Proveedores LLM/TTS (solo los usan los productores; sin red)
    _doctor_providers(config)

    # Feed de música (decisión #8)
    tinydesk = config.producers.get("music_tinydesk")
    feed_url = (tinydesk.params.get("feed_url") if tinydesk else None) or ""
    if tinydesk is None or not tinydesk.active:
        _check("music_tinydesk", False, warn=True, msg="inactivo: no entrará música nueva")
    else:
        _check("music_tinydesk feed_url", bool(feed_url), info=feed_url,
               msg="sin configurar (producers.yaml → music_tinydesk.params.feed_url)")
    if network and feed_url:
        import httpx  # noqa: PLC0415

        from radio.music.feed import USER_AGENT  # noqa: PLC0415

        try:
            resp = httpx.head(feed_url, headers={"User-Agent": USER_AGENT},
                              timeout=10.0, follow_redirects=True)
            _check("feed accesible", resp.status_code < 400, warn=True,
                   info=f"HTTP {resp.status_code}", msg=f"HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            _check("feed accesible", False, warn=True,
                   msg=f"{exc} (sin red la emisora sigue sonando desde el stock)")


def _doctor_providers(config: RadioConfig) -> None:
    """
    Comprobaciones de ``radio doctor`` para los proveedores (§4.1), sin red: solo
    miran si las credenciales, el binario y los modelos de voz están presentes.
    """
    from radio.providers.llm.claude import detect_credentials  # noqa: PLC0415
    from radio.providers.registry import LLM_PROVIDERS, TTS_PROVIDERS  # noqa: PLC0415
    from radio.providers.tts.piper import (  # noqa: PLC0415
        DEFAULT_MODELS_DIR,
        PIPER_VOICE_PROVIDERS,
        find_binary,
        resolve_model,
    )

    providers = config.station.providers
    llm = providers.get("llm")
    llm_name = LLM_PROVIDERS.get(llm.name.lower()) if llm else None
    if llm is None or llm_name is None:
        _check("LLM", False, warn=True,
               msg=f"proveedor desconocido: {llm.name}" if llm else "sin configurar")
    elif llm_name == "claude":
        source = detect_credentials()
        _check(f"LLM claude ({llm.model or 'claude-sonnet-5'}) credenciales", source is not None,
               warn=True, info=f"{source} presente (sin comprobar en red)",
               msg="falta ANTHROPIC_API_KEY (o `ant auth login`): no habrá locutor")
    else:
        _check(f"LLM {llm_name}", True, info="sin red")

    tts = providers.get("tts")
    tts_name = TTS_PROVIDERS.get(tts.name.lower()) if tts else None
    if tts is None or tts_name is None:
        _check("TTS", False, warn=True,
               msg=f"proveedor desconocido: {tts.name}" if tts else "sin configurar")
    elif tts_name == "piper":
        binary = str(tts.extra.get("binary", "piper"))
        _check(f"TTS piper ({binary})", find_binary(binary) is not None, warn=True,
               msg="no está instalado: no habrá voz (ver voices.yaml)")
        models_dir = Path(tts.extra.get("models_dir", DEFAULT_MODELS_DIR))
        for entry in config.voices.voices:
            if entry.provider not in PIPER_VOICE_PROVIDERS:
                continue
            model = resolve_model(entry.provider_voice_id, models_dir)
            ok = model.is_file() and model.with_name(model.name + ".json").is_file()
            _check(f"voz {entry.id} ({model})", ok, warn=True,
                   msg="falta el modelo .onnx o su .onnx.json (instálalo a mano, revisa la licencia)")
    elif tts_name == "cloud":
        env = str(tts.extra.get("api_key_env", "ELEVENLABS_API_KEY"))
        _check(f"TTS cloud ({env})", bool(os.environ.get(env, "").strip()), warn=True,
               info="presente (sin comprobar en red)", msg="falta la clave en el entorno / .env")
    else:
        _check(f"TTS {tts_name}", True, info="sin red")


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


@app.command("analyze-loudness")
def analyze_loudness(
    kind: str = typer.Option("music", "--kind", help="Kind de los segmentos a medir"),
    missing_only: bool = typer.Option(
        False, "--missing-only", help="Solo los que aún no tienen meta.loudness_lufs"
    ),
    config_dir: Path = typer.Option(  # noqa: B008
        Path("config"), "--config-dir", help="Directorio de configuración"
    ),
    data_dir: Path | None = typer.Option(  # noqa: B008
        None, "--data-dir", help="Directorio de datos (por defecto station.yaml → data_dir)"
    ),
) -> None:
    """
    Mide con ffmpeg (sin modificar ni recodificar) el loudness del stock `ready` y lo
    guarda en meta, para la normalización en reproducción. Sale con 1 si algo falla.
    """
    from radio.core.config import RadioConfig  # noqa: PLC0415
    from radio.core.paths import db_path  # noqa: PLC0415
    from radio.core.store import DB  # noqa: PLC0415
    from radio.producers.post import NullAnalyzer, analyze_stock, choose_analyzer  # noqa: PLC0415

    config = RadioConfig.load(config_dir)
    path = db_path(data_dir or Path(config.station.data_dir))
    if not path.exists():
        typer.echo(f"No existe la BD {path}: todavía no hay stock.")
        return
    analyzer = choose_analyzer()
    if isinstance(analyzer, NullAnalyzer):
        typer.echo("ffmpeg no está instalado: no se puede medir el loudness.", err=True)
        raise typer.Exit(1)
    with DB(path) as db:
        report = analyze_stock(db, analyzer, kind=kind, missing_only=missing_only)
    typer.echo(report.to_text())
    if report.failed:
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
    timeline: bool = typer.Option(
        False, "--timeline", help="Imprime la línea de tiempo: una línea por archivo emitido"
    ),
    catalog: str = typer.Option(
        "default", "--catalog",
        help="Música sintética: default (canciones de 150–600 s) o tinydesk (conciertos de 15–30 min)",
    ),
    mode: str = typer.Option("default", "--mode", help="Modo de la parrilla (grid.yaml)"),
    talk_stock: bool = typer.Option(
        False, "--talk-stock", help="Añade stock sintético de palabra e intros (afinar grid.yaml)"
    ),
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
    Simula N horas de emisión con el motor de la emisora (reloj falso, sin audio ni red)
    y comprueba los invariantes. Sale con código 1 si alguno falla.
    """
    from datetime import datetime  # noqa: PLC0415

    from radio.core.config import RadioConfig  # noqa: PLC0415
    from radio.sim import CATALOGS, SIM_START, run_simulation  # noqa: PLC0415

    if catalog not in CATALOGS:
        typer.echo(f"Catálogo desconocido {catalog!r} (disponibles: {', '.join(CATALOGS)})",
                   err=True)
        raise typer.Exit(2)
    config = RadioConfig.load(config_dir)
    start_dt = datetime.fromisoformat(start) if start else SIM_START
    report = run_simulation(
        hours=hours, seed=seed, config=config, prompts_dir=prompts_dir, start=start_dt,
        mode=mode, talk_stock=talk_stock, catalog=catalog,  # type: ignore[arg-type]
    )
    if json_out:
        typer.echo(report.to_json(timeline=timeline))
    else:
        typer.echo(report.to_text(timeline=timeline))
    if not report.passed:
        raise typer.Exit(1)


@app.command()
def station(
    config_dir: Path = typer.Option(  # noqa: B008
        Path("config"), "--config-dir", help="Directorio de configuración"
    ),
    data_dir: Path | None = typer.Option(  # noqa: B008
        None, "--data-dir", help="Directorio de datos (por defecto station.yaml → data_dir)"
    ),
    emergency_dir: Path | None = typer.Option(  # noqa: B008
        None, "--emergency-dir",
        help="Audios de emergencia (por defecto station.yaml → playout.emergency_dir)",
    ),
    mode: str = typer.Option("default", "--mode", help="Modo de la parrilla (grid.yaml)"),
) -> None:
    """
    Arranca la emisora real (mpv + reloj del sistema) hasta Ctrl+C / SIGTERM.

    Solo programa y reproduce desde disco: no produce ni usa la red (invariante 2).
    El stock lo rellena `radio produce --all` (deploy/radio-produce.timer).
    """
    import logging  # noqa: PLC0415

    from radio.station.service import run_station  # noqa: PLC0415

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    code = run_station(
        config_dir=config_dir, data_dir=data_dir, emergency_dir=emergency_dir, mode=mode
    )
    if code:
        raise typer.Exit(code)
