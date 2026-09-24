# Radio Parra — Arquitectura maestra (v0.1)

> Nombre provisional. Radio casera para una Raspberry Pi (y para el portátil en desarrollo) que combina
> música (Tiny Desk), un locutor generado con IA, segmentos informativos reales y segmentos de ficción declarada.
> Este documento es la fuente de verdad del diseño. Si el código y este documento discrepan, se para y se discute
> antes de seguir (ver `CLAUDE.md`).

---

## 0. Cómo usar este documento

- Se construye **por fases verticales** (sección 12). Cada fase deja algo que suena y que funciona de punta a punta.
- Los **invariantes** (sección 1) no se negocian sin abrir un ADR en `docs/decisions/`.
- Las **decisiones abiertas** (sección 13) se resuelven con la persona dueña del proyecto, no se asumen.
- Todo lo que dependa de servicios externos (LLM, TTS, feeds) va detrás de una interfaz y tiene un *fake* para tests.

---

## 1. Principios e invariantes

1. **El reproductor es tonto.** Consume una cola de archivos de audio ya listos. No sabe qué es un clima, una noticia
   ni una crónica deportiva.
2. **Producción y emisión están desacopladas.** Los *productores* generan audio por adelantado (jobs puntuales, en lote).
   La *emisora* solo programa y reproduce desde disco. Si se cae internet, la radio sigue sonando.
3. **Nunca silencio.** Existe una escalera de degradación (sección 8) que termina en un bucle de emergencia local.
4. **Factual y ficción no se mezclan.** Cada segmento declara `factual: true|false`. Los factuales se generan
   *pegados a fuentes*; los de ficción, con libertad creativa. Son plantillas, parámetros y voces distintas.
5. **Todo dato verificable viene de una fuente.** Un segmento factual guarda las fuentes con las que se escribió
   y pasa un validador antes de estar `ready`. Si no hay material, se hace una versión sin dato, nunca un dato inventado.
6. **Proveedores intercambiables.** LLM, TTS y reproducción de audio son interfaces. Cambiar de proveedor no toca la lógica.
7. **Estado explícito y reproducible.** Estado en SQLite, configuración en YAML, semillas aleatorias registradas,
   reloj inyectable. El scheduler es una función casi pura y testeable.
8. **Charla acotada.** La proporción de voz hablada sobre el total tiene un tope configurable (por defecto ~22 % en ventana de 60 min).
9. **Consentimiento y legalidad por diseño.** Ninguna voz de persona real sin `consent: true` en `voices.yaml`;
   ninguna persona real identificable como personaje de ficción; la música solo por vías legítimas (RSS oficial de podcast).
10. **Mismo código en portátil y en Pi.** Lo único que cambia es configuración y adaptadores de hardware.

---

## 2. Vista general

```
                    ┌──────────────────────────── OFFLINE / EN LOTE (cron o systemd timers) ─────────────────────────────┐
  Fuentes           │                                                                                                    │
  ────────          │   ┌────────────┐   gather    ┌──────────┐  write   ┌───────────┐ validate ┌─────┐  post  ┌───────┐ │
  RSS Tiny Desk ───►│   │ Productor  │────────────►│ Material │─────────►│  Guion    │─────────►│ TTS │───────►│ ffmpeg│ │
  Open-Meteo        │   │ (uno por   │             │ + fuentes│  (LLM)   │ (JSON)    │ (groun-  └─────┘        └───┬───┘ │
  Wikimedia         │   │  kind)     │             └──────────┘          └───────────┘  ding)                       │     │
  Calendario        │   └─────┬──────┘                                                                              │     │
  Telegram (inbox)  │         │ lee estado/semillas/memoria                                                         ▼     │
  Sensores (MQTT)   │         ▼                                                                       registra Segment   │
                    │   ┌───────────┐                                                                  (archivo atómico  │
                    │   │  Universo │  bibles YAML + estado SQLite                                      + fila SQLite)    │
                    │   └───────────┘                                                                        │           │
                    └────────────────────────────────────────────────────────────────────────────────────────┼───────────┘
                                                                                                               ▼
                                                                                     ┌──────────────────────────────────┐
                                                                                     │  data/  (stock de audio + SQLite)│
                                                                                     └───────────────┬──────────────────┘
                    ┌───────────────────────────── ONLINE / SIEMPRE ENCENDIDO ──────────────────────┼──────────────────┐
                    │                                                                               ▼                  │
                    │   ┌────────────┐  próximo segmento   ┌──────────────┐   cola (lookahead 2-3)  ┌──────────────┐  │
                    │   │ Adaptadores│────modo/skip───────►│  Scheduler   │────────────────────────►│   Player     │──┼──► altavoz
                    │   │ GPIO / API │                     │  (parrilla)  │                         │ (mpv IPC)    │  │
                    │   └────────────┘                     └──────┬───────┘                         └──────┬───────┘  │
                    │                                             └────────── play_log ◄────────────────────┘          │
                    └───────────────────────────────────────────────────────────────────────────────────────────────────┘
```

Dos "mundos":

| | Productores | Emisora (`station`) |
|---|---|---|
| Ciclo de vida | Jobs de una sola ejecución (CLI) | Proceso de larga duración |
| Red | Sí | No la necesita |
| Escribe | `segments`, `universe_state`, archivos de audio | `play_log` |
| Lee | Fuentes externas, bibles, memoria | `segments`, config de parrilla |
| Falla → | Se reintenta o se degrada; la radio sigue | Nunca debe caerse (supervisada por systemd) |

---

## 3. Modelo de datos

### 3.1 `Segment` (núcleo del sistema)

```python
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

Status = Literal["ready", "pending_review", "quarantined", "expired", "retired"]

@dataclass(frozen=True)
class Segment:
    id: str                      # ULID
    kind: str                    # "music" | "host_intro" | "weather" | "consultorio" | ...
    factual: bool                # separa datos reales de ficción
    path: Path                   # audio ya listo en disco
    duration_s: float
    created_at: datetime
    producer: str                # nombre del productor que lo creó
    status: Status = "ready"
    expires_at: datetime | None = None   # noticias sí; poema clásico no
    priority: int = 0            # >0 puede interrumpir (p. ej. señal horaria)
    parent_id: str | None = None # host_intro -> música a la que precede
    voice_id: str | None = None
    prompt_version: str | None = None
    summary: str | None = None   # resumen corto: alimenta la memoria de ficción
    meta: dict = field(default_factory=dict)  # fuentes, semillas, modelo, etc.
```

### 3.2 Esquema SQLite (WAL activado)

```sql
CREATE TABLE segments (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, factual INTEGER NOT NULL,
  status TEXT NOT NULL, path TEXT NOT NULL, duration_s REAL NOT NULL,
  created_at TEXT NOT NULL, expires_at TEXT, priority INTEGER DEFAULT 0,
  parent_id TEXT REFERENCES segments(id), voice_id TEXT,
  producer TEXT NOT NULL, prompt_version TEXT, summary TEXT, meta_json TEXT
);
CREATE INDEX idx_segments_kind_status ON segments(kind, status);

CREATE TABLE play_log (
  id INTEGER PRIMARY KEY, segment_id TEXT REFERENCES segments(id),
  kind TEXT NOT NULL, mode TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, skipped INTEGER DEFAULT 0
);

CREATE TABLE producer_runs (
  id INTEGER PRIMARY KEY, producer TEXT NOT NULL,
  started_at TEXT NOT NULL, ended_at TEXT, ok INTEGER,
  n_segments INTEGER, tokens_in INTEGER, tokens_out INTEGER,
  tts_chars INTEGER, cost_eur REAL, error TEXT
);

CREATE TABLE universe_state (        -- estado vivo de la ficción (liga, pueblo, radionovela...)
  universe TEXT PRIMARY KEY, version INTEGER NOT NULL,
  state_json TEXT NOT NULL, updated_at TEXT NOT NULL
);

CREATE TABLE signals (               -- lecturas de sensores (humedad de la parra, etc.)
  source TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, at TEXT NOT NULL
);

CREATE TABLE inbox (                 -- dedicatorias y mensajes de voz entrantes
  id INTEGER PRIMARY KEY, channel TEXT NOT NULL, sender TEXT NOT NULL,
  payload TEXT NOT NULL, status TEXT NOT NULL,   -- pending|approved|rejected|used
  created_at TEXT NOT NULL
);
```

### 3.3 Ciclo de vida de un segmento

```
draft ──validación OK──► ready ──emitido──► (queda en stock según política de reuso)
  │                        │
  │                        └── expires_at vencido ──► expired ──► GC
  ├── validación falla 2× ──► quarantined (revisión manual)
  └── requiere moderación ──► pending_review ──aprobado──► ready
```

**Escritura atómica:** el audio se genera en `data/tmp/`, se mueve (`rename`) a `data/stock/<kind>/` y solo entonces se inserta la fila.
Nunca debe existir una fila `ready` que apunte a un archivo incompleto.

---

## 4. Componentes

### 4.1 Proveedores (`providers/`)

```python
class LLM(Protocol):
    def complete(self, system: str, user: str, *, temperature: float,
                 json_schema: dict | None = None, max_tokens: int = 1000) -> LLMResult: ...

class TTS(Protocol):
    def synthesize(self, text: str, voice: Voice, out_path: Path) -> AudioInfo: ...

class AudioBackend(Protocol):       # solo lo usa la emisora
    def play(self, path: Path) -> None: ...
    def enqueue(self, path: Path) -> None: ...
    def skip(self) -> None: ...
```

- **LLM:** implementación con la API de Claude. Temperatura baja para factual, alta para ficción. Salida estructurada (JSON validado con pydantic).
- **TTS:** dos implementaciones desde el inicio, `cloud` (p. ej. ElevenLabs u otro) y `local` (Piper, gratis, offline). Cache por `hash(texto + voz + proveedor)` para no pagar dos veces lo mismo.
- **Fakes:** `FakeLLM` (devuelve fixtures) y `FakeTTS` (genera un tono/silencio de duración proporcional al texto). Todos los tests corren con fakes, sin red ni claves.

### 4.2 Productores (`producers/`)

Un productor = una clase = un `kind`. Mismo pipeline por etapas, cada etapa testeable por separado:

```python
class Producer(Protocol):
    name: str
    kind: str
    factual: bool
    target_stock: int                 # cuántos segmentos `ready` quiere mantener
    def deficit(self, stock: StockView, now: datetime) -> int: ...
    def produce(self, ctx: ProducerContext) -> list[Segment]: ...
```

Pipeline estándar (`producers/base.py` ofrece la plantilla):

1. **gather**: consulta fuentes → `list[SourceDoc]` (con `id`, `text`, `url`). *(Ficción: semillas + bible + memoria en lugar de fuentes.)*
2. **write**: LLM escribe el guion como JSON (`script`, y en factual también `claims: [{text, source_id}]`; en ficción `state_delta`).
3. **validate**: 
   - factual → *grounding check* (números, fechas y nombres propios del guion deben aparecer en las fuentes; cada `claim` apunta a una fuente real). Un reintento con prompt más estricto; si falla, `quarantined` o versión sin dato.
   - ficción → filtro de temas prohibidos (bible) y de personas reales.
   - ambos → duración objetivo (± tolerancia), idioma, sin marcado raro.
4. **tts**: voz según `voices.yaml` y el rol del segmento.
5. **post**: `ffmpeg` (normalización `loudnorm` ≈ −16 LUFS, recorte de silencios, jingle de entrada/salida si el kind lo pide).
6. **register**: mover archivo, insertar `Segment`, aplicar `state_delta` de ficción **en la misma transacción**.

Regla de gasto: antes de producir, comprobar `producer_runs` contra `budget.monthly_eur`; si se excede, el productor se salta y lo registra.

### 4.3 Scheduler / parrilla (`grid/`)

Función casi pura:

```python
def next_unit(state: SchedulerState, stock: StockView, now: datetime,
              mode: str, rng: random.Random) -> PlayUnit: ...
```

`PlayUnit` = lista ordenada de segmentos que se emiten juntos (p. ej. `[host_intro, music]`, `[jingle_fiction, consultorio, jingle_fiction]`).

Algoritmo (en orden):

1. **Interrupciones** (`priority > 0` o reglas `interrupts` de la parrilla): p. ej. señal horaria a las `:00` si no se emitió esa hora.
2. **Franja horaria** (`daypart`) según `now` y `mode` → `pattern` cíclico de huecos (`music`, `talk`) y `talk_pool` con pesos por `kind`.
3. **Presupuesto de charla**: si la ventana móvil supera `max_ratio`, el hueco `talk` se convierte en `music`.
4. **Filtros de candidatos**: `status=ready`, no caducado, *cooldown* por kind, no repetido recientemente, sin mezclar ficción justo después de factual (debe haber música o jingle entre medias).
5. **Elección**: aleatoria ponderada con `rng` sembrado (reproducible en tests).
6. **Vinculación**: si el segmento elegido es música y tiene un `host_intro` hijo disponible, el `PlayUnit` los incluye juntos.
7. **Escalera de degradación** si algo falla (sección 8).

Modos ("emisoras"): `default`, `tinydesk` (solo música + locutor), `informativo`, `random`… Se cambian por API o por el dial físico.

### 4.4 Emisora / Player (`station/`)

- Proceso único (`radio station`) con: bucle de programación, `AudioBackend` (mpv con `--input-ipc-server`), servidor de control local.
- **Lookahead de 2-3 unidades** en cola para evitar huecos y para que un fallo puntual del scheduler no se note.
- **Transiciones:**
  - v1: cortes limpios enmascarados por jingles/stingers de cada kind.
  - v2: mezcla precocinada con `ffmpeg` (crossfade y *ducking*) hecha al encolar, no en directo.
- Escribe `play_log` al empezar y terminar cada segmento (necesario para cooldowns, presupuesto de charla y "qué sonó").
- Watchdog: si mpv muere, se relanza y se reanuda con la siguiente unidad.

### 4.5 Adaptadores y control (`adapters/`)

- **API local** (FastAPI, solo `127.0.0.1`): `GET /status`, `GET /stock`, `POST /skip`, `POST /mode/{name}`, `POST /inbox` (dedicatorias).
- **GPIO** (`gpiozero`): encoder rotatorio para cambiar de modo (con "estática" al girar), botón para skip. Solo se carga si hay hardware.
- **Telegram** (bot con lista blanca de chat ids): escribe en `inbox` con `status=pending`. Nada se emite sin aprobación.
- **Sensores** (MQTT o HTTP): escribe en `signals`. El productor `parra_report` lee de ahí.

---

## 5. Configuración

```
config/
  station.yaml     # zona horaria, idioma, rutas, presupuesto, proveedores, loudness objetivo
  grid.yaml        # modos, franjas horarias, patrones, pesos, interrupciones, cooldowns
  voices.yaml      # voces y su consentimiento
  producers.yaml   # por productor: activo, target_stock, cron, parámetros
universe/
  consultorio.yaml, liga.yaml, pueblo.yaml, ...   # bibles (estáticas, versionadas en git)
prompts/
  factual/*.j2  ficcion/*.j2                       # plantillas Jinja versionadas
```

Ejemplo `grid.yaml`:

```yaml
timezone: Europe/Madrid
talk_budget: { window_minutes: 60, max_ratio: 0.22 }
cooldowns_minutes: { consultorio: 90, horoscope: 720, weather: 180 }

modes:
  default:
    interrupts:
      - kind: time_signal
        when: "minute == 0"
        max_late_seconds: 90
    dayparts:
      - name: manana
        from: "07:00"
        to: "12:00"
        pattern: [music, talk, music, music]
        talk_pool: { weather: 3, ephemeris: 2, horoscope: 2, word_of_day: 1 }
      - name: tarde
        from: "12:00"
        to: "20:00"
        pattern: [music, music, talk]
        talk_pool: { consultorio: 3, liga: 2, artist_fact: 2, trivia: 1 }
      - name: noche
        from: "20:00"
        to: "07:00"
        pattern: [music, music, music, talk]
        talk_pool: { radionovela: 3, interview: 2 }
  tinydesk:
    dayparts: [{ name: todo, from: "00:00", to: "24:00", pattern: [music], talk_pool: {} }]
```

Ejemplo `voices.yaml`:

```yaml
voices:
  - id: locutor_principal
    role: host
    provider: cloud
    provider_voice_id: "<pendiente>"
    language: es
    consent: true
    consent_note: "Voz sintética genérica del proveedor"
  - id: bego_clon
    role: host
    provider: cloud
    provider_voice_id: "<pendiente>"
    consent: true
    consent_note: "Voz propia, consentimiento propio"
  - id: dona_remedios
    role: character
    universe: consultorio
    provider: cloud
    provider_voice_id: "<pendiente>"
    consent: true
    consent_note: "Voz sintética genérica"
```

Regla de carga: `voices.yaml` falla al validar si una voz no tiene `consent: true` y `consent_note`.

Los secretos van en `.env` (nunca en git). Estructura de `.env.example` incluida en el repo.

---

## 6. Universos de ficción

Cada formato de ficción es un **universo** con tres capas:

1. **Bible** (`universe/*.yaml`, estática): personaje, tics, tono, reglas, temas prohibidos, banco de semillas.
2. **Estado** (`universe_state`, dinámico): clasificación de la liga, alcalde del pueblo, capítulo de la radionovela, rivalidades.
3. **Memoria corta**: `summary` de los últimos 10-15 segmentos del formato, para instruir "no repitas premisas ni chistes de estos".

Flujo de generación de un segmento de ficción:

```
semillas (2-3 al azar del banco, registradas en meta)
  + bible + estado + memoria corta
    → LLM (temperatura alta, salida JSON: {script, summary, state_delta})
      → validar (temas prohibidos, personas reales, duración 45-90 s)
        → TTS (voz del personaje) → post (jingle propio de entrada/salida)
          → registrar segmento + aplicar state_delta (misma transacción)
```

Ejemplo `universe/consultorio.yaml`:

```yaml
personaje: "Doña Remedios, 70 años, ex-peluquera, cero filtro, gran corazón"
tics: ["empieza siempre con 'hija mía'", "cita un refrán que se inventa"]
tono: "consejos terribles dichos con total seguridad; de vez en cuando, un destello de sabiduría real"
prohibido: ["personas reales", "salud y enfermedad", "duelo y muerte", "violencia"]
duracion_objetivo_s: [45, 90]
semillas:
  problemas: ["mi pareja ama más a su monstera que a mí", "me han dejado en visto los astros"]
  elementos_surrealistas: ["un semáforo", "una nube de paso", "un cubito de hielo"]
jingle: assets/jingles/consultorio.mp3
```

Formatos previstos: `consultorio`, `liga` (deporte inventado con estado persistente), `interview` (personajes ficticios o inanimados, incluida la parra),
`horoscope`, `pueblo` (informativo de un pueblo inexistente), `radionovela`, `teletienda`, `lost_found`, `contest`, `ads_parody`.

---

## 7. Guardarraíles

| Riesgo | Medida |
|---|---|
| Datos inventados presentados como reales | Segmentos factuales solo con `SourceDoc`s; grounding check; sin material → sin dato |
| Ficción confundida con información | `factual=False`, `kind` propio, voz y jingles distintos, nunca pegada a factual sin música/jingle |
| Voces de personas reales | `consent: true` obligatorio; no clonar famosos; personajes propios como alternativa |
| Personas reales en ficción | Prohibido en bible + filtro en validación; cameos de amistades solo con permiso explícito y guion revisado |
| Dedicatorias con contenido inapropiado | `inbox` con moderación; lista blanca de remitentes; nada al aire sin aprobar |
| Música | Solo el feed RSS oficial de audio (verificar URL y términos al implementar). Nada de scraping de YouTube |
| Exceso de charla | Presupuesto de charla + cooldowns por kind |
| Gasto descontrolado | `budget.monthly_eur` + `producer_runs` con coste por ejecución |
| Fugas de secretos | `.env` fuera de git, `pre-commit` con detección de secretos |

---

## 8. Fallos y degradación

Escalera del scheduler (baja un peldaño solo si el anterior no da candidato):

1. Selección ideal según parrilla.
2. Relajar cooldowns.
3. Cualquier música `ready`.
4. Repetir algo emitido antes (el más antiguo primero).
5. **Bucle de emergencia** (`assets/emergency/`, versionado y siempre presente). La radio nunca queda muda.

Otros fallos previstos:

- Job de producción falla → se registra en `producer_runs`, no toca lo existente, se reintenta en el siguiente timer.
- TTS o LLM caídos → el stock cubre; alerta en `radio status` si el stock de un kind baja del umbral.
- Disco lleno → GC por caducidad + tope de tamaño de caché de música (LRU).
- Reloj sin sincronizar (Pi sin red al arrancar) → la parrilla usa el reloj del sistema; se registra la advertencia.

---

## 9. Testing y herramientas de desarrollo

**Herramientas de CLI que hacen la vida fácil (implementar pronto):**

- `radio simulate --hours 24 --seed 1` → simula la parrilla **sin audio** y pinta una línea de tiempo (qué suena, cuándo, % de charla). Es la herramienta principal para afinar `grid.yaml`.
- `radio preview <producer> [--fake]` → genera un solo segmento y lo reproduce en local. Para iterar prompts y voces.
- `radio stock` → stock por kind frente a su objetivo, caducidades próximas.
- `radio produce [<producer>|--all] [--dry-run]` → rellena huecos de stock.
- `radio doctor` → comprueba ffmpeg, mpv, claves, feeds, permisos de escritura, espacio.
- `radio station` → arranca la emisora.
- `radio inbox list|approve|reject` → moderación de dedicatorias.

**Estrategia de tests:**

- Unitarios del scheduler con reloj y stock falsos. Propiedades a verificar: nunca devuelve vacío; nunca supera el presupuesto de charla; nunca pone ficción pegada a factual; respeta cooldowns; es determinista con la misma semilla.
- Productores con `FakeLLM`/`FakeTTS` y fixtures de fuentes. Test de grounding: un guion con un dato que no está en las fuentes **debe** ser rechazado.
- Test de contrato para cada proveedor (misma suite corre contra el fake y, opcionalmente y a mano, contra el real).
- Integración: `simulate` de 48 h con stock sintético termina sin huecos.
- CI: `ruff`, `mypy` (en `core/` y `grid/` como mínimo), `pytest`.

---

## 10. Despliegue

- **Desarrollo:** portátil con `uv`, `ffmpeg`, `mpv`. Audio por el dispositivo por defecto.
- **Pi:** Raspberry Pi (Zero 2 W como mínimo; 4/5 si van a usar TTS local con voces pesadas). Audio por I2S (p. ej. MAX98357A) o salida USB.
- `deploy/` incluye:
  - `radio-station.service` (systemd, `Restart=always`).
  - `radio-produce.service` + `radio-produce.timer` (uno por productor o uno global).
  - Script de provisión (`ffmpeg`, `mpv`, usuario, directorios, permisos).
- **Producción en otra máquina** (opcional): los productores pueden correr en el portátil y sincronizar `data/stock/` + SQLite a la Pi con `rsync`. Es útil si el TTS local es demasiado pesado para la Pi.
- **Backups:** solo `config/`, `universe/`, `assets/` y `state.db` (el stock de audio es regenerable, salvo lo aprobado a mano).

---

## 11. Estructura del repositorio

```
radio-parra/
├── CLAUDE.md
├── ARCHITECTURE.md
├── README.md
├── pyproject.toml            # uv; Python 3.12
├── Makefile                  # make test | lint | sim | run
├── .env.example
├── config/                   # station.yaml, grid.yaml, voices.yaml, producers.yaml
├── universe/                 # bibles de ficción
├── prompts/
│   ├── factual/
│   └── ficcion/
├── assets/
│   ├── jingles/  stingers/  emergency/
├── src/radio/
│   ├── core/                 # models, config (pydantic), store (sqlite), clock, logging, ids
│   ├── providers/
│   │   ├── llm/              # claude.py, fake.py
│   │   ├── tts/              # cloud.py, piper.py, fake.py
│   │   └── audio/            # mpv.py, null.py (simulación)
│   ├── producers/
│   │   ├── base.py           # pipeline por etapas
│   │   ├── music_tinydesk.py
│   │   ├── host_intro.py
│   │   ├── time_signal.py
│   │   ├── weather.py  ephemeris.py  word_of_day.py  sky.py  agenda.py  news.py
│   │   ├── dedication.py  voicemail.py  parra_report.py
│   │   └── fiction/          # consultorio.py, liga.py, interview.py, horoscope.py, ...
│   ├── grounding/            # extracción y verificación de afirmaciones
│   ├── grid/                 # scheduler.py, rules.py, budget.py
│   ├── station/              # player.py, queue.py, mixer.py (v2)
│   ├── adapters/             # api.py, gpio.py, telegram.py, sensors.py
│   └── cli.py                # typer
├── tests/
│   ├── fixtures/
│   └── unit/  integration/
├── docs/
│   └── decisions/            # ADRs (0001-...md)
├── deploy/
└── data/                     # gitignored: stock/, cache/, tmp/, state.db
```

Stack sugerido: Python 3.12, `uv`, `pydantic` v2, `typer`, `jinja2`, `httpx`, `feedparser`, `mutagen`, `fastapi`+`uvicorn`, `gpiozero`, `pytest`, `ruff`.
Sin dependencias nuevas sin justificarlas en el PR.

---

## 12. Roadmap por fases

Cada fase termina en una rama/PR con **criterios de aceptación** verificables.

### Fase 0 — Esqueleto y herramientas
- Repo, `pyproject`, CI, `Makefile`, config con pydantic, `store.py` + migraciones, `Clock`, modelos.
- Interfaces de proveedores y sus fakes.
- CLI mínima: `doctor`, `stock`.
- **Aceptación:** `make test` verde; `radio doctor` informa del entorno; un segmento de prueba se registra y se lista.

### Fase 1 — Música y emisora mínima
- Productor `music_tinydesk` (feed RSS → caché → `Segment`), con deduplicación por `guid` y tope de caché.
- Scheduler v0 (solo música, mezcla aleatoria) + `station` con mpv por IPC + `play_log`.
- `radio simulate` v0.
- Servicio systemd de ejemplo.
- **Aceptación:** en el portátil se enchufa y suena música en bucle; cortando la red sigue sonando; `simulate` produce una línea de tiempo de 24 h.

### Fase 2 — Locutor (intros)
- Productor `host_intro` con pipeline completo (gather → write → **grounding** → tts → post).
- Fuentes: descripción del episodio + MusicBrainz/Wikipedia (respetar `User-Agent` y límites de tasa).
- Vinculación `parent_id` y `PlayUnit` `[host_intro, music]`. Normalización de volumen.
- **Aceptación:** test que rechaza un dato ausente de las fuentes; el 100 % de las intros con dato tienen `claims` trazables; `radio preview host_intro` funciona con y sin fakes.

### Fase 3 — Parrilla real y ambiente
- `grid.yaml` con franjas, patrones, pesos, interrupciones, cooldowns y presupuesto de charla.
- `time_signal` y jingles/stingers (identificadores de emisora).
- Productores factuales baratos: `weather` (Open-Meteo), `ephemeris` (feed "On this day" de Wikimedia; verificar idioma disponible), `sky` (`astral`).
- **Aceptación:** `simulate` de 48 h cumple todas las propiedades del scheduler; la charla no supera el tope; la señal horaria salta a las `:00`.

### Fase 4 — Ficción
- Infraestructura de universos (bible + estado + memoria corta + semillas).
- `consultorio` primero (menos estado), después `liga` (con clasificación persistente) y `horoscope`.
- Separación factual/ficción reforzada en el scheduler.
- **Aceptación:** dos ejecuciones seguidas de `consultorio` no repiten premisa; `liga` actualiza clasificación de forma consistente tras cada crónica; el test de "ficción nunca pegada a factual" pasa.

### Fase 5 — Contenido personal
- `inbox` + bot de Telegram + moderación por CLI; productores `dedication` y `voicemail`.
- `news` (RSS + resumen anclado al titular/entradilla, con caducidad corta).
- `agenda` (calendario) y `birthday`.
- **Aceptación:** nada llega al aire con `status != ready`; las noticias caducan; la agenda usa solo datos del calendario.

### Fase 6 — Hardware y mundo físico
- Portar a Raspberry Pi; `deploy/` y provisión.
- Adaptador GPIO (encoder = modo, botón = skip), API de control.
- Sensor de la parra → `signals` → productor `parra_report` (datos reales, voz de personaje) y `interview` con la parra.
- Mezcla v2 con `ffmpeg` (crossfade / ducking) si el corte limpio se queda corto.
- **Aceptación:** arranque en frío en la Pi sin intervención; cambio de modo por dial; 7 días seguidos sin silencio.

---

## 13. Decisiones abiertas (resolver con la dueña del proyecto)

1. **Nombre de la emisora** (afecta jingles y prompts).
2. **Idioma(s)** de la radio: ¿solo español, o alternar con catalán? (afecta TTS, prompts y fuentes).
3. **Proveedor de TTS** principal (nube vs. Piper local) y presupuesto mensual.
4. **Voz del locutor:** sintética genérica o voz propia clonada (con consentimiento propio).
5. **Dónde se producen los segmentos:** en la propia Pi o en el portátil con sincronización.
6. **Fuentes de noticias** concretas (2-3 medios con RSS).
7. **Calendario y datos personales:** qué se conecta y qué queda fuera de la radio.
8. **Feed exacto de Tiny Desk** en audio (verificar URL y términos de uso en el momento de implementarlo).
   → *Resuelta (2026-09-24):* `https://feeds.npr.org/510306/podcast.xml` ("Tiny Desk Concerts - Audio").
   Términos de NPR: uso personal y no comercial, sin modificar el contenido, sin usarlo para construir o
   entrenar sistemas de IA. Pendiente de revisar antes de la Fase 2 (descripciones del episodio como fuente del LLM).
9. **Deporte inventado** y su bible de reglas (para `liga`).

---

## 14. Catálogo de segmentos

| kind | factual | Fuente / semilla | Caducidad | Prioridad | Fase |
|---|---|---|---|---|---|
| `music` | — | RSS Tiny Desk | nunca | 0 | 1 |
| `host_intro` | sí | Descripción del episodio + MusicBrainz/Wikipedia | ligada a la música | 0 | 2 |
| `time_signal` | sí | Reloj | n/a (plantilla) | alta | 3 |
| `jingle` / `stinger` | — | `assets/` | nunca | 0 | 3 |
| `weather` | sí | Open-Meteo | ~6 h | 0 | 3 |
| `ephemeris` | sí | Wikimedia "On this day" | 24 h | 0 | 3 |
| `sky` | sí | `astral` (cálculo) | 24 h | 0 | 3 |
| `word_of_day` | sí | Diccionario/Wiktionary | 24 h | 0 | 3-5 |
| `consultorio` | no | Bible + semillas + memoria | nunca | 0 | 4 |
| `liga` | no | Bible + estado persistente | ~7 d | 0 | 4 |
| `horoscope` | no | Bible + semillas | 24 h | 0 | 4 |
| `interview` | no | Bible + semillas | nunca | 0 | 4-6 |
| `pueblo`, `radionovela`, `teletienda`, `lost_found`, `contest`, `ads_parody` | no | Bibles | variable | 0 | 4+ |
| `news` | sí | RSS (titular + entradilla) | ~4 h | 0 | 5 |
| `agenda`, `birthday` | sí | Calendario / contactos | 24 h | 0 | 5 |
| `dedication`, `voicemail` | sí (contenido de terceros) | `inbox` aprobado | según uso | 0 | 5 |
| `serial_classic` | sí | Obras de dominio público | nunca | 0 | 5+ |
| `parra_report` | sí (datos) + voz de personaje | `signals` (sensor) | ~12 h | 0 | 6 |

Nota: `parra_report` es un híbrido; los datos son reales (se rige por el grounding factual) y solo la voz es de personaje.
