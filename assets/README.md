# assets/

Audio fijo de la emisora, versionado en git (ARCHITECTURE.md §10: entra en los backups).

| Carpeta      | Contenido                                                              |
|--------------|------------------------------------------------------------------------|
| `emergency/` | Bucle de emergencia (§8, peldaño 5). **Siempre presente** (§1 inv. 3). |
| `jingles/`   | Jingles por kind (vacío por ahora).                                    |
| `stingers/`  | Ráfagas cortas de transición (vacío por ahora).                        |

## `emergency/emergency_loop.wav`

- **Qué es:** colchón suave de acordes (Cmaj7 → Am7 → Fmaj7 → G6) con campanitas,
  24 s, WAV mono 22,05 kHz 16 bits (~1 MB). Empieza y acaba en silencio para que el
  bucle empalme sin clics.
- **Procedencia:** generado por síntesis con `scripts/generate_emergency.py` (solo
  biblioteca estándar de Python, determinista). No contiene muestras de terceros.
  Para regenerarlo: `uv run python scripts/generate_emergency.py`.
- **Licencia:** obra propia generada por código, dedicada al dominio público
  ([CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/deed.es)). Se puede
  emitir, copiar y modificar sin restricciones.
- **Garantía en CI:** `tests/unit/test_emergency_asset.py` falla si el archivo falta,
  no es un WAV válido o su duración se sale de 20–30 s.

La emisora (`Playout`) reproduce, rotando, cualquier audio de `emergency/` cuando no
hay nada más que emitir. Se pueden añadir más archivos aquí (con su licencia anotada
en este README).
