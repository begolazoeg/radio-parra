# deploy/

Unidades systemd de ejemplo para la Raspberry Pi (ARCHITECTURE.md §10).

| Archivo                 | Qué es                                                     |
|-------------------------|------------------------------------------------------------|
| `radio-station.service` | La emisora (`radio station`): `Type=simple`, `Restart=always`. |
| `radio-produce.service` + `radio-produce.timer` | Jobs de producción (`radio produce --all`) cada 15 min. `Type=oneshot`, baja prioridad. |

Son los dos mundos de ARCHITECTURE.md §2: la emisora **no** produce ni usa la red
(invariante 2); el timer rellena el stock en `data/` y la emisora lo lee.

## Instalación de la emisora

Se asume el checkout en `/opt/radio-parra` y un usuario de sistema `radio`. Si usas
otra ruta, edita `WorkingDirectory`, `EnvironmentFile` y `ExecStart` en la unidad.

```bash
# 1. Dependencias del sistema y usuario
sudo apt install mpv ffmpeg
sudo useradd --system --create-home --groups audio radio
sudo chown -R radio:radio /opt/radio-parra

# 2. Entorno Python (crea /opt/radio-parra/.venv con el comando `radio`)
cd /opt/radio-parra
sudo -u radio uv sync
sudo -u radio cp .env.example .env      # opcional: claves de API

# 3. Instalar y arrancar la unidad
sudo cp deploy/radio-station.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now radio-station.service

# 4. Comprobar
systemctl status radio-station
journalctl -u radio-station -f
```

## Instalación de la producción

`radio-produce.service` es un ejemplo con marcadores: sustituye `<RADIO_DIR>` (p. ej.
`/opt/radio-parra`) y `<UV>` (ruta de `uv`) antes de copiarlo. Se activa **el timer**,
no el servicio:

```bash
sudo cp deploy/radio-produce.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now radio-produce.timer
systemctl list-timers radio-produce.timer
journalctl -u radio-produce.service     # una entrada por pasada
```

La primera pasada con red descarga episodios de Tiny Desk (`music_tinydesk`, hasta
`max_per_run` por pasada); hasta entonces la emisora suena con el bucle de emergencia.
`radio stock` muestra el stock frente a su objetivo.

## Notas

- **Sin red también suena** (§1 inv. 2): la unidad se ordena tras
  `network-online.target` pero no la requiere. Si en tu sistema algún servicio
  `*-wait-online` retrasa mucho el arranque sin red, puedes quitar ese `After=`.
- **Nunca silencio** (§1 inv. 3): systemd relanza la emisora a los 2 s si cae
  (`Restart=always`, sin límite de reinicios), y dentro de la emisora un watchdog
  relanza mpv si muere. Si no hay nada que emitir suena
  `assets/emergency/emergency_loop.wav`.
- **Audio:** un servicio de sistema no tiene sesión de PulseAudio/PipeWire; mpv sale
  por ALSA con el dispositivo por defecto. El dispositivo se elige en
  `config/station.yaml → audio.mpv_args` (p. ej.
  `["--ao=alsa", "--audio-device=alsa/plughw:CARD=sndrpihifiberry"]`; `mpv
  --audio-device=help` lista los disponibles) o en `/etc/asound.conf`. El usuario
  `radio` debe estar en el grupo `audio`.
- **Señal horaria:** con `station.yaml → interrupts.cut_music: true` (por defecto) la
  emisora corta la canción en curso a la hora en punto; con `false` espera a que acabe
  (y si llegaría tarde, la omite).
- `radio doctor` comprueba mpv, ffmpeg, permisos y espacio de `data/`, el esquema de
  `state.db`, el bucle de emergencia, el feed y los productores activos.
