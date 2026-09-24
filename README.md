# Radio Parra

Radio casera con música Tiny Desk, locutor IA y segmentos factuales/ficción.

## Requisitos

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- ffmpeg
- mpv

## Setup

```bash
cp .env.example .env
# editar .env con tus claves API
make setup
```

## Uso

```bash
make test    # pasar tests
make run     # arrancar la radio
make sim     # simular 24 h con semilla 1
make lint    # ruff + mypy
```

## Comandos

```bash
uv run radio doctor                         # diagnóstico del entorno
uv run radio import-music ~/Musica/TinyDesk # importa audios locales como música "ready"
uv run radio stock                          # stock por kind frente a su objetivo y caducidades
uv run radio simulate --hours 24 --seed 1   # simulación acelerada (añade --json)
uv run radio station                        # emisora real (mpv); Ctrl+C para parar
```

- **`import-music DIR [--db data/state.db]`**: recorre `DIR` de forma recursiva, lee
  duración y etiquetas con mutagen (o las deduce de `Artista - Título.ext`) y registra
  cada pista como segmento `music` (en `meta`: título, artista, `source: local` y
  etiquetas `artist:<slug>` y, si la ruta menciona Tiny Desk, `source:tiny_desk`). Los
  archivos no se mueven. Es idempotente.
- **`stock [--config-dir config] [--db data/state.db]`**: por kind, segmentos emitibles
  (`ready` y sin caducar) frente al `target_stock` de `producers.yaml`, recuento por
  estado y próximas caducidades.
- **`simulate [--hours 24] [--seed 1] [--json] [--config-dir config] [--start ISO]`**:
  emite N horas en memoria con reloj falso, un catálogo sintético (250 canciones de
  40 artistas, 3 jingles) y el producer de señal horaria con TTS falso. Imprime el reparto de antena y comprueba los invariantes; sale con código 1
  si alguno falla. Misma semilla → mismo informe.
- **`station [--config-dir config] [--data-dir data] [--emergency-dir assets/emergency]`**:
  bucle real con `SystemClock`, `data/state.db` y mpv. Los producers activos en
  `producers.yaml` se ejecutan entre segmentos según su `cron` (hora local). Si el proveedor LLM/TTS de
  `station.yaml` no está disponible (hoy solo existe `fake`), arranca en modo solo
  música. SIGINT/SIGTERM cortan el audio y cierran la BD.

## Fase 1 — playout y simulación

Qué hay:

- **Scheduler de parrilla** (`core/scheduler.py`): decide el *tipo* de lo siguiente
  según franjas, presupuesto de palabra en hora móvil, rachas, cooldowns y señal horaria.
- **Playout** (`core/playout.py`): elige el segmento concreto (señal de la hora en
  curso, sin repetir artista, sin pisar la señal de la próxima hora), lo emite, lo
  registra en `play_log` y actualiza su estado. Audio de emergencia si no hay nada.
- **Producers** (`producers/`): señal horaria, ejecutada por `ProducerRunner` entre
  segmento y segmento. La locutora (`host_intro`) se rehace en Fase 2 con grounding.
- **Proveedores** (`providers/registry.py`): solo `fake` por ahora.
- **Simulación** (`sim.py`): invariantes duros — cero silencio, nunca dos canciones
  seguidas del mismo artista, al menos `horas - 1` señales horarias y ningún producer
  con error. CI ejecuta `radio simulate --hours 6 --seed 1`.

Pendiente: productor `music_tinydesk`, producers factual/ficción, proveedores reales de
LLM/TTS y producción fuera de la emisora. Decisiones en `docs/decisions/`.

## Datos

Esquema y rutas según ARCHITECTURE.md §3 y §11 (todo bajo `data/`, en `.gitignore`):

```
data/state.db          # SQLite (WAL), esquema v1 en PRAGMA user_version
data/stock/<kind>/     # audio listo para emitir
data/tmp/              # audio a medio generar (se mueve al stock con os.replace)
```

Si tienes una `data/radio.db` o una `state.db` de antes del esquema v1, bórrala: no hay
datos que migrar y la emisora se niega a abrirla.

## Estructura

```
src/radio/      # paquete principal
config/         # station.yaml, grid.yaml, voices.yaml, producers.yaml
prompts/        # plantillas Jinja2 para LLM
universe/       # estado de universo de ficción
assets/         # jingles, stingers, emergency
data/           # generado en runtime (en .gitignore)
tests/          # unit + fixtures
docs/decisions/ # ADRs
```
