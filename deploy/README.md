# deploy/

Unidades systemd de ejemplo para la Raspberry Pi (ARCHITECTURE.md §10).

| Archivo                 | Qué es                                                     |
|-------------------------|------------------------------------------------------------|
| `radio-station.service` | La emisora (`radio station`): `Type=simple`, `Restart=always`. |
| `radio-produce.service` + `radio-produce.timer` | Jobs de producción. **Los aporta otro componente** (productores); no están aún en esta carpeta. |

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

Cuando existan `radio-produce.service`/`.timer`, se instalan igual (copiar a
`/etc/systemd/system/`, `daemon-reload`) y se activa **el timer**:
`sudo systemctl enable --now radio-produce.timer`.

## Notas

- **Sin red también suena** (§1 inv. 2): la unidad se ordena tras
  `network-online.target` pero no la requiere. Si en tu sistema algún servicio
  `*-wait-online` retrasa mucho el arranque sin red, puedes quitar ese `After=`.
- **Nunca silencio** (§1 inv. 3): systemd relanza la emisora a los 2 s si cae
  (`Restart=always`, sin límite de reinicios), y dentro de la emisora un watchdog
  relanza mpv si muere. Si no hay nada que emitir suena
  `assets/emergency/emergency_loop.wav`.
- **Audio:** un servicio de sistema no tiene sesión de PulseAudio/PipeWire; mpv sale
  por ALSA con el dispositivo por defecto (configurable en `/etc/asound.conf` o con
  un `~radio/.config/mpv/mpv.conf` con `ao=alsa` y `audio-device=...`). El usuario
  `radio` debe estar en el grupo `audio`.
- `radio doctor` comprueba que `mpv` y `ffmpeg` están instalados.
