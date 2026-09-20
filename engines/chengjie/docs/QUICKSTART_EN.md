# ChatX Quickstart (English) — 30 minutes from download to first AI reply

> WP-9 deliverable (2026-08-17), aligned with the WP-1 "clean VM in 30 minutes"
> path and the WP-2 first-run wizard. Audience: an overseas evaluator with a
> Windows PC and a Telegram account. No GPU, no LAN services, no Docker needed.

## What you need

- Windows 10/11 (64-bit), ~2 GB free disk, outbound internet.
- A Telegram account you control (for the QR login).
- A license key — the free trial key is issued on the download page
  (`/en/download/chatx`); paid keys come from your vendor contact.

## 1. Install (5 min)

1. Download `ChatX-Setup-<version>.exe` from the website download page.
2. Run the installer. It installs per-user (no admin prompt needed) and starts
   ChatX when done.
3. First boot seeds a **pure-cloud starter profile** (`cloud_light`): all
   LAN/GPU-dependent features stay off, AI replies run against the built-in
   cloud endpoints. Nothing to configure by hand.

If the window opens and then closes immediately, another copy of the backend
may be holding the port from an older install — reboot once, or reinstall with
the latest setup (installers since 1.0.38 reap stale backends automatically).

## 2. First-run wizard (10 min)

On a fresh install the app opens **Getting started** (`/welcome`) after login.
Five steps, each optional to skip and resumable later:

1. **License** — paste your key, click Activate. The seat/channel/quota summary
   should show up immediately.
2. **Channel** — connect Telegram by scanning the QR with the Telegram app
   (Settings → Devices → Link Desktop Device). The status flips to *online*.
3. **Persona** — pick a template (companion / support / sales) or create your
   own. This controls the AI's name, tone and boundaries.
4. **Automation tier** — choose how bold the AI should be:
   - *Review*: AI drafts, a human approves every message (recommended first);
   - *Auto*: AI replies on its own for low-risk messages.
   You can change this per conversation later.
5. **Test message** — send a hello to your own account to confirm the pipe.

The wizard remembers progress (`skip` is fine); it stops appearing once
completed and never shows for agent-role logins.

## 3. First real reply (5 min)

1. From a second Telegram account (a friend's phone works), message the
   connected account.
2. Open **Workspace → Inbox**: the conversation appears within seconds.
3. In *Review* tier you'll see an AI draft — click approve to send. In *Auto*
   tier the reply goes out on its own and is mirrored in the inbox.

## 4. Where to look every day (5 min)

- **Boss daily report** (`/boss`): replies sent today, human hours saved,
  proactive greetings, referrals — the money view, one screen.
- **Inbox** (`/workspace`): conversations, drafts pending review, manual send.
- **Personas** page: the *Stocking* tab shows per-persona readiness
  (profile / cloned voice / album / line bank) with one-click entries.

## 5. Optional next steps

- **Voice**: enroll a cloned voice in the persona's Voice tab (a 10-second
  clean sample is enough); replies can then go out as voice notes.
- **Compliance mode**: for EU AI Act Art. 50 / CA SB 243 deployments, the
  operator can turn on AI disclosure + honest identity in config
  (`compliance.*`, all off by default). See the compliance capability doc.
- **Backup / machine move**: `scripts/instance_backup.ps1` +
  `instance_restore.ps1` — full data-root backup with per-file checksums; see
  the migration SOP.

## Troubleshooting quick hits

| Symptom | Likely cause / fix |
|---|---|
| App opens, backend "connecting" forever | Old backend holding port 18799 — reboot or reinstall with ≥1.0.38 |
| QR login loops | Telegram app too old, or the account has an active password prompt — finish login on the phone first |
| Wizard doesn't appear | It only auto-opens on fresh installs with onboarding enabled; open `/welcome` manually |
| Replies stay in "pending review" | You're in *Review* tier — approve in Inbox, or raise the automation tier in the wizard/settings |
