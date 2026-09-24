# Radio Parra

Radio casera con música Tiny Desk, locutor IA y segmentos factuales/ficción.

## Requisitos

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- ffmpeg
- mpv
- [Piper](https://github.com/rhasspy/piper) (TTS local del locutor) y un modelo de voz

## Setup

```bash
cp .env.example .env
# editar .env: ANTHROPIC_API_KEY (o `ant auth login`)
make setup
```

- **LLM (locutor):** Claude Sonnet 5 (`station.yaml → providers.llm`). La clave va en
  `.env` como `ANTHROPIC_API_KEY` (systemd la carga con `EnvironmentFile`; en el
  portátil, `set -a; . ./.env; set +a`). Nunca en git. `radio doctor` avisa si falta.
- **TTS (Piper local):** instala el binario `piper` (o pon su ruta en
  `station.yaml → providers.tts.extra.binary`) y copia a mano el modelo de voz de
  `voices.yaml → locutor_principal` (`es_ES-davefx-medium.onnx` y su `.onnx.json`) en
  `data/models/piper/`. La radio nunca descarga modelos. **Antes de emitir, comprueba
  la licencia del modelo de voz y de su dataset** en su ficha (MODEL_CARD) y que no
  imita a una persona real; si no encaja, cambia el `provider_voice_id`.
- Sin clave o sin Piper, `radio produce --all` sigue trayendo música; `host_intro`
  registra el error en `producer_runs` y la radio suena sin locutor.

## Arquitectura: dos mundos (§2)

| | Productores | Emisora |
|---|---|---|
| Comando | `radio produce --all` (job puntual) | `radio station` (proceso de larga duración) |
| systemd | `radio-produce.timer` cada 15 min → `radio-produce.service` | `radio-station.service` (`Restart=always`) |
| Red | Sí (feed de Tiny Desk, MusicBrainz/Wikipedia, API de Claude) | **No**: solo lee `segments` y la parrilla y escribe `play_log` |
| Si falla | Se registra en `producer_runs` y se reintenta en la siguiente pasada | Nunca silencio: escalera de §8 hasta el bucle de emergencia |

La emisora nunca ejecuta productores (invariante 2): si se cae internet, sigue sonando
con el stock que haya en disco. Decisiones en `docs/decisions/` (ver ADR 0002).

## Uso

```bash
make test    # pasar tests
make run     # arrancar la emisora
make sim     # simular 24 h con semilla 1
make lint    # ruff + mypy
```

## Comandos

```bash
uv run radio doctor                         # diagnóstico del entorno (añade --network para probar el feed)
uv run radio produce --all                  # rellena stock (job; lo lanza el timer de systemd)
uv run radio produce music_tinydesk         # un productor concreto (añade --dry-run para ver qué haría)
uv run radio produce host_intro             # intros del locutor para la música en stock
uv run radio preview host_intro --fake      # una intro de prueba sin red ni claves (quita --fake: Claude + Piper)
uv run radio audit host_intro               # ¿toda intro con datos tiene claims trazables?
uv run radio analyze-loudness --missing-only  # mide (sin tocar) el volumen del stock musical
uv run radio stock                          # stock por kind frente a su objetivo y caducidades
uv run radio import-music ~/Musica/TinyDesk # SOLO DESARROLLO: audios locales como música "ready"
uv run radio simulate --hours 24 --seed 1   # simulación acelerada (--timeline, --json, --catalog tinydesk)
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
- **`station [--config-dir config] [--data-dir data] [--emergency-dir DIR] [--mode default]`**:
  la emisora (§4.4). Un único mpv por JSON-IPC con la playlist como cola: mantiene
  `playout.lookahead_units` unidades (2) en cola por detrás de lo que suena, decididas
  por la parrilla en su hora prevista; escribe `play_log` al empezar y al terminar
  cada archivo; da paso a la señal horaria en punto (cortando la música si
  `interrupts.cut_music`); si no hay nada que emitir suena
  `assets/emergency/emergency_loop.wav`; si mpv muere, el watchdog lo relanza y la cola
  se reconcilia. Dispositivo y argumentos de mpv en `station.yaml → audio`.
  SIGINT/SIGTERM paran limpio (lo que suena se registra como cortado).
- **`simulate [--hours 24] [--seed 1] [--timeline] [--json] [--catalog default|tinydesk] [--mode default] [--talk-stock] [--start ISO]`**:
  ejecuta **el mismo motor que la emisora** con reloj falso y un reproductor falso, en
  memoria y sin red. La producción se modela aparte: `radio produce` cada 15 min
  simulados (señal horaria y locutor `host_intro` con fuentes, LLM y TTS falsos).
  Música sintética: `default` (250 canciones
  de 150–600 s, 40 artistas) o `tinydesk` (40 conciertos de 15–30 min). `--timeline`
  imprime una línea por archivo emitido; por defecto, un resumen (reparto de antena,
  palabra máxima en ventana móvil, señales horarias a tiempo, interrupciones y
  canciones cortadas, unidades con intro del locutor, peldaños de la escalera,
  silencio). Sale con código 1 si falla
  algún invariante. Misma semilla → mismo informe.
- **`preview host_intro [--fake] [--music-id ID] [--no-play] [--out PATH] [--register]`**
  (§9): genera **una** intro y la reproduce con mpv, para iterar prompts y voces. Imprime
  guion, claims, fuentes (licencia y URL), informe de grounding, intentos y coste. Sin
  `--fake` usa los proveedores de `station.yaml` (Claude + Piper) y las fuentes
  abiertas reales sobre la música de `data/state.db` (`--music-id` o la siguiente
  candidata); **no registra nada** salvo `--register`, respeta la regla de gasto y su
  coste queda en `producer_runs` como `preview:host_intro`. Con `--fake`: LLM, TTS y
  fuentes simulados, sin red, claves ni BD. `--out` guarda el audio; `--no-play` no
  lo reproduce.
- **`audit host_intro [--db PATH]`**: comprueba que el 100 % de las intros `ready` con
  datos tienen claims, que cada claim cita una fuente guardada en `meta["sources"]` (con
  URL) y que el grounding vuelve a pasar con lo guardado. Sale con código 1 si no.
- **`analyze-loudness [--kind music] [--missing-only]`**: mide con ffmpeg, **sin
  modificar ni recodificar**, el loudness del stock y lo guarda en `meta` para la
  ganancia en reproducción. Sale con 1 si algo falla o no hay ffmpeg.
- **`doctor [--network]`**: Python, configuración, mpv, ffmpeg (aviso), `.env` (aviso),
  `data/` escribible y con espacio, versión de esquema de `state.db`, bucle de
  emergencia presente, productores activos y `feed_url` de `music_tinydesk`. Solo
  `--network` sale a la red (HEAD al feed).
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

## Fase 1 — estado frente a los criterios de aceptación (§12)

| Criterio | Estado | Cómo se comprueba |
|---|---|---|
| En el portátil se enchufa y suena música en bucle | Hecho (falta probarlo con mpv real y el feed real a mano) | `radio produce music_tinydesk` + `radio station --mode tinydesk`; `test_fase1_acceptance.py` (48 h de solo música en modo `tinydesk`, emisora real contra un mpv falso por socket unix, `radio station` de extremo a extremo con SIGTERM) |
| Cortando la red sigue sonando | Hecho | `test_fase1_acceptance.py`: 12 h de emisión con conexiones AF_INET/AF_INET6 y `httpx` bloqueados; importar `radio.station` no carga `httpx`, `feedparser`, productores ni LLM/TTS |
| `simulate` produce una línea de tiempo de 24 h | Hecho | `radio simulate --hours 24 --seed 1 --timeline`; tests de la línea de tiempo continua de 24 h |
| Productor `music_tinydesk` con dedup por `guid` y tope de caché | Hecho | `test_music_tinydesk.py` (fixture del feed, sin red) |
| Scheduler v0 + `station` con mpv por IPC + `play_log` | Hecho (el scheduler es ya el de §4.3 completo) | `test_grid_*.py`, `test_station_engine.py`, `test_mpv_backend.py` |
| Servicio systemd de ejemplo | Hecho | `deploy/` (`radio-station.service`, `radio-produce.service` + `.timer`) |

Adelantado de fases posteriores (ADR 0002): parrilla completa de §4.3 y señal horaria
(Fase 3). Pendiente: jingles y API/GPIO de control.

Decisiones abiertas que tocan la Fase 1: #1 nombre y #2 idioma son provisionales en
`station.yaml`; #8 (feed) está resuelta.

## Fase 2 — Locutor (intros)

Productor `host_intro` (ADR 0003): la intro hablada del locutor IA antes de cada
concierto, **pegada a fuentes** y vinculada a su canción (`parent_id`, unidad
`[host_intro, music]`).

- **Fuentes: solo abiertas** (decisión de la dueña, 2026-09-24): datos centrales de
  MusicBrainz (CC0; sin géneros/etiquetas, que son CC BY-NC-SA) y resumen de Wikipedia
  (CC BY-SA 4.0; primero en español, si no en inglés), con `User-Agent` propio, ≤ 1
  petición/s a MusicBrainz y caché en `data/cache/sources/`. El título del episodio
  solo es un identificador. **La descripción del episodio de NPR nunca llega al LLM**
  (términos de NPR: no usar su contenido para sistemas de IA) y ni siquiera se guarda.
- **Guion**: Claude Sonnet 5 con las plantillas `prompts/factual/host_intro_*.j2`
  (`host_intro/v1`), JSON `{script, claims: [{text, source_id}]}`; 2–3 frases,
  ≤ 320 caracteres (~20 s), en español; es una IA y nunca dice ser humana.
- **Grounding** (`radio.grounding`): todo número, fecha o nombre propio del guion tiene
  que estar en un claim que cite una fuente donde aparece. Si falla: un reintento con
  prompt más estricto (con los problemas), después una **versión sin dato** y, si
  tampoco, el borrador queda en `quarantined` con su audio para revisión manual.
- **Voz**: Piper local, voz sintética genérica `locutor_principal` (revisar la
  licencia del modelo antes de emitir). Audio normalizado con `loudnorm` (ffmpeg).
- **Ligada a la música**: solo suena justo antes de su canción y si esta está
  `ready`; tras emitirse se retira; si la canción sale de la caché, su intro también.
  Cuenta para el presupuesto de charla (si no cabe, la canción suena sola).
- **Coste**: tokens y euros de cada llamada (también las fallidas) en
  `producer_runs`; la regla de gasto (`budget.monthly_eur`) frena el productor.
- **Normalización de volumen**: la palabra se normaliza al producirla; la música de
  NPR no se toca y la emisora le aplica una ganancia por archivo en mpv (medida con
  `analyze-loudness`).

| Criterio (§12) | Estado | Cómo se comprueba |
|---|---|---|
| Test que rechaza un dato ausente de las fuentes | Hecho | `test_grounding.py::test_grounding_rejects_fact_absent_from_sources` (§9) y `test_fase2_acceptance.py::test_a_*` (el productor reintenta y cae a la versión sin dato) |
| El 100 % de las intros con dato tienen `claims` trazables | Hecho | `test_fase2_acceptance.py::test_b_*` (40 intros con un LLM que a veces responde mal) y `radio audit host_intro` sobre la BD |
| `radio preview host_intro` funciona con y sin fakes | Hecho (falta probarlo a mano con Claude y Piper reales) | `test_fase2_acceptance.py::test_c_*`: `--fake` de punta a punta; sin `--fake`, SDK de Anthropic sobre transporte simulado, `piper` y `mpv` falsos y fuentes con `MockTransport` |
| La descripción de NPR nunca llega al LLM (decisión de la dueña) | Hecho | `test_fase2_acceptance.py::test_d_*` |

`radio simulate` ejecuta `host_intro` con dobles (fuentes sintéticas, LLM pegado a
ellas y TTS falso) e informa de las unidades con intro; ninguna intro puede sonar sin
su canción detrás. Pendiente de verificar con proveedores y hardware reales: ver ADR
0003 (licencia de la voz, coste y comportamiento reales de Claude, fuentes con
artistas reales, velocidad de Piper en la Pi, ganancia por archivo en mpv).

## Producción de stock (`radio produce`)

- **Framework** (`producers/base.py`): protocolo `Producer` (`deficit`, `produce`) y
  plantilla `StagedProducer` con las etapas gather → write → validate → tts → post →
  register. `register` mueve el audio de `data/tmp/` a `data/stock/<kind>/` y solo
  entonces inserta la fila (y el `state_delta` de ficción) en una transacción.
- **Post** (`producers/post.py`): `ffmpeg` loudnorm en dos pasadas (objetivo
  `station.loudness_lufs`) + recorte de silencios; sin ffmpeg, se avisa y no se toca.
- **Registro** (`producers/registry.py`): `PRODUCERS` = nombre → fábrica
  (`time_signal`, `music_tinydesk`, `host_intro`).
- **`host_intro`**: ver la sección Fase 2 y ADR 0003.
- **`music_tinydesk`**: lee el feed RSS oficial de `producers.yaml →
  music_tinydesk.params.feed_url`: el feed oficial "Tiny Desk Concerts - Audio" de NPR,
  `https://feeds.npr.org/510306/podcast.xml` (decisión #8, resuelta). **Términos de NPR:
  uso personal y no comercial, sin modificar el contenido (`loudnorm: false`) y sin
  usarlo para construir o entrenar sistemas de IA.** Sin `feed_url` el productor falla
  con "feed_url no configurado (decisión abierta #8)". Solo episodios con enclosure de
  audio, deduplicados por `guid`, del más reciente al más antiguo, descargas
  secuenciales con `User-Agent` propio y GET condicional (ETag/Last-Modified guardados
  en `data/cache/feeds/`). Tope de caché LRU (`max_cache_items` / `max_cache_mb`):
  lo retirado pasa a `retired` y se borra el archivo (nunca los importados a mano).
- **systemd**: `deploy/radio-produce.service` (oneshot, `radio produce --all`) y
  `deploy/radio-produce.timer` (cada 15 min, `Persistent=true`), junto a
  `deploy/radio-station.service`. Ver `deploy/README.md`.

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
