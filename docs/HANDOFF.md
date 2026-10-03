# Traspaso: continuar Radio Parra en local (tras la Fase 2)

Estado a 2026-10-03:
- Fases 0 y 1 mergeadas en `main` (PR #1 y #2).
- Fase 2 en el PR #3 (rama `claude/focused-ritchie-sd6zzt`), abierto y con CI en verde.
  **No mergear hasta verificarlo a mano** (ver TAREA 1).

## Preparar el repo

```bash
git clone https://github.com/begolazoeg/radio-parra.git   # si aún no lo tienes
cd radio-parra
git fetch origin
git checkout claude/focused-ritchie-sd6zzt
git pull
```

Abre la carpeta en VS Code, abre Claude Code y pega el prompt de abajo.

## Prompt para Claude Code

````markdown
# Contexto: Radio Parra — continuar tras la Fase 2

Eres mi asistente en el repo `begolazoeg/radio-parra`: una radio casera para Raspberry Pi con
conciertos Tiny Desk de NPR, un locutor IA, informativos con fuentes verificables y ficción
declarada. Respóndeme en castellano. Razona a fondo antes de actuar.

## Fuente de verdad y reglas de trabajo
- `ARCHITECTURE.md` es la fuente de verdad del diseño. Si el código y el documento discrepan,
  PARA y coméntamelo antes de seguir.
- Los invariantes (§1) no se cambian sin un ADR en `docs/decisions/`. Hay tres:
  0001 (fase 1, en parte sustituida), 0002 (realineación de la fase 1) y 0003 (fase 2, locutor).
  Léelos antes de tocar nada.
- Las decisiones abiertas (§13) las decido yo, no se suponen. Pregúntame.
- `ARCHITECTURE.md` cita un `CLAUDE.md` que todavía NO existe. Propónme crearlo (reglas del repo,
  comandos, convenciones) en cuanto tengas contexto, pero no lo crees sin preguntarme.
- Todo lo externo (LLM, TTS, feeds, fuentes) va detrás de una interfaz con fake. Los tests nunca
  usan red ni claves reales. Los tests con proveedores reales están marcados
  `@pytest.mark.real_provider` y solo corren con `RADIO_REAL_PROVIDERS=1`.
- Comentarios y docstrings en castellano, con el estilo del código que ya existe.
- No añadas dependencias sin justificarlas en el commit o PR.
- Antes de cada commit tienen que pasar todas estas comprobaciones:
  `uv run ruff check src tests && uv run mypy src/radio && uv run pytest -q && uv run radio simulate --hours 24 --seed 1`
- Nunca hagas push a `main` ni merge de un PR sin que yo lo apruebe explícitamente.

## Estado actual
- Fase 0 y Fase 1 están mergeadas en `main`: PR #1 y PR #2 (squash, commit `fcc076e`).
- La Fase 2 está en el PR #3, ABIERTO y con CI en verde, rama `claude/focused-ritchie-sd6zzt`:
  https://github.com/begolazoeg/radio-parra/pull/3
  Tiene 721 tests en verde y 13 omitidos (proveedores reales). La simulación de 24 h y la de 48 h
  pasan. NO lo mergees hasta que hayamos verificado a mano lo que sigue.

### Arquitectura en dos mundos (invariante 2)
- Productores: jobs puntuales con `radio produce [NAME|--all]`, lanzados por el timer de systemd
  (`deploy/radio-produce.{service,timer}`). Usan red, LLM y TTS.
- Emisora: proceso de larga duración (`radio station`, `src/radio/station/`). NUNCA importa
  productores, LLM, TTS ni código de red (hay un test que lo comprueba). Usa mpv por JSON-IPC con
  lookahead de 2 unidades, escribe `play_log` al empezar y al terminar, corta en punto para la
  señal horaria (configurable con `station.interrupts.cut_music`) y tiene un bucle de emergencia
  versionado (`assets/emergency/emergency_loop.wav`).
- Parrilla (§4.3, `src/radio/grid/`): `next_unit()` casi pura. Tiene dayparts, patrones,
  interrupciones, presupuesto de charla del 22 % y escalera de degradación §8. Enlaza
  `[host_intro, música]`.
- `radio simulate` usa el mismo motor que la emisora (`--timeline`, `--json`,
  `--catalog tinydesk`).

### Decisiones mías ya tomadas (respétalas)
- #8 Música: feed oficial de NPR `https://feeds.npr.org/510306/podcast.xml`. Términos de NPR:
  uso personal y no comercial, sin modificar el contenido (la música NO se recodifica:
  `loudnorm: false`) y sin usarlo para construir ni entrenar sistemas de IA.
- Fuentes del locutor: SOLO MusicBrainz (datos centrales, CC0) y Wikipedia (CC BY-SA 4.0), más el
  título del episodio. La descripción del episodio de NPR NUNCA llega al LLM y ya ni se guarda.
  Los géneros y etiquetas de MusicBrainz están excluidos (son CC BY-NC-SA).
- #3 TTS: Piper local por defecto. ElevenLabs está implementado pero desactivado.
- #4 Voz: sintética genérica `locutor_principal`, modelo Piper `es_ES-davefx-medium`.
  Su LICENCIA aún NO está verificada.
- LLM: Claude Sonnet 5 (`claude-sonnet-5`, SDK oficial `anthropic` 1.x).
  - Sonnet 5 RECHAZA `temperature`, `top_p` y `top_k` con un 400; `ClaudeLLM` no los envía.
  - La salida estructurada va por `output_config.format` con `json_schema`.
  - Pensamiento adaptativo con esfuerzo bajo.
  - Para cualquier cambio en el código de Claude API, consulta la documentación actual: no
    supongas parámetros de memoria.
- Nombre ("Radio Parra") e idioma ("es") son configuración provisional en `station.yaml`
  (#1 y #2 siguen abiertas).

### Fase 2: qué hay
- Productor `host_intro`. Etapas: gather (MusicBrainz + Wikipedia, ≤ 1 petición/s, caché en
  `data/cache/sources/`) → write (JSON `{script, claims}`) → validate (`src/radio/grounding/`)
  → tts (Piper) → post (loudnorm de voz) → register con `parent_id` de la canción.
- Escalera de fallos: reintento con un prompt más estricto → versión sin datos → `quarantined`.
- `preflight()`: no se paga a Claude si la voz no se puede sintetizar.
- Volumen de la música: se mide con `ffmpeg ebur128`, solo lectura, y mpv aplica la ganancia por
  archivo al reproducir (`af=[lavfi-volume=…]`, sintaxis distinta en mpv < 0.38 y ≥ 0.38).
- CLI: `radio preview host_intro [--fake] [--no-play] [--register]`, `radio audit host_intro`,
  `radio analyze-loudness`, `radio doctor`.

## TAREA 1: acompañarme en la verificación real (lo primero)
Guíame paso a paso, comprueba cada salida conmigo y arregla lo que falle (en la rama del PR #3):
1. Requisitos: `mpv` y `ffmpeg` instalados (brew o apt), `uv sync --extra dev`, y el binario
   `piper` (github.com/rhasspy/piper).
2. Voz: revisa CONMIGO la licencia de `es_ES-davefx-medium` en la página de voces de Piper. Si no
   sirve, propón otra voz española con licencia clara y cambia `provider_voice_id` en
   `config/voices.yaml`. Los archivos `.onnx` y `.onnx.json` van en `data/models/piper/`.
3. Clave: yo creo `.env` desde `.env.example` con `ANTHROPIC_API_KEY`. NUNCA me pidas la clave ni
   la escribas en ningún archivo ni en el chat. Ahora mismo la app NO carga `.env` sola; en cada
   terminal hay que hacer `set -a; . ./.env; set +a`. Propónme añadir la carga automática de
   `.env` en la CLI sin dependencias nuevas (no sobrescribir variables ya definidas, con test) y
   hazlo si te digo que sí.
4. `uv run radio doctor`: interpreta la salida y resuelve lo que falte.
5. `uv run radio produce music_tinydesk`: comprueba que baja episodios reales de NPR, que la
   deduplicación por `guid` funciona, que se mide la sonoridad y que el archivo queda
   byte-idéntico.
6. `uv run radio preview host_intro --no-play` (y luego sin `--no-play`). Revisa el guion, los
   claims, las fuentes, el grounding y el coste en €. Comprueba el coste real frente a la tabla de
   precios de `src/radio/providers/llm/claude.py` (marcada "verify") y el tipo de cambio
   `usd_to_eur`. Haz unas cuantas previews con artistas reales y dime qué proporción sale con
   datos, sin datos o en cuarentena. El grounding es conservador a propósito: rechaza nombres que
   no sabe traducir ("Nueva York"/"New York") y algunos verbos al principio de frase. Si rechaza
   demasiado, propón mejoras SIN abrir la puerta a datos sin fuente.
7. `uv run radio produce --all` y luego `uv run radio station` durante un rato. Verifica:
   - que mpv acepta el `loadfile` con opciones por archivo en mi versión
     (`mpv --version`; la forma cambia a partir de 0.38);
   - que la ganancia por archivo no deja huecos ni chasquidos entre pistas;
   - el volumen de la intro frente al concierto;
   - la señal horaria en punto;
   - `play_log`;
   - Ctrl+C limpio.
8. `uv run radio audit host_intro` debe salir con 0.
Al terminar, actualiza la tabla "Pendiente de verificar" del PR #3 y del README con lo que se haya
confirmado.

## Puntos abiertos que tienes que plantearme (no decidas solo)
- Atribución CC BY-SA de Wikipedia a los oyentes: las URLs ya se guardan en `meta.sources`. ¿Cómo
  se muestra (web local, `radio status`, mención hablada)?
- `host_intro.target_stock: 10`: con la música elegida al azar, solo 1 de cada 3–6 conciertos
  tiene intro. ¿Subirlo (más llamadas al LLM)?
- Si Piper falla después de escribir el guion, ese guion pagado se pierde. ¿Guardar los
  borradores pagados para reintentar solo el TTS?
- `radio doctor` siempre sale con código 0 aunque haya ERROR. ¿Lo cambio?
- Decisiones abiertas de §13 que siguen pendientes: #1 nombre, #2 idioma (¿català?), #5 dónde se
  produce (Pi o portátil + rsync), #6 fuentes de noticias, #7 calendario, #9 deporte inventado.

## TAREA 2: después, cerrar la Fase 2
Cuando la verificación real esté bien y yo lo apruebe: commits de ajustes en la rama, CI en verde
y merge squash del PR #3 SOLO cuando te lo diga. Después reinicia la rama de trabajo desde `main`.

## TAREA 3: preparar la Fase 3 (§12), sin empezar a programar hasta que yo apruebe el plan
Parte ya está hecha: la parrilla completa de §4.3, `time_signal` y el slot de jingles (ADR 0002).
Falta:
- jingles y stingers reales en `assets/` (¿generados o grabados? pregúntame la licencia);
- `weather` (Open-Meteo);
- `ephemeris` (Wikimedia "On this day": verificar el idioma disponible y la licencia);
- `sky` (`astral`, dependencia nueva que hay que justificar).
Criterio de aceptación: la simulación de 48 h cumple todas las propiedades del scheduler, la
charla no supera el tope y la señal horaria salta a las :00.
Propónme un plan y las preguntas de §13 que bloqueen esta fase (ubicación para el tiempo y el
cielo, idioma, etc.) antes de escribir código.

Empieza leyendo `ARCHITECTURE.md`, `docs/decisions/0001–0003`, `README.md`, este archivo
(`docs/HANDOFF.md`) y el PR #3. Luego confírmame en pocas líneas que entiendes el estado y dime
cuál es el primer paso de la TAREA 1.
````
