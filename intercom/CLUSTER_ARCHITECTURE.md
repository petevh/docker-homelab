# Cluster Unlock — Future Architecture (design notes, NOT yet built)

Forward design thinking for scaling the Dahua intercom unlock beyond a single
apartment to the wider **9-building apartment cluster**. Nothing here is built —
these are the agreed principles to follow if/when it is. The working single-user
system is documented in `DEVNOTES.md` / `README.md`.

## Context
- The cluster is 9 buildings, each with its own VTO. (The author sits on the
  Owner's Committee, so has influence over the official solution.)
- Background: the door locks have been broken for years; residents force doors or
  wedge them open. Any software here is a **convenience/bridge** layer, **parallel
  to and on top of** the legacy card system — its failure must NOT stop the
  existing system from working.
- **Real endgame (official, preferred):** the Committee replaces the old card
  readers with ones that read the newer NFC cards already issued (which currently
  open the entrance boom). That is the proper centrally-managed fix — no personal
  liability, no Dahua-cloud dependency. The software is a bridge that demonstrates
  value and buys time until that hardware refresh. (Current official stopgap =
  residents pay for replacement cards.)

## Hard constraints (non-negotiable design rules)
1. **Store NO ONE's credentials anywhere central** — unnecessary liability. Not
   even encrypted / in a secrets manager.
2. **Nothing running at the user's premises** — no Pi, no agent, no per-resident
   container.
3. **No dependency on the author's homelab or VTH** for other people. (A given VTH
   only reaches its own building's VTO; an isolated VPS has no LAN path to any VTH.)
4. **No Tailscale dependency** for residents.
5. Self-sufficient per user: an **iOS Shortcut**, auth happens **on their phone**,
   done.
6. **Watch expectation-shift:** a too-convenient NFC tag makes people abandon their
   official card → the system silently becomes load-bearing. Frame explicitly as
   "convenience layer, keep your card."

## Key technical fact
**Unlock is PURE CLOUD — VPS-ready today, no VTH/LAN needed.** `unlock_door()` is
just an HTTPS POST to `dmss-di.dolynkcloud.com/pcs/v1/...OpenVthDoor`. Zero
socket / LAN / VTH. Only doorbell `/events` (DHIP TCP/5000) and local video need
the LAN — those stay per-resident/local, NOT on a VPS. So a federated
unlock-only service needs nothing but the cloud + per-user auth.

## The crux / unresolved tension
"Central management" vs "zero credential exposure" are in tension:
- **(i) Phone → Dahua cloud directly** (Shortcut holds the user's own token, calls
  the cloud itself): truly zero central custody/transit — but then the VPS is not
  in the path at all, so there's no central control/audit. Essentially "everyone
  uses their own DMSS account" (the lowest-liability first step).
- **(ii) Phone → VPS gateway → Dahua cloud:** gives a central control point
  (allow/deny, audit, stable URL, rate-limit, Authentik identity) — but the user's
  token *transits* the VPS, so "no creds anywhere" becomes "none *stored*, but they
  *transit*." Still far better than storing.
- **Sweet spot:** (ii) as a **dumb pass-through gateway** with **short-lived tokens
  minted on-device**, so what transits is low-value + expiring. The VPS holds
  identity (who / which building) but never persists a Dahua credential.

## Recommended posture
- **Default for most residents:** their own DMSS account + Shortcut/NFC tag. Zero
  custody, scales on an instruction sheet.
- **A central VPS service's job = identity + convenience, NOT credential custody.**
  Authentik for federation (Apple/Google → building group → permission), a clean
  UI, audit. Deploy on a **standalone VPS** (not the homelab) so others' access
  doesn't share fate with the author's home network / power / ISP.
- **API keys** (the intercom container's named-key scheme) stay for *machine*
  callers; **Authentik** is the *human/browser* layer — they complement, not
  replace, each other.
- **Avoid:** a VPS storing everyone's Dahua passwords (worst blast radius: their
  whole Dahua account, not just a door), and "one superuser account unlocks a whole
  building for everyone."
- A Dahua scoped/delegated token (door-only, expiring) would improve (ii) a lot —
  but reverse-engineering shows the Bearer is **account-wide** (minted from account
  login); no evidence of a door-only scope. Would need research; don't bank on it.

## Phone-as-key & the official reader-replacement decision
Relevant to the Committee's reader-replacement choice — and potentially makes the
software bridge unnecessary by doing it officially:
- **Generic NFC phone-tap:** possible only if the cards are **13.56 MHz** (MIFARE
  etc.) — Android can emulate via HCE; **iPhone is locked down** (only Apple Wallet
  badges / sanctioned NFC entitlements). If the existing boom cards are old
  **125 kHz** prox, phones CANNOT emulate them at all. → First unknown to pin down:
  what frequency/standard are the issued NFC cards.
- **Dahua's integrated phone-as-key is BLUETOOTH, not NFC tap** — which is exactly
  why it works on iPhone (sidesteps Apple's NFC lockdown). Product: **ASR2100Z-B
  Bluetooth reader** (does IC cards + smartphone), set up via **DMSS** (creates a
  "digital ID" on the phone). Needs the **DHI-ASC3202B** web access controller
  (RS-485, sold separately) — a reader + controller system.
- **Strategic pitch for the Committee:** instead of just reprinting cards, spec the
  reader replacement as a **Dahua Bluetooth access controller (ASR2100Z-B +
  ASC3202B)** → phones become keys *officially*, integrated with the same DMSS app
  residents already use, vendor-supported, **zero credentials held personally, no
  VPS gateway needed**. This is the cleanest endgame — it obsoletes the personal
  software bridge.
- **Caveat:** model numbers / RS-485 controller requirement / current iPhone
  credential support are from 2024 vendor + news sources and vary by region/SKU —
  **confirm current models + iPhone support with a regional Dahua distributor before
  any formal Committee proposal.** Directionally correct; verify specifics.

## Liability framing
Operating access control for buildings one doesn't own is a real responsibility
even if informal. Keep the credential blast radius minimal, keep it explicitly
parallel to the legacy system, and push the official reader replacement as the
true fix. The Committee seat is the lever to make it official rather than a
personal hack.
