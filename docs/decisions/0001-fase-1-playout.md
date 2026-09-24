# ADR 0001 — Fase 1: playout, producers entre segmentos y simulación

- Estado: aceptada; **sustituida en parte por 0002** (realineación con ARCHITECTURE.md)
- Fecha: 2026-09-24

> Nota: tras la realineación con ARCHITECTURE.md, la tabla `plays` pasa a ser `play_log`
> (§3.2), los estados `done`/`error` pasan a `retired`/`quarantined`, la BD es
> `data/state.db` y el audio va a `data/stock/<kind>/`. El producer `host_intro` descrito
> aquí se ha retirado hasta la Fase 2 (se rehará con grounding) y los producers se
> programan por `cron` en lugar de `interval_minutes`. Ver ADR 0002.

## Contexto

La Fase 1 tiene que poner la emisora en el aire de principio a fin: decidir qué suena,
elegir el audio concreto, emitirlo con mpv en una Raspberry Pi y generar señales
horarias e intervenciones de la locutora IA, todo verificable sin hardware ni red.

## Decisiones

### Scheduler: decide el tipo, no el segmento

`Scheduler.next_kind(now, history, available)` es lógica pura (ver docstring de
`core/scheduler.py`). Resumen de reglas, por prioridad:

1. Nunca un tipo sin stock.
2. Señal horaria en los minutos 0–4 de la hora local (cooldown 55 min); ignora el resto.
3. Cooldowns por tipo (factual 15, ficción 30, jingle 10, host_intro 12 min).
4. Racha de palabra ≤ `max_talk_run_min` de la franja (los jingles son transparentes).
5. Palabra en la hora móvil ≤ `min(talk_budget_ratio, 1 - music_ratio)` de la franja.
6. Jingle de transición tras música (20 %); palabra por pesos (factual > host_intro >
   ficción); si no, música; y como último recurso cualquier cosa antes que silencio.

### Playout: elige el segmento y registra la emisión

`core/playout.py` traduce el tipo en un segmento:

- `time_signal`: solo la etiquetada con la hora local en curso (`hour:YYYY-MM-DDTHH`);
  si no existe, se descarta el tipo y se vuelve a preguntar al scheduler.
- `music`: excluye el artista de la canción anterior. Si la señal de la próxima hora
  está lista, prefiere canciones que acaben antes del minuto 5 de esa hora. Sin esta
  regla, con canciones de 150–600 s, la simulación perdía 6–8 señales en 24 h (una
  canción larga empezada poco antes de en punto tapaba toda la ventana). Para ello
  `DB.pick_ready` acepta `max_duration_s`.
- Audio inexistente → segmento en `error` y reintento (máximo 6 intentos por paso).
- Tras emitirse, la palabra pasa a `done`; música y jingles siguen `ready` (rotación).
- Sin nada que emitir → audio de `assets/emergency/` (no se registra) o `None`.

Los tiempos de `plays` se toman con `clock.now()` antes y después de
`audio.play(path)`. En simulación, `SimAudioBackend` avanza el `FakeClock` la duración
del segmento (la busca en la BD por `audio_path`), así el Playout no sabe que está
simulado y `started_at`/`ended_at` salen correctos.

### Producers entre segmentos

`ProducerRunner.tick()` se ejecuta antes de cada segmento, en el mismo hilo. Es simple
y determinista; el coste es que un producer lento (LLM/TTS reales) retrasa el siguiente
segmento. Moverlo a un hilo en segundo plano queda para una fase posterior.

### Proveedores: registro solo con fakes

`providers/registry.py` expone `build_llm` / `build_tts`. Hoy solo existe `fake`;
cualquier otro nombre lanza `ProviderNotAvailable`. `radio station` lo captura, avisa y
emite solo música, en vez de fallar al arrancar.

### Simulación como red de seguridad

`radio simulate` (catálogo sintético de 250 canciones / 40 artistas, jingles, producers
con fakes, `random.Random(seed)`) comprueba invariantes duros y sale con código 1 si
alguno falla:

- 0 s de silencio;
- 0 canciones consecutivas del mismo artista;
- al menos `horas - 1` señales horarias (la primera hora no tiene señal preparada);
- 0 ejecuciones de producers con error.

También informa del reparto de antena, la palabra máxima en hora móvil frente al
presupuesto y una muestra de decisiones. CI ejecuta 6 h con semilla 1.

## Consecuencias

- Sin factual/ficción, la música ocupa ~98 % de la antena: la locutora está limitada
  por su cooldown de 12 min. El reparto 70–80 % llegará con esos producers.
- El orden de rotación depende de `created_at`/`id`; los informes de simulación son
  reproducibles bit a bit con la misma semilla.
