# Seedbox Setup Guide

This guide explains how to set up a seedbox for use with the Media Concierge Bot.

## Overview

A seedbox allows you to:
- Download torrents on a remote high-speed server
- Stream or sync files to your local NAS
- Keep your home IP separate from torrent activity

The bot supports **Deluge** torrent client (used by Ultra.cc and similar providers).

## 1. Register on Ultra.cc

1. Go to https://ultra.cc/
2. Choose a plan:
   - **S-Wind** ($4.48/mo) - Good for personal use
   - **M-Wind** ($8.95/mo) - More storage and bandwidth
3. Select **Netherlands** datacenter (or closest to you)
4. Complete registration and payment

## 2. Install Deluge

1. Login to UCP (Ultra Control Panel) at https://cp.ultra.cc/
2. Go to **Applications** > **Install**
3. Find and install **Deluge**
4. Wait for installation to complete

## 3. Configure Deluge

1. In UCP, click on **Deluge** to open settings
2. Set a **Web UI password** (you'll need this for the bot)
3. Note your Deluge URL format:
   ```
   https://USERNAME.SERVERNAME.usbx.me/deluge
   ```
   Replace USERNAME and SERVERNAME with your actual values

## 4. Configure the Bot

In Telegram, send the `/seedbox` command to the bot.

Enter your credentials when prompted:
1. **Host URL**: Your Deluge Web UI URL (e.g., `https://john.sb01.usbx.me/deluge`)
2. **Username**: Your Ultra.cc username
3. **Password**: Your Deluge Web UI password

The bot will test the connection before saving.

## 5. Test the Setup

1. Search for any movie in the bot
2. Click the **Seedbox** button on a search result
3. Check your Deluge web UI to confirm the torrent was added

## Troubleshooting

### "Connection failed"
- Verify your Deluge URL is correct
- Make sure Deluge is running (check UCP)
- Try accessing the URL in a browser

### "Authentication failed"
- Double-check your password
- Reset the Deluge password in UCP if needed

### "Torrent not added"
- Check if Deluge has available disk space
- Verify the magnet link is valid

## Multi-User Support

Each user can configure their own seedbox:
- Your credentials are stored encrypted
- Only you can access your seedbox
- Friends can set up their own seedbox accounts

## Optional: Sync to NAS

See [FREEBOX_VM_SETUP.md](FREEBOX_VM_SETUP.md) for automatic sync to your local NAS.
