# IB Gateway Config

This folder is no longer mounted by default.

For the `ghcr.io/gnzsnz/ib-gateway` image we use environment variables plus a
persistent `TWS_SETTINGS_PATH` volume under `data/ib-gateway`.

If you ever need a fully custom IBC config, mount a file directly to:

- `/home/ibgateway/ibc/config.ini`

and only do that together with `CUSTOM_CONFIG=yes`.
