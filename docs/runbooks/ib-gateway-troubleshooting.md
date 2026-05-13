# IB Gateway Troubleshooting Runbook

Common issues and resolutions for the Interactive Brokers Gateway container.

## Architecture

The IB Gateway runs as a Docker container (`ib-gateway` service in `docker-compose.prod.yml`) using the `ghcr.io/gnzsnz/ib-gateway:stable` image. It exposes:

- Port 4004: IB API (paper trading — socat proxy to internal 127.0.0.1:4002)
- Port 5900: VNC for visual debugging

## Issue 1: Connection Drops

**Symptoms**: Backend logs show `IB Gateway connection lost` or `ConnectionError` when placing orders.

**Diagnosis**:

```bash
# Check if the container is running
docker compose -f docker-compose.prod.yml ps ib-gateway

# Check container logs
docker compose -f docker-compose.prod.yml logs ib-gateway --tail=50

# Test connectivity from the backend container
docker compose -f docker-compose.prod.yml exec backend \
  python -c "import socket; s=socket.create_connection(('ib-gateway', 4004), timeout=5); print('OK'); s.close()"
```

**Resolution**:

1. Restart the IB Gateway container:

   ```bash
   docker compose -f docker-compose.prod.yml restart ib-gateway
   ```

2. If the restart does not fix it, check if IB is performing weekend maintenance (typically Saturday 00:00 - Sunday 17:00 ET). The gateway will reconnect automatically when IB servers come back.

3. If connections continue to drop during market hours, check the IB system status page: https://www.interactivebrokers.com/en/trading/systemStatus.php

## Issue 2: Credential Rotation

**When to rotate**: If credentials are compromised, or as part of regular security hygiene (recommended quarterly).

**Procedure**:

1. Log in to the IB Account Management portal and change the password.

2. Update the `.env` file on the VM:

   ```bash
   # Edit the .env file
   nano /home/msai/.env
   # Update TWS_USERID and/or TWS_PASSWORD
   ```

3. Restart the IB Gateway container to pick up new credentials:

   ```bash
   docker compose -f docker-compose.prod.yml restart ib-gateway
   ```

4. Verify the gateway connects successfully:
   ```bash
   docker compose -f docker-compose.prod.yml logs ib-gateway --tail=20
   # Look for "IB Gateway is ready" or similar success message
   ```

**Important**: IB enforces a login session limit. If you see `Login failed: Second login attempt`, it means another session is active. Close the other session first (e.g., TWS desktop app or another gateway instance).

## Issue 3: TWS Restart / Daily Reset

IB Gateway performs a daily restart (typically around 23:45 ET on weekdays). During this window (approximately 5-10 minutes), the API is unavailable.

**Expected behavior**: The container automatically restarts and reconnects. The `restart: unless-stopped` policy in `docker-compose.prod.yml` handles this.

**If auto-restart fails**:

```bash
# Check the container status
docker compose -f docker-compose.prod.yml ps ib-gateway

# If the container is in "Exited" state, start it manually
docker compose -f docker-compose.prod.yml up -d ib-gateway

# If the container is restarting in a loop, check logs for the root cause
docker compose -f docker-compose.prod.yml logs ib-gateway --tail=100
```

**Mitigation for strategies**: Strategies should handle `ConnectionError` gracefully and implement retry logic with exponential backoff. Do not place orders during the daily restart window.

## Issue 4: Paper vs. Live Mode Mismatch

**Symptom**: Orders rejected or unexpected account data.

**Check current mode**:

```bash
docker compose -f docker-compose.prod.yml exec ib-gateway env | grep TRADING_MODE
```

The `docker-compose.prod.yml` sets `TRADING_MODE: paper` by default. To switch to live trading:

1. Update `docker-compose.prod.yml` to set `TRADING_MODE: live`
2. Ensure the TWS credentials are for a funded live account
3. Restart the gateway:
   ```bash
   docker compose -f docker-compose.prod.yml up -d ib-gateway
   ```

**Warning**: Always test strategies thoroughly in paper mode before switching to live. There is no undo for live trades.

## Issue 5: VNC Debugging

For visual inspection of the gateway UI:

```bash
# Connect via VNC client to port 5900
# On macOS:
open vnc://VM_IP:5900

# On Linux:
vncviewer VM_IP:5900
```

The VNC session shows the IB Gateway desktop interface, which is useful for:

- Verifying login status
- Checking error dialogs that only appear in the GUI
- Confirming the account and trading mode

## Quick Reference

| Symptom              | Likely Cause                     | Fix                                   |
| -------------------- | -------------------------------- | ------------------------------------- |
| `ConnectionError`    | Container down or IB maintenance | Restart container or wait             |
| `Login failed`       | Bad credentials or session limit | Check `.env`, close other sessions    |
| Orders rejected      | Paper/live mismatch              | Verify `TRADING_MODE`                 |
| Gateway restart loop | IB daily reset or image issue    | Check logs, wait 10 min, then restart |
| No market data       | Data subscription missing        | Check IB account data subscriptions   |
