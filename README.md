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

## Estructura

```
src/radio/      # paquete principal
config/         # station.yaml, grid.yaml, voices.yaml, producers.yaml
prompts/        # plantillas Jinja2 para LLM
universe/       # estado de universo de ficción
assets/         # jingles, stingers, emergency
data/           # generado en runtime (en .gitignore)
tests/          # unit + fixtures
```
