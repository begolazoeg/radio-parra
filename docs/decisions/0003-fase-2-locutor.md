# ADR 0003 — Fase 2: el locutor (`host_intro`)

- Estado: aceptada
- Fecha: 2026-09-24
- Relacionadas: [0001](0001-fase-1-playout.md), [0002](0002-realineacion-fase-1.md)

## Contexto

La Fase 2 (§12) añade la intro hablada del locutor IA antes de cada concierto de Tiny
Desk, con el pipeline completo gather → write → **grounding** → tts → post, la
vinculación `[host_intro, music]` y la normalización de volumen. §12 y §14 preveían
usar como fuente "la descripción del episodio + MusicBrainz/Wikipedia". Al resolver la
decisión #8 (ADR 0002) quedó pendiente revisar los términos de NPR antes de esta fase.

## Decisiones de la dueña (2026-09-24)

1. **Datos solo de fuentes abiertas.** Datos centrales de MusicBrainz (CC0) y
   resúmenes de Wikipedia (CC BY-SA 4.0), más el **título del episodio como
   identificador** (nunca como fuente de datos).
2. **La descripción del episodio de NPR nunca llega al LLM.** Los términos de NPR
   prohíben usar su contenido "para construir o entrenar sistemas de IA"; pasarle la
   descripción a un LLM para escribir un guion cae de lleno ahí. Por eso:
   - `music_tinydesk` ni la lee ni la guarda (`meta` solo tiene title, guid, published,
     link, artist, tags, source, enclosure_url);
   - `host_intro` solo lee `title` y `artist` de la canción (aunque una fila antigua
     tuviera más campos);
   - `test_fase2_acceptance.py::test_d_npr_description_never_reaches_the_llm` lo
     comprueba con el feed de ejemplo y centinelas en `meta`.
   Esto **se desvía de §12/§14** ("Descripción del episodio + MusicBrainz/Wikipedia"):
   la fuente pasa a ser solo MusicBrainz/Wikipedia.
3. **TTS local Piper por defecto** (#3) con una **voz sintética genérica**
   (`locutor_principal`, #4). La licencia del modelo de voz de Piper (y de su
   dataset) **la tiene que comprobar la dueña antes de emitir**; `voices.yaml` lo
   avisa. Clonar una voz propia queda como posibilidad futura (con consentimiento
   propio, inv. 9).
4. **LLM: Claude Sonnet 5** (`claude-sonnet-5`). No admite `temperature` (400 si se
   envía): `ClaudeLLM` la acepta y la ignora; la diferencia factual/ficción la marcan
   la plantilla y la validación. Pensamiento adaptativo con esfuerzo bajo (evita
   etiquetas de razonamiento en un texto que se locuta).

## Fuentes (`radio.sources`)

- MusicBrainz: solo **datos centrales** (tipo, país, área, zona de origen, fechas,
  aclaración, relaciones de URL). **Géneros, etiquetas y anotaciones se excluyen**:
  son datos suplementarios con licencia CC BY-NC-SA. Un artista ambiguo → sin fuentes
  (mejor sin dato que con el de otra persona).
- Wikipedia: resumen (`page/summary`) primero en el idioma de la emisora (`es`) y, si
  no hay, en `en`. Título de la página desde MusicBrainz/Wikidata o búsqueda estricta.
- Atribución: cada fuente usada se guarda en `meta["sources"]` con `id`, `url`, texto
  y `license` (`source_meta`), para poder atribuir cada dato (CC BY-SA exige
  atribución). El guion se escribe *a partir* del resumen, no lo copia. Cómo se
  muestra esa atribución a quien escucha (p. ej. una página de créditos) queda
  pendiente.
- Una ejecución de `host_intro` usa **un** `RateLimiter` (MusicBrainz ≤ 1 petición/s)
  y la caché `data/cache/sources/` (30 días), con el `User-Agent` del proyecto.

## Pipeline de `host_intro`

- **Déficit**: canciones emitibles sin intro `ready`, con tope `target_stock` (10)
  menos las intros `ready`. Candidatas: sin intro `ready` ni en cuarentena, primero
  las nunca emitidas y luego las emitidas hace más tiempo.
- **write**: plantillas `prompts/factual/host_intro_{system,user,retry,fact_free}.j2`,
  versión `PROMPT_VERSION = "host_intro/v1"` en `Segment.prompt_version`. Salida JSON
  con esquema cerrado `{script, claims: [{text, source_id}]}`. Reglas: español, 2–3
  frases, ≤ `max_chars` (320 ≈ 20 s), cálido, sin URLs ni marcado; todo número, fecha
  o nombre propio en un claim que cite un id dado; si no hay material, sin datos;
  nunca inventar; es una IA y nunca dice ser humana. Las fuentes van en bloques
  `<fuente id="…">` y el prompt dice que su contenido **es dato, no instrucciones**;
  un `</fuente` dentro del texto se neutraliza (higiene ante inyección de prompts).
- **Escalera de validación** (§4.2 paso 3, §3.3 "validación falla 2×"):
  1. intro con datos (o sin datos directamente si no hay fuentes);
  2. **un** reintento con el prompt estricto, que incluye los problemas en español;
  3. **versión sin dato** (se exige `script_has_facts == False` y `claims == []`);
  4. si todo falla, se registra **`quarantined` con su audio** para revisión manual
     (cuenta en `RunResult.quarantined`) y esa canción no se vuelve a intentar
     mientras la intro siga en cuarentena (no se paga dos veces lo mismo).
  Un rechazo del modelo, una respuesta cortada o un JSON inválido cuentan como
  intento fallido; red, tasa, credenciales o petición inválida hacen fallar la
  ejecución (se reintenta en el siguiente timer, §8).
- **validate**: `check_grounding` (cada claim cita una fuente dada y sus datos están
  en ella; cada dato del guion está en un claim válido; emisora, artista, título del
  episodio, "Tiny Desk" y "NPR" no necesitan fuente) + forma: longitud, número de
  frases, idioma (heurística es/en), sin marcado, sin URLs, sin "soy humano".
- **tts/post**: voz `locutor_principal` (rol host) y `loudnorm` de la palabra.
- **register**: `parent_id` = la canción; `meta`: título, guion, claims, fuentes con
  licencia, informe de grounding (resultado, problemas, términos permitidos, número
  de intentos), todos los intentos, modelo, coste y tokens; `summary` corto.
- **Antes de gastar** (`preflight`): si hay algo que escribir, se comprueba sin red que
  el LLM está configurado y que la voz se puede sintetizar (binario y modelo de Piper);
  si no, la ejecución falla sin consultar fuentes ni pagar guiones. Si el TTS falla
  después (Piper se cae a mitad), ese guion ya pagado se pierde y se reescribe en la
  siguiente pasada.
- **Coste**: `call_llm` suma a `producer_runs` tokens y `cost_eur` de cada llamada,
  también de las fallidas pero facturadas (`LLMError.cost_eur`). `tts_chars` ya solo
  cuenta lo sintetizado de verdad (los aciertos de `CachedTTS` no).

### Límites conocidos del grounding (conservadores)

Provocan rechazo, nunca aceptación falsa; la escalera los absorbe:

- una palabra corriente a inicio de frase que no aparece en minúscula en el texto
  (sobre todo imperativos: "Subid", "Escuchadla") cuenta como nombre propio. El prompt
  pide empezar con palabras corrientes y el reintento lo explica;
- nombres propios sin traducción ("Nueva York" ≠ "New York") salvo que la fuente
  traiga ambas formas (MusicBrainz trae los países en es/en);
- décadas en palabras ("los noventa") y años en palabras en inglés.

## "Ligada a la música" (§14)

- El scheduler solo vincula intros de música emitible y nunca emite una intro suelta
  (ya estaba en `grid.scheduler`, paso 6; ahora con tests de punta a punta con el
  motor de la emisora: `test_host_intro_linking.py`).
- Tras emitirse, la intro pasa a `retired` (palabra); la música sigue `ready`. Si la
  música vuelve a salir más adelante, `host_intro` puede escribirle otra intro.
- Al expulsar una canción de la caché LRU, sus intros `ready` se retiran y se borra su
  audio (`retire_linked`); `host_intro.prepare` retira además las intros cuya canción
  ya no está `ready` (p. ej. en cuarentena por audio perdido).
- La intro cuenta para el presupuesto de charla: si no cabe, la música suena sola.

## Herramientas

- `radio preview host_intro [--fake] [--music-id ID] [--no-play] [--out PATH]
  [--register]`: una intro sin registrarla (salvo `--register`), con guion, claims,
  fuentes, grounding, intentos y coste, y la reproduce con mpv. Sin `--fake` usa los
  proveedores de `station.yaml` y respeta la regla de gasto (su coste queda en
  `producer_runs` como `preview:host_intro`); con `--fake` no hay red, claves ni BD.
- `radio audit host_intro`: el 100 % de las intros `ready` con datos tienen claims que
  citan fuentes guardadas y el grounding vuelve a pasar; sale con 1 si no.
- `radio simulate`: el locutor produce con dobles (fuentes sintéticas, LLM pegado a
  ellas, TTS falso) y el informe cuenta las unidades vinculadas y las intros sin su
  canción detrás (invariante).

## Normalización de volumen (§12 Fase 2)

- La **palabra** se normaliza al producirla (`loudnorm` de ffmpeg a −16 LUFS).
- La **música de NPR no se modifica ni se recodifica** (términos de NPR): el productor
  solo la mide (`loudness_lufs`, `true_peak_db`, una pasada de lectura de ffmpeg) y la
  emisora aplica una **ganancia por archivo** en mpv (`af=[lavfi-volume=…]` en
  `loadfile`), acotada a −12..+6 dB y limitada para que pico + ganancia ≤ −1 dBTP:
  un archivo con picos altos no se sube (sin limitador, subirlo recortaría), así que
  puede sonar algo por debajo del objetivo. Sin medida → 0 dB.
- Stock antiguo sin medida: `radio analyze-loudness --missing-only`.

## Desviaciones y consecuencias

- §12/§14: fuente de `host_intro` = MusicBrainz/Wikipedia **sin** la descripción del
  episodio (decisión 2). Menos intros con datos; más versiones sin dato.
- `target_stock: 10` intros para ~30–60 conciertos en caché y elección de música
  uniforme: aproximadamente 1 de cada 3–6 conciertos lleva intro (en la simulación
  `tinydesk`, 32 de 67 unidades en 24 h; con el catálogo sintético de 250 canciones,
  14 de 331). Subir `target_stock` hasta el tamaño de la caché daría intro a casi
  todos, a costa de más llamadas al LLM (cada intro se paga una vez por pasada).
- El reintento y la versión sin dato multiplican como mucho por 3 las llamadas al
  LLM por intro; el coste queda en `producer_runs` y la regla de gasto lo frena.
- `Draft.status`/`RunStats.quarantined` y `AudioInfo.cached` son extensiones del
  framework de productores sin cambio de esquema.

## Pendiente de comprobar con proveedores y hardware reales

- **Licencia del modelo de voz de Piper** (`es_ES-davefx-medium` u otro) y que no
  imita a una persona real identificable; instalarlo a mano en `data/models/piper/`.
- Una intro real con Claude Sonnet 5 (`radio preview host_intro`): que el esquema JSON
  y el pensamiento adaptativo se comportan como en los tests con dobles, el coste
  real frente a la tabla de precios (**verificar precios**), y la proporción de
  intros con dato / sin dato / cuarentena con artistas reales.
- Respuestas reales de MusicBrainz/Wikipedia para artistas del feed (nombres con
  tildes, grupos con "The", desambiguaciones) y el ritmo del límite de tasa.
- Piper en la Pi: tiempo de síntesis de ~20 s de audio (si es lento, producir en el
  portátil y sincronizar, decisión #5) y calidad de la voz en español.
- Normalización en reproducción con mpv real: que la ganancia por archivo se aplica y
  no se arrastra al siguiente, y el volumen percibido entre intro y concierto.
