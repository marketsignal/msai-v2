# IB Gateway Troubleshooting

1. Check container health: `docker compose --env-file .env.prod -f docker-compose.prod.yml ps ib-gateway`.
2. Validate connectivity from backend: `nc -z ib-gateway 4002`.
3. Confirm credentials are present in secret store / `.env.prod` and that Compose is being run with `--env-file .env.prod`.
4. Review gateway logs for auth/session errors.
5. If 3 probe failures occur, stop live deployments and rotate credentials.
