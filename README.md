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
uv run radio stock                          # segmentos por tipo y estado
uv run radio simulate --hours 24 --seed 1   # simulación acelerada (añade --json)
uv run radio station                        # emisora real (mpv); Ctrl+C para parar
```

- **`import-music DIR [--db data/radio.db]`**: recorre `DIR` de forma recursiva, lee
  duración y etiquetas con mutagen (o las deduce de `Artista - Título.ext`) y registra
  cada pista como segmento `music` con etiquetas `artist:<slug>` y, si la ruta menciona
  Tiny Desk, `source:tiny_desk`. Es idempotente.
- **`simulate [--hours 24] [--seed 1] [--json] [--config-dir config] [--start ISO]`**:
  emite N horas en memoria con reloj falso, un catálogo sintético (250 canciones de
  40 artistas, 3 jingles) y los producers de señal horaria y locutora con LLM/TTS
  falsos. Imprime el reparto de antena y comprueba los invariantes; sale con código 1
  si alguno falla. Misma semilla → mismo informe.
- **`station [--config-dir config] [--data-dir data] [--emergency-dir assets/emergency]`**:
  bucle real con `SystemClock`, `data/radio.db` y mpv. Los producers activos en
  `producers.yaml` se ejecutan entre segmentos. Si el proveedor LLM/TTS de
  `station.yaml` no está disponible (hoy solo existe `fake`), arranca en modo solo
  música. SIGINT/SIGTERM cortan el audio y cierran la BD.

## Fase 1 — playout y simulación

Qué hay:

- **Scheduler de parrilla** (`core/scheduler.py`): decide el *tipo* de lo siguiente
  según franjas, presupuesto de palabra en hora móvil, rachas, cooldowns y señal horaria.
- **Playout** (`core/playout.py`): elige el segmento concreto (señal de la hora en
  curso, sin repetir artista, sin pisar la señal de la próxima hora), lo emite, lo
  registra en `plays` y actualiza su estado. Audio de emergencia si no hay nada.
- **Producers** (`producers/`): señal horaria y locutora IA, ejecutados por
  `ProducerRunner` entre segmento y segmento.
- **Proveedores** (`providers/registry.py`): solo `fake` por ahora.
- **Simulación** (`sim.py`): invariantes duros — cero silencio, nunca dos canciones
  seguidas del mismo artista, al menos `horas - 1` señales horarias y ningún producer
  con error. CI ejecuta `radio simulate --hours 6 --seed 1`.

Pendiente: producers factual/ficción, proveedores reales de LLM/TTS y producers en un
hilo en segundo plano. Decisiones en `docs/decisions/0001-fase-1-playout.md`.

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
