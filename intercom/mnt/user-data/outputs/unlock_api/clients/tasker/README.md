# Tasker — Unlock Front Door

## Import
1. Copy `unlock_door.xml` to your phone (via TrueNAS share or direct download)
2. In Tasker → Tasks → long press → Import Task
3. Select `unlock_door.xml`

## Configure
Edit the task and update two values in the HTTP Request action:
- URL: replace `DOCKER_HOST` with your Docker VM's IP or Tailscale hostname
- Header: replace `YOUR_API_KEY` with the value of `DAHUA_API_KEY` in docker-compose.yml

## Home Screen Shortcut
1. Nova Launcher → long press home screen → Widgets → Tasker → Task Shortcut
2. Select "Unlock Front Door"
3. Choose an icon (lock/key icon works well)

## What it does
- POST /unlock to dahua-intercom API
- Shows "🔓 Door Unlocked" toast on success
- Shows "❌ Unlock Failed (HTTP xxx)" toast on failure

## NFC Trigger (optional)
Create a Tasker Profile → NFC tag → link to this task.
Stick a cheap NTAG213 sticker inside your door frame —
tap your phone to it to unlock without opening any app.
