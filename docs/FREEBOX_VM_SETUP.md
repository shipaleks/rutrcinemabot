# Freebox VM Setup Guide

This guide explains how to set up automatic sync from your seedbox to Freebox Ultra NAS.

## Overview

The sync system:
1. Runs every 30 minutes on a Freebox VM
2. Rsyncs completed downloads from seedbox
3. Sorts files into Movies/TV Shows folders
4. Notifies the bot when files are ready
5. Cleans up the seedbox

## Prerequisites

- Freebox Ultra or Delta (with VM support)
- Ultra.cc seedbox account (see [SEEDBOX_SETUP.md](SEEDBOX_SETUP.md))
- SSH access to your seedbox

## 1. Create a Freebox VM

1. Open Freebox OS: https://mafreebox.free.fr
2. Go to **VMs** section
3. Click **Create a new VM**
4. Configure:
   - **Name**: `mediabot`
   - **System**: Debian 12
   - **RAM**: 1 GB
   - **Disk**: 16 GB
   - **Network**: Bridge mode
   - Check **Access to Freebox disk**
5. Create and start the VM

## 2. Initial VM Setup

Connect to the VM via Freebox OS console or SSH.

### Install Dependencies

```bash
sudo apt update
sudo apt install -y rsync sshpass curl jq cron
```

### Create User and Directories

```bash
# Create dedicated user
sudo useradd -m -s /bin/bash mediabot

# Create sync directories
sudo mkdir -p /home/mediabot/sync/logs
sudo chown -R mediabot:mediabot /home/mediabot
```

### Mount Freebox Storage

The Freebox disk should be automatically available at `/mnt/Freebox` or similar.
Verify with:

```bash
ls /mnt/Freebox/Space/
```

If not mounted, add to `/etc/fstab`:
```
//mafreebox.free.fr/Space /mnt/Freebox/Space cifs credentials=/home/mediabot/.smbcredentials,uid=mediabot,gid=mediabot 0 0
```

## 3. Install Sync Scripts

### Copy Scripts

From the bot repository, copy the scripts:

```bash
# As mediabot user
sudo -u mediabot -i

# Create directory structure
mkdir -p ~/sync/logs

# Download scripts (or copy from repo)
# Option 1: Clone repo
git clone https://github.com/YOUR_REPO/media-concierge-bot.git /tmp/bot
cp /tmp/bot/scripts/sync_seedbox.sh ~/sync/
cp /tmp/bot/scripts/config.env.template ~/sync/config.env

# Option 2: Create manually (copy content from repository)
```

### Configure Credentials

```bash
# Edit config
nano ~/sync/config.env

# Secure the file
chmod 600 ~/sync/config.env
```

Fill in:
- `SEEDBOX_HOST`: Your Ultra.cc server (e.g., `john.sb01.usbx.me`)
- `SEEDBOX_USER`: Your username
- `SEEDBOX_PASS`: Your password
- `SEEDBOX_PATH`: Completed downloads path (usually `/home/USERNAME/Downloads/completed`)
- `NAS_MOVIES`: Local movies folder
- `NAS_TV`: Local TV shows folder
- `BOT_API_URL`: Your bot's Koyeb URL
- `SYNC_API_KEY`: API key for notifications

### Make Script Executable

```bash
chmod +x ~/sync/sync_seedbox.sh
```

## 4. Test the Script

Run manually first:

```bash
~/sync/sync_seedbox.sh
```

Check the log:
```bash
tail -f ~/sync/logs/sync.log
```

## 5. Set Up Cron Job

```bash
# Edit crontab
crontab -e

# Add this line (runs every 30 minutes):
*/30 * * * * /home/mediabot/sync/sync_seedbox.sh >> /home/mediabot/sync/logs/cron.log 2>&1
```

## 6. Configure Bot API Key

On your Koyeb deployment, add the environment variable:

```
SYNC_API_KEY=your_secret_key_here
```

Use the same key in `config.env`.

## Folder Structure

After setup, your NAS will organize files like this:

```
/mnt/Freebox/Space/Фильмы и сериалы/
├── Кино/
│   ├── Movie.Name.2024.2160p.BluRay.mkv
│   └── Another.Movie.1080p.WEB-DL.mkv
└── Сериалы/
    ├── Series.Name.S01E01.720p.WEB.mkv
    └── Other.Show.S02E05.1080p.mkv
```

## Troubleshooting

### Script Fails with "Permission denied"
```bash
chmod +x ~/sync/sync_seedbox.sh
chmod 600 ~/sync/config.env
```

### "Host key verification failed"
First SSH manually to accept the host key:
```bash
ssh USERNAME@SERVERNAME.usbx.me
# Type 'yes' to accept
```

### Rsync Times Out
Check your seedbox connectivity:
```bash
ping SERVERNAME.usbx.me
ssh USERNAME@SERVERNAME.usbx.me "ls"
```

### Files Not Sorted Correctly
The script detects TV shows by patterns like `S01E01`. If files are mislabeled, they'll go to Movies by default.

### No Notifications
1. Check `SYNC_API_KEY` matches in both config.env and Koyeb
2. Verify bot URL is correct
3. Check sync.log for curl errors

## Maintenance

### View Logs
```bash
tail -100 ~/sync/logs/sync.log
```

### Clear Old Logs
```bash
find ~/sync/logs -name "*.log" -mtime +30 -delete
```

### Check Disk Space
```bash
df -h /mnt/Freebox/Space/
```
