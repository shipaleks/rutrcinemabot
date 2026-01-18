# Deployment Guide

This guide explains how to deploy the Media Concierge Bot to Koyeb.

## Prerequisites

Before deploying, you need:

1. **Koyeb Account** - Sign up at [koyeb.com](https://www.koyeb.com/)
2. **GitHub Repository** - Push your code to GitHub
3. **API Keys** - Obtain all required API keys (see below)
4. **Telegram Bot Token** - Create bot via [@BotFather](https://t.me/botfather)

## Required API Keys

| Service | How to Get |
|---------|------------|
| Telegram Bot Token | Talk to [@BotFather](https://t.me/botfather), use `/newbot` command |
| Anthropic API Key | [console.anthropic.com](https://console.anthropic.com/settings/keys) |
| TMDB API Key | [themoviedb.org/settings/api](https://www.themoviedb.org/settings/api) |
| Kinopoisk API Token | [kinopoiskapiunofficial.tech](https://kinopoiskapiunofficial.tech/) |
| Encryption Key | Generate with command below |

**Generate Encryption Key:**
```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

## Deployment Steps

### Step 1: Prepare Your Repository

1. Push your code to GitHub:
```bash
git add .
git commit -m "Prepare for deployment"
git push origin main
```

2. Verify the repository contains:
   - `Dockerfile` (multi-stage build)
   - `koyeb.yaml` (deployment configuration)
   - `src/` directory with all code

### Step 2: Create Koyeb Secrets

In the Koyeb dashboard, go to **Settings > Secrets** and create these secrets:

| Secret Name | Value |
|-------------|-------|
| `telegram-bot-token` | Your Telegram bot token |
| `anthropic-api-key` | Your Anthropic API key |
| `tmdb-api-key` | Your TMDB API key |
| `kinopoisk-api-token` | Your Kinopoisk API token |
| `encryption-key` | Generated Fernet key |
| `webhook-url` | Will be set after app creation (see Step 4) |

### Step 3: Create Koyeb Service

**Option A: Using Koyeb Dashboard**

1. Go to [Koyeb Dashboard](https://app.koyeb.com/)
2. Click **Create App**
3. Select **GitHub** as deployment source
4. Connect your GitHub account and select the repository
5. Configure the service:
   - **Name**: `media-concierge-bot`
   - **Region**: Frankfurt (fra)
   - **Instance**: Nano (free tier)
   - **Builder**: Docker
   - **Dockerfile**: `Dockerfile`

**Option B: Using Koyeb CLI**

```bash
# Install Koyeb CLI
curl https://raw.githubusercontent.com/koyeb/koyeb-cli/master/install.sh | bash

# Login
koyeb login

# Deploy using koyeb.yaml
koyeb app create --definition koyeb.yaml
```

### Step 4: Configure Webhook URL

After the app is created:

1. Note your app URL: `https://media-concierge-bot-<your-org>.koyeb.app`
2. Go to **Koyeb Secrets** and update `webhook-url`:
   ```
   https://media-concierge-bot-<your-org>.koyeb.app
   ```
3. Redeploy the service for the change to take effect

### Step 5: Verify Deployment

1. **Check Health Endpoint:**
   ```bash
   curl https://media-concierge-bot-<your-org>.koyeb.app:8080/health
   ```
   Expected response:
   ```json
   {"status": "healthy", "service": "media-concierge-bot", "ready": true}
   ```

2. **Test the Bot:**
   - Open Telegram
   - Find your bot by username
   - Send `/start`
   - You should receive the welcome message

3. **Check Logs:**
   - Go to Koyeb Dashboard > Your App > Logs
   - Look for `bot_started` and `webhook_set` messages

## Configuration Reference

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | - | Telegram bot token from BotFather |
| `ANTHROPIC_API_KEY` | Yes | - | Anthropic API key for Claude |
| `TMDB_API_KEY` | Yes | - | TMDB API key |
| `KINOPOISK_API_TOKEN` | Yes | - | Kinopoisk API token |
| `ENCRYPTION_KEY` | Yes | - | Fernet encryption key |
| `WEBHOOK_URL` | Yes* | - | Full URL for Telegram webhook |
| `ENVIRONMENT` | No | `development` | `development` or `production` |
| `LOG_LEVEL` | No | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `PORT` | No | `8000` | Webhook server port |
| `HEALTH_PORT` | No | `8080` | Health check endpoint port |
| `WEBHOOK_PATH` | No | `/webhook` | Webhook endpoint path |
| `SEEDBOX_HOST` | No | - | Seedbox URL (optional) |
| `SEEDBOX_USER` | No | - | Seedbox username (optional) |
| `SEEDBOX_PASSWORD` | No | - | Seedbox password (optional) |

*Required for production deployment

### koyeb.yaml Reference

```yaml
name: media-concierge-bot

service:
  type: docker
  docker:
    dockerfile: Dockerfile
    context: .

  instance_type: nano
  regions:
    - fra

  scaling:
    min: 1
    max: 1

  ports:
    - port: 8080
      protocol: http
      path: /health
    - port: 8000
      protocol: http
      path: /webhook

  health_checks:
    - type: http
      port: 8080
      path: /health
      interval_seconds: 30
      timeout_seconds: 10
      healthy_threshold: 1
      unhealthy_threshold: 3
      grace_period_seconds: 60

  env:
    - key: TELEGRAM_BOT_TOKEN
      secret: telegram-bot-token
    # ... other environment variables
```

## Troubleshooting

### Bot Not Responding

1. **Check webhook registration:**
   ```bash
   curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"
   ```
   Verify `url` matches your Koyeb app URL.

2. **Check logs for errors:**
   - Koyeb Dashboard > Logs
   - Look for `error` level messages

3. **Verify secrets are set correctly:**
   - All secrets should show as "Set" in Koyeb dashboard

### Health Check Failing

1. **Check if app is starting:**
   - Look for startup logs in Koyeb dashboard
   - Verify no import errors

2. **Increase grace period:**
   - Edit `koyeb.yaml` and increase `grace_period_seconds`
   - Redeploy

3. **Check port configuration:**
   - Ensure `HEALTH_PORT` is `8080`
   - Verify health check path is `/health`

### Webhook Errors

1. **"Webhook was not set" error:**
   - Verify `WEBHOOK_URL` secret is correctly set
   - URL must include `https://`
   - URL must be accessible from the internet

2. **SSL/TLS errors:**
   - Koyeb provides valid SSL certificates
   - Ensure webhook URL uses `https://`

### Database Issues

1. **SQLite file not persisting:**
   - Koyeb instances are ephemeral
   - Data directory `/app/data` should use Koyeb volumes
   - Consider adding persistent volume configuration

2. **Migration errors:**
   - Check logs for migration output
   - Migrations are idempotent and safe to re-run

## Updating the Bot

To deploy updates:

1. **Push changes to GitHub:**
   ```bash
   git add .
   git commit -m "Update: description"
   git push origin main
   ```

2. **Automatic deployment:**
   - If auto-deploy is enabled, Koyeb will automatically rebuild
   - Watch the deployment progress in Koyeb dashboard

3. **Manual deployment:**
   - Go to Koyeb Dashboard > Your App
   - Click **Redeploy**

## Monitoring

### Logs

Koyeb provides log aggregation:
- Go to Dashboard > Your App > Logs
- Logs are in JSON format for easy parsing
- Filter by log level: `level:error`

### Metrics

Koyeb provides basic metrics:
- CPU usage
- Memory usage
- Request count
- Response time

### Alerts

Configure alerts in Koyeb:
1. Go to Settings > Alerts
2. Add alert for unhealthy instances
3. Configure notification (email, Slack, etc.)

## Cost Estimation

Koyeb Pricing (as of 2024):

| Instance Type | CPU | RAM | Price |
|---------------|-----|-----|-------|
| Nano | Shared | 256 MB | Free tier / $0.001/hour |
| Micro | Shared | 512 MB | $0.0035/hour |
| Small | 1 vCPU | 1 GB | $0.007/hour |

The bot runs well on Nano instance for personal use.

## Security Best Practices

1. **Never commit secrets to Git**
   - Use Koyeb Secrets for all sensitive data
   - Keep `.env` in `.gitignore`

2. **Use encryption for user data**
   - All OAuth tokens encrypted with Fernet
   - Encryption key stored as secret

3. **Keep dependencies updated**
   - Regularly update `pyproject.toml` dependencies
   - Check for security advisories

4. **Monitor for anomalies**
   - Watch logs for unusual patterns
   - Set up alerts for error spikes
