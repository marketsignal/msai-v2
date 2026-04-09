# VM Setup Runbook

1. Provision Ubuntu 24.04 VM (D4s_v5 minimum) with managed identity enabled.
2. Install Docker Engine + Docker Compose plugin.
3. Install `uv`, `node`, and Azure CLI.
4. Clone repo into `/opt/msai-v2`.
5. Copy [`.env.prod.example`](/Users/pablomarin/Code/msai-v2/codex-version/.env.prod.example) to `.env.prod` and fill in DB, Redis, Entra, Databento, and IB credentials.
6. Run `docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build`.
7. Verify `/health` and `/ready` endpoints before enabling trading.
