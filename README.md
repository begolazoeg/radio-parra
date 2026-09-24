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
uv run radio produce --all                  # rellena stock (job; lo lanza el timer de systemd)
uv run radio produce music_tinydesk         # un productor concreto (añade --dry-run para ver qué haría)
uv run radio stock                          # stock por kind frente a su objetivo y caducidades
uv run radio import-music ~/Musica/TinyDesk # SOLO DESARROLLO: audios locales como música "ready"
uv run radio simulate --hours 24 --seed 1   # simulación acelerada (añade --json)
uv run radio station                        # emisora real (mpv); Ctrl+C para parar
```

- **`produce [NAME] [--all] [--dry-run] [--config-dir config] [--data-dir data]`**:
  ejecuta productores como job puntual, fuera de la emisora (invariante 2). Con un
  nombre, ese productor (aunque esté inactivo). Con `--all`, los activos de
  `producers.yaml` cuyo `cron` haya disparado desde su última ejecución **o** que
  tengan déficit de stock. Cada ejecución queda en `producer_runs` (segmentos,
  caracteres de TTS, coste, error); un fallo no afecta a los demás productores ni al
  stock existente. Regla de gasto: si el gasto del mes alcanza `budget.monthly_eur`,
  los productores de pago se saltan con el error "presupuesto agotado". Sale con
  código 1 si alguna ejecución falla. `--dry-run` solo lista qué tocaría.
- **`import-music DIR [--db data/state.db]`** — *herramienta de desarrollo/offline*:
  sirve para probar la emisora sin red con audios que ya tienes. En la radio real la
  música entra **solo** por el feed RSS oficial (§7, productor `music_tinydesk`). Recorre `DIR` de forma recursiva, lee
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

- **Parrilla** (`grid/`, §4.3): `next_unit(state, stock, now, mode, rng)` es una
  función pura que devuelve la próxima `PlayUnit` (p. ej. `[host_intro, music]`):
  interrupciones (`when: "minute == 0"`, `max_late_seconds`), franjas y patrón cíclico
  por modo, presupuesto de charla (22 % en 60 min), cooldowns, nunca ficción justo tras
  factual y escalera de degradación de §8 (peldaño 5 = emergencia). Config en
  `config/grid.yaml` (modos `default` y `tinydesk`).
- **Playout** (`core/playout.py`): emite cada unidad segmento a segmento, la registra
  en `play_log`, retira la palabra emitida y pone en cuarentena audios ausentes.
  Audio de emergencia en el peldaño 5.
- **Producers** (`producers/`): señal horaria, ejecutada por `ProducerRunner` entre
  segmento y segmento. La locutora (`host_intro`) se rehace en Fase 2 con grounding.
- **Proveedores** (`providers/registry.py`): solo `fake` por ahora.
- **Simulación** (`sim.py`): invariantes duros — cero silencio, nunca dos canciones
  seguidas del mismo artista, al menos `horas - 1` señales horarias a tiempo, charla
  bajo el tope, nunca ficción tras factual, ningún peldaño 5 y ningún producer con
  error. El informe incluye el histograma de peldaños de la escalera. CI ejecuta `radio simulate --hours 6 --seed 1`.

Pendiente: producers factual/ficción, proveedores reales de LLM/TTS y sacar los
producers del bucle de la emisora. Decisiones en `docs/decisions/`.

## Producción de stock (`radio produce`)

- **Framework** (`producers/base.py`): protocolo `Producer` (`deficit`, `produce`) y
  plantilla `StagedProducer` con las etapas gather → write → validate → tts → post →
  register. `register` mueve el audio de `data/tmp/` a `data/stock/<kind>/` y solo
  entonces inserta la fila (y el `state_delta` de ficción) en una transacción.
- **Post** (`producers/post.py`): `ffmpeg` loudnorm en dos pasadas (objetivo
  `station.loudness_lufs`) + recorte de silencios; sin ffmpeg, se avisa y no se toca.
- **Registro** (`producers/registry.py`): `PRODUCERS` = nombre → fábrica.
- **`music_tinydesk`**: lee el feed RSS oficial de `producers.yaml →
  music_tinydesk.params.feed_url`. **La URL es la decisión abierta #8 y no tiene valor
  por defecto**: hasta que la dueña la configure, el productor falla con
  "feed_url no configurado (decisión abierta #8)". Solo episodios con enclosure de
  audio, deduplicados por `guid`, del más reciente al más antiguo, descargas
  secuenciales con `User-Agent` propio y GET condicional (ETag/Last-Modified guardados
  en `data/cache/feeds/`). Tope de caché LRU (`max_cache_items` / `max_cache_mb`):
  lo retirado pasa a `retired` y se borra el archivo (nunca los importados a mano).
- **systemd**: `deploy/radio-produce.service` (oneshot, `radio produce --all`) y
  `deploy/radio-produce.timer` (cada 15 min, `Persistent=true`). Son ejemplos: cambia
  `<RADIO_DIR>` y `<UV>` por tus rutas.

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

## Contacto

Las descargas del feed se identifican con `User-Agent: RadioParra/0.1 (+contacto en README)`.
Es una radio casera sin ánimo de lucro que descarga, de forma secuencial y con GET
condicional, solo el feed RSS oficial y sus audios. Para cualquier incidencia con
estas peticiones, abre un issue en este repositorio.
