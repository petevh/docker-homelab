# iOS Shortcuts — Unlock Front Door

## Create the Shortcut

1. Open the Shortcuts app → tap **+** (new shortcut)
2. Tap **Add Action** → search for **"Get Contents of URL"**
3. Configure:
   - URL: `http://DOCKER_HOST:8000/unlock`
     (use Tailscale hostname for remote access)
   - Method: **POST**
   - Headers:
     - Key: `X-API-Key`
     - Value: `YOUR_API_KEY`
4. Add action → **"Get Dictionary from Input"** (to parse JSON response)
5. Add action → **"If"** → Dictionary → `success` → **equals** → `true`
6. Add **"Show Notification"** → "🔓 Door Unlocked"
7. Otherwise → **"Show Notification"** → "❌ Unlock Failed"
8. Name the shortcut **"Unlock Front Door"**
9. Tap the shortcut settings (info icon) → enable **"Add to Home Screen"**

## Home Screen
- Add to home screen for one-tap unlock
- Works on iPhone and Apple Watch (Shortcuts complication)

## Siri
Say **"Hey Siri, Unlock Front Door"** — works hands-free

## NFC (iPhone XS or later)
1. Shortcuts app → Automation → **+** → **NFC**
2. Tap an NTAG213 sticker to register it
3. Select **"Unlock Front Door"** shortcut
4. Disable "Ask Before Running" for instant unlock

## Notes
- On home network: use local IP `http://192.168.x.x:8000/unlock`
- Remote access: use Tailscale IP or hostname
- Consider creating two shortcuts (home/remote) and using
  a Personal Automation to switch based on WiFi network
