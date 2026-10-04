# ADR 0002 — Realineación de la Fase 1 con ARCHITECTURE.md

- Estado: aceptada
- Fecha: 2026-09-24
- Sustituye en parte a: [0001](0001-fase-1-playout.md)

## Contexto

La primera Fase 1 (ADR 0001) se construyó antes de que existiera `ARCHITECTURE.md`. Tenía
un scheduler propio (`next_kind`), un playout bloqueante que emitía segmento a segmento
(`core/playout.py`) y una emisora que **ejecutaba productores entre canción y canción**
(`ProducerRunner` dentro de `radio station`). Eso contradice el invariante 2 (producción
y emisión desacopladas) y §4.4 (cola con lookahead, `play_log` al empezar y terminar,
watchdog). Este ADR recoge la realineación completa y las desviaciones que quedan.

## Qué cambia respecto a 0001

| 0001 | Ahora | Referencia |
|---|---|---|
| Tabla `plays`, estados `done`/`error`, `data/radio.db` | `play_log`, `retired`/`quarantined`, `data/state.db` (esquema v1 en `user_version`), audio en `data/stock/<kind>/` | §3, §11 |
| `Scheduler.next_kind` + elección en el playout | `grid.next_unit(state, stock, now, mode, rng)` → `PlayUnit` (función pura) | §4.3 |
| `core/playout.py`: `audio.play()` bloqueante por segmento | `radio.station.StationEngine`: dirigido por eventos `Started`/`Ended` de un backend con cola; lookahead de `playout.lookahead_units` (2) unidades por detrás de la que suena | §4.4 |
| Productores entre segmentos (`ProducerRunner.tick()` en la emisora) | La emisora **no** ejecuta productores, LLM, TTS ni red. Producción = `radio produce --all` (timer de systemd cada 15 min). `ProducerRunner` eliminado | inv. 2, §2 |
| Simulación con `SimAudioBackend` y el playout | `radio simulate` ejecuta **el mismo motor** con `FakeClock` + `FakeEventBackend(advance=clock.advance)`; la producción se modela aparte (`runner.produce` cada 15 min simulados) | §9 |
| Productor `host_intro` (locutora sin grounding) | Eliminado hasta la Fase 2 (se rehará con gather → write → grounding → tts → post) | §12 |
| Programación de productores por `interval_minutes` | `cron` de 5 campos en hora local (`producers.yaml`) | §5 |

### La emisora (§4.4)

Paquete `src/radio/station/` (§11 lo esbozaba como `player.py`, `queue.py`,
`mixer.py`): `engine.py` (motor), `queue.py` (espejo de la cola del reproductor) y
`service.py` (proceso real con mpv y señales). `mixer.py` llegará con la mezcla v2.

- **Lookahead.** Cada unidad se decide en su *hora proyectada de inicio* con un
  `SchedulerState` que es el `play_log` real (lo emitido y lo que suena) más lo ya
  planificado, añadido con `advance_state`. El scheduler ve lo que hay en cola:
  cooldowns, presupuesto de charla, artista anterior y §1.4 cuentan con ello. La
  palabra en cola se quita del stock que ve el scheduler.
- **`play_log`** se escribe al empezar (`Started`) y al terminar (`Ended`; `skipped` si
  se cortó o falló). La palabra que acaba entera pasa a `retired`; música, jingles y
  stingers siguen `ready`.
- **Audio ausente**: al encolar se comprueba el archivo; si falta, `quarantined` y se
  vuelve a decidir. Si desaparece estando ya en cola (la caché LRU de Tiny Desk puede
  borrarlo), mpv da `Ended(error)` sin `Started`: `quarantined` y se repone la cola. Un
  `error` a mitad de reproducción con el archivo presente (mpv murió) no pone en
  cuarentena.
- **Interrupciones** (señal horaria). Temporizador con `rules.next_interrupt_at`. A la
  hora, `next_unit` desde `now` con lo emitido de verdad; si devuelve `interrupt=True`
  y la cola no la tenía ya a tiempo, se vacía lo pendiente
  (`QueueingAudioBackend.clear_pending()`, `playlist-clear` en mpv) y la interrupción
  pasa delante. Lo que suena se deja acabar si termina dentro de `max_late_seconds`; si
  no, se corta la música cuando `station.interrupts.cut_music: true` (por defecto), y
  siempre el bucle de emergencia y la palabra o jingles que harían llegar tarde la
  interrupción. Con `cut_music: false`, la interrupción espera a que acabe la canción;
  si así llegaría más tarde que `max_late_seconds`, **se omite** (una señal horaria
  tardía sería falsa) y queda en el log.
- **Peldaño 5**: si no hay nada que emitir (o el scheduler falla), se encola el bucle
  de emergencia (`playout.emergency_dir`, `assets/emergency/emergency_loop.wav`),
  registrado en `play_log` con kind `emergency` y `segment_id` NULL; se reintenta
  programar a los `emergency_retry_s` s y, en cuanto hay stock, se corta el bucle.
- **Watchdog**: `MpvIpcBackend` relanza mpv; en cada `tick()` la emisora detecta el
  relanzamiento, reconcilia su cola con la del backend y lo registra.
- **Audio**: `station.yaml → audio.mpv_bin` y `audio.mpv_args` (dispositivo ALSA de la
  Pi, volumen…) se pasan a `MpvIpcBackend(extra_args=…)` (inv. 10).
- **Señales**: SIGINT/SIGTERM → se deja de programar, se cierra el backend (lo que suena
  se registra como `skipped`) y se cierra la BD.
- **Determinismo**: el motor no tiene hilos; con `FakeClock` + `FakeEventBackend` los
  tests y la simulación son reproducibles. Con mpv, `station/service.py` lo conduce
  desde el hilo principal (los eventos llegan del hilo lector a una bandeja).

### Ajuste del scheduler: relleno de jingles

Con conciertos de Tiny Desk (15–30 min) ninguna canción cabe antes de la señal horaria
y el scheduler encadenaba decenas de jingles de relleno durante los últimos minutos de
la hora. Ahora solo se rellena si faltan como mucho `FILLER_MAX_GAP_S` (60 s); si no,
se elige música y la emisora la corta en punto. `radio simulate --catalog tinydesk`
sirve para comprobarlo.

## Componentes adelantados a su fase

Se construyeron antes de la Fase 3 porque la emisora los necesitaba para no divergir de
§4.3 desde el principio. Quedan activos, sin coste:

- Scheduler completo de §4.3 (`grid/`): franjas, patrón, presupuesto de charla,
  cooldowns, interrupciones, §1.4, vinculación y escalera de §8.
- Productor `time_signal` (Fase 3): solo plantilla + TTS, sin LLM. Con el proveedor
  `fake` de Fase 1 genera silencio; la señal real llegará con el TTS de verdad.
- Huecos `jingle` en `grid.yaml` (Fase 3): sin jingles en `assets/jingles/` ni productor,
  el hueco baja de peldaño y suena música.

## Decisiones abiertas (§13)

- **#8 — Feed de Tiny Desk: resuelta.** `https://feeds.npr.org/510306/podcast.xml`
  ("Tiny Desk Concerts - Audio"). Términos de NPR: uso personal y no comercial; **sin
  modificar el contenido** → `loudnorm: false` para la música; **no usar el contenido
  para construir o entrenar sistemas de IA** → hay que revisarlo **antes de la Fase 2**,
  que prevé usar las descripciones de los episodios como fuente (*grounding*) del LLM
  del locutor. Cortar un concierto para dar la señal horaria es dejar de reproducirlo,
  no modificar el archivo; aun así es configurable (`interrupts.cut_music`).
  → *Revisado (2026-09-24), ver [ADR 0003](0003-fase-2-locutor.md):* la descripción del
  episodio **nunca** llega al LLM ni se guarda; el locutor usa solo fuentes abiertas
  (MusicBrainz CC0 + Wikipedia CC BY-SA) y el título del episodio como identificador.
- **#1 (nombre) y #2 (idioma): provisionales** en `station.yaml` (`name: "Radio Parra"`,
  `language: "es"`). La señal horaria usa `name`.
- **#4 (voz del locutor): abierta.** `voices.yaml` solo tiene una voz sintética genérica
  provisional (`provider_voice_id: "<pendiente>"`). → *Resuelta después (ADR 0003):*
  voz sintética genérica de Piper (`locutor_principal`); #3: TTS local Piper.
- #3, #5, #6, #7 y #9 siguen abiertas y no afectan a la Fase 1.

## Otras decisiones y desviaciones documentadas

- **Fechas en UTC.** La BD guarda ISO 8601 normalizado a UTC con microsegundos (la
  comparación de texto coincide con la cronológica). La parrilla razona en hora local de
  `grid.timezone` y hace la aritmética en UTC (cambios de hora).
- **`billable`** (no está en el protocolo de §4.2): un productor que no gasta en APIs
  (`music_tinydesk`) declara `billable = False` y la regla de gasto no le aplica.
- **`rotate_per_run`** en `music_tinydesk`: entran episodios nuevos en cada ejecución
  aunque no haya déficit (si no, con el stock lleno la música nunca se renovaría); el
  tope LRU de caché retira los más antiguos.
- **Caché del feed en disco** (`data/cache/feeds/`): ETag/Last-Modified y episodios
  descartados, para el GET condicional. Es regenerable y no está en §3.2.
- **`consent: StrictBool`** en `voices.yaml`: solo vale el booleano literal `true`
  (`"yes"` o `1` no cuentan como consentimiento, inv. 9).
- **`import-music`** existe solo como herramienta de desarrollo/offline; en la radio la
  música entra solo por el RSS oficial (§7).
- **`radio doctor`** informa pero sale siempre con código 0; `--network` es la única
  comprobación que usa la red.

## Consecuencias

- Invariante 2 verificable: un test comprueba que importar `radio.station` no carga
  `httpx`, `feedparser`, productores ni proveedores LLM/TTS, y otro emite horas con las
  conexiones AF_INET/AF_INET6 y `httpx` bloqueadas.
- `radio simulate` y `radio station` comparten el motor: lo que se afina en simulación
  es lo que suena.
- Criterios de aceptación de la Fase 1 (§12): ver la tabla del README.
