# Docker — sviluppo macOS

Stack per provare **mirror_logger** (e opzionalmente **kvbm**) su Mac con Docker Desktop, senza Raspberry/Kvaser.

## Avvio rapido

```bash
# Solo mirror_logger (consigliato su Mac)
./scripts/docker-mac-up.sh mirror

# Stack completo (mirror + kvbm, build più lungo)
./scripts/docker-mac-up.sh full
```

## URL

| Servizio | URL |
|----------|-----|
| mirror_logger UI | http://127.0.0.1:5050 |
| mirror health | http://127.0.0.1:5050/api/health |
| kvbm (profilo `full`) | http://127.0.0.1:5001 |

Token API mirror (default Mac): `dev-mirror-mac` — header `X-Auth-Token`.

## Comandi utili

```bash
docker compose -f docker-compose.mac.yml logs -f mirror-logger
docker compose -f docker-compose.mac.yml ps
docker compose -f docker-compose.mac.yml down
```

## Note Mac

- `MIRROR_FAKE=1`: traffico CAN simulato (nessun gateway reale in Docker).
- `auto_activate_mirror` disabilitato in `user.json` generato dallo script.
- I log MF4 persistono nel volume Docker `trc-onboard-mac_mirror-logs`.
- Su **Raspberry/Linux** in produzione usare `install.sh --systemd` senza `MIRROR_FAKE`.

## Affidabilità (env in `docker/mac.env`)

Allineato alle modifiche per viaggio lontano: flush MF4 frequenti, retention disco, healthcheck, coda ampia.
