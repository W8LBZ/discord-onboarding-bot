# SREL Onboarding Bot

Source: https://github.com/W8LBZ/discord-onboarding-bot (private).

Minimal Discord onboarding bot for the club. New members pick one button:

- **I'm a club member** → callsign + email modal → 6-digit code emailed to the address on the roster → enter code → `@member` role granted. If the roster marks the callsign as an officer, the case posts to `#verification-review` for human approval (server owner or existing officers can decide).
- **I'm just visiting** → `@guest` role granted immediately.

Officer roles and the review queue are built. Full spec lives in [REQUIREMENTS.md](REQUIREMENTS.md).

**Not built yet:**

| Gap | Spec |
|---|---|
| Versioned rules acceptance + re-prompt on bump (a plain "type I AGREE" gate is built) | §8 |
| Public verification log | §6.6 |
| `/claim transfer <old> <new>` — vanity call changes | §6.9, §12 |
| `/verify user <@user>` — manual override path | §6.9 |
| `/rules bump <version>` | §6.9, §8 |
| Audit log for privileged commands | §6.9, §13 |
| Funnel reporting | §13.1 |

Built officer commands: `/post-welcome`, `/roster-sync`, `/roster-status`, `/claim-release`, `/claim-who`, `/rename-all`.

**Decisions still open before launch** (§16): who owns the roster, newsletter bounce rate, which channels sit above `@guest`, audit-log retention.

---

## Architecture at a glance

```
        +--------------------+          +----------------+
Discord | persistent buttons |          | Google Sheets  |
------> |  in #welcome       |          |  (roster)      |
        +---------+----------+          +--------+-------+
                  |                              |
                  v                              | hourly + on demand
        +--------------------+                   v
        |     bot.py         |    +--------------+---------+
        |  discord.py client |<---+ SQLite  (bot.db)       |
        +---------+----------+    |  roster, claimed, codes|
                  |               +------------------------+
                  | SMTPS
                  v
        +--------------------+
        | Gmail (or any      |
        |  SMTP host)        |
        +--------------------+
```

All persistent state lives in one SQLite file (`bot.db`). Backup = copy the file. No external database, no message queue, no vendor lock-in.

---

## Prerequisites (create these once, before deploying)

### 1. Discord application

1. https://discord.com/developers/applications → **New Application** → name it.
2. **Bot** tab → **Reset Token** → copy the token (`DISCORD_TOKEN`).
3. **Bot** tab → enable **Server Members Intent** (required to detect joins).
4. **OAuth2 → URL Generator**:
   - Scopes: `bot`, `applications.commands`
   - Bot permissions: `Manage Roles`, `Manage Nicknames`, `Send Messages`, `Read Messages/View Channels`, `Use Slash Commands`
   - Open the generated URL, invite the bot to your server.
5. In your server, create these roles (any names; you only need the IDs):
   - `@unverified` — assigned on join
   - `@guest` — for visitors
   - `@member` — for verified club members
   - `@officer` — for verified officers
6. Create channels:
   - `#welcome` — public, holds the verification buttons.
   - `#verification-review` — **private**, visible only to `@officer` and the server owner. Officer-grant cases post here for approval.
7. Enable **User Settings → Advanced → Developer Mode**. Right-click roles / channels / server to copy their IDs.
8. **Role hierarchy**: drag the bot's own role **above** `@unverified`, `@guest`, `@member`, and `@officer`. It cannot manage roles positioned above its own, and it can never rename the server owner.

### 2. Google Sheet (roster)

1. Create a sheet with a header row containing at minimum: `callsign`, `full_name`, `email`, `is_officer`. Extra columns are ignored.
   - `is_officer` accepts `TRUE`/`FALSE`, `yes`/`no`, `1`/`0`, or a native Google Sheets checkbox (Insert → Checkbox). Blank = not an officer.
2. https://console.cloud.google.com → new project → **APIs & Services → Enable APIs** → enable **Google Sheets API**.
3. **IAM & Admin → Service Accounts** → create service account → **Keys → Add key → JSON**. Download the JSON file. Guard it like a password.
4. Copy the service account's email address (looks like `srel-bot@project.iam.gserviceaccount.com`), open your sheet, **Share** with that address (Viewer is enough).
5. Copy the sheet ID from its URL: `https://docs.google.com/spreadsheets/d/`**`<this-part>`**`/edit`.

### 3. Gmail (email sender)

The bot sends from a dedicated club Gmail account (see the shared password vault for the address and credentials).

1. On that account, turn on **2-Step Verification** — https://myaccount.google.com/security. App passwords do not exist without it.
2. https://myaccount.google.com/apppasswords → create one named `srel-bot`. Save the 16-character password; it is shown once.
3. Use the account address as `SMTP_USER` and `FROM_EMAIL`, and the app password as `SMTP_PASSWORD`.

Gmail caps outbound mail at ~500 messages/day, well above what onboarding needs.

Any other SMTP provider works too — just point `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` at it (SMTPS on port 465 assumed).

---

## Deployment on a Proxmox LXC

### Create the container

On the Proxmox host:

1. Download a Debian 12 or Ubuntu 24.04 template if you don't already have one:
   `pveam update && pveam available | grep -E 'debian-12|ubuntu-24'` then `pveam download <storage> <template>`.
2. **Create CT** in the web UI:
   - **Unprivileged container**: yes (default). Keep it that way.
   - Resources: 1 CPU, 512 MB RAM, 2 GB disk — comfortably enough.
   - Network: DHCP or a static IP. Only outbound access is required (Discord gateway, Google APIs, SMTP). No inbound ports.
   - **Features**: leave `keyctl` and `nesting` **off**. Not needed.
   - Set a strong root password.
3. Start the container, open its console.

### Inside the container

```bash
# 1. Base packages
apt update && apt install -y python3 python3-venv python3-pip git ca-certificates

# 2. Dedicated system user (no login shell, no home)
adduser --system --group --home /opt/srel-bot --shell /usr/sbin/nologin srelbot
mkdir -p /opt/srel-bot
chown srelbot:srelbot /opt/srel-bot
chmod 750 /opt/srel-bot

# 3. Clone the repo into /opt/srel-bot
#    The repo is private: generate a read-only deploy key on the container
#    (ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N ""), add the public key
#    under Settings -> Deploy keys on GitHub, then:
git clone git@github.com:W8LBZ/discord-onboarding-bot.git /tmp/srel-bot
mv /tmp/srel-bot/* /tmp/srel-bot/.env.example /opt/srel-bot/
cd /opt/srel-bot
chown -R srelbot:srelbot .

# 4. Virtualenv + deps (as the service user)
sudo -u srelbot python3 -m venv venv
sudo -u srelbot ./venv/bin/pip install --upgrade pip
sudo -u srelbot ./venv/bin/pip install -r requirements.txt

# 5. Configuration
cp .env.example .env
# Edit .env: paste tokens, IDs, SMTP password. Then lock it down:
chown srelbot:srelbot .env
chmod 600 .env

# 6. Google service account key
#    Copy service-account.json into /opt/srel-bot, then:
chown srelbot:srelbot service-account.json
chmod 600 service-account.json

# 7. systemd unit
cp srel-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now srel-bot
systemctl status srel-bot

# 8. Unattended OS security updates
apt install -y unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades   # answer "Yes"
```

Step 8 applies Debian/Ubuntu security patches automatically. It does **not**
update the bot's own code — see [Updating the bot](#updating-the-bot). If a
patched package needs a reboot, `/var/run/reboot-required` appears; reboot at
your convenience, the service comes back on its own (`enable --now` above).

Follow logs:

```bash
journalctl -u srel-bot -f
```

You should see `logged in as <bot name>` and, within a minute, `roster synced: N rows`.

### First-time bootstrap in Discord

Run each slash command in your server (they only appear to administrators):

1. `/roster-sync` — confirm the sheet loads.
2. `/post-welcome` — posts the persistent buttons into `#welcome`.

Test the flow with a second Discord account. That's it.

---

## Service control

```bash
systemctl restart srel-bot      # restart (after a code or .env change)
systemctl stop srel-bot
systemctl start srel-bot
systemctl status srel-bot       # is it up, and why did it die
journalctl -u srel-bot -f       # follow logs
journalctl -u srel-bot -n 200   # last 200 lines
reboot                          # whole container; the bot auto-starts
```

The unit is `Restart=on-failure`, so a crash restarts it after 10s. A clean
exit (bad config, `SystemExit` on missing env vars) does **not** restart —
`status` will show the reason.

## Updating the bot

Deliberately manual: pulling and restarting unreviewed code unattended is how
a bad commit takes the server down at 2am with nobody watching.

```bash
cd /opt/srel-bot
git pull                                          # as root: the deploy key is /root/.ssh
chown -R srelbot:srelbot /opt/srel-bot
sudo -u srelbot ./venv/bin/pip install -r requirements.txt   # only if it changed
diff <(grep -o '^[A-Z_]*=' .env.example | sort) <(grep -o '^[A-Z_]*=' .env | sort)
systemctl restart srel-bot
journalctl -u srel-bot -n 50
```

The `diff` line catches the common upgrade break: a release adds a variable to
`.env.example` and the bot exits on startup because your `.env` lacks it. Add
any missing keys before restarting.

---

## Configuration reference

All settings are environment variables (see `.env.example`). Required:

| Variable | Meaning |
|---|---|
| `DISCORD_TOKEN` | Bot token from the Discord developer portal. |
| `GUILD_ID` | Discord server ID. |
| `WELCOME_CHANNEL_ID` | Channel where `/post-welcome` publishes the buttons. |
| `REVIEW_CHANNEL_ID` | Private channel where officer-grant cases post for approval. |
| `UNVERIFIED_ROLE_ID` / `MEMBER_ROLE_ID` / `GUEST_ROLE_ID` / `OFFICER_ROLE_ID` | The four role IDs. |
| `SMTP_USER` / `SMTP_PASSWORD` / `FROM_EMAIL` | Gmail (or other SMTP) credentials. |
| `GOOGLE_SHEET_ID` | Roster spreadsheet ID. |

Optional:

| Variable | Default | Meaning |
|---|---|---|
| `ANNOUNCE_CHANNEL_ID` | *(unset)* | Members channel for the new-member welcome post. Unset or `0` = no announcement. |
| `RULES_CHANNEL_ID` | *(unset)* | Rules channel linked in the welcome post and agreement prompts. Unset or `0` = generic wording. |
| `SMTP_HOST` | `smtp.gmail.com` | SMTPS host. |
| `SMTP_PORT` | `465` | SMTPS port (implicit TLS). |
| `FROM_NAME` | `SREL Bot` | Display name on outbound email. |
| `GOOGLE_SA_JSON` | `./service-account.json` | Path to the service account key. |
| `ROSTER_SYNC_INTERVAL_MIN` | `60` | Minutes between automatic roster resyncs. |
| `DB_PATH` | `./bot.db` | SQLite file. |
| `CODE_TTL_MIN` | `15` | Verification code lifetime. |
| `CODE_RATE_LIMIT_PER_HOUR` | `3` | Max codes any one user can request per hour. |

---

## Slash commands

All are administrator-only.

| Command | Purpose |
|---|---|
| `/post-welcome` | Post (or repost) the persistent verification buttons in `#welcome`. |
| `/roster-sync` | Force an immediate roster resync. |
| `/roster-status` | Show roster row count, officer count, claim count, open review cases, last sync time. |
| `/claim-release <callsign>` | Release a claimed callsign so it can be re-verified. |
| `/claim-who <callsign>` | Show which Discord user holds a callsign. |
| `/rename-all [apply]` | Backfill nicknames for already-verified members. Dry run by default; add `apply:True` to rename. |

## Review queue

When a callsign flagged `is_officer=TRUE` on the roster reaches the email-code step:

1. Email code is verified as usual → identity proven.
2. **No roles are granted yet.** Case is written to the `review_queue` table.
3. An embed posts to `#verification-review` with: the user mention, submitted callsign, roster row (name + officer flag), Discord account creation date, server join date, and a review ID.
4. Two buttons on the embed: **Approve** / **Deny**.
5. Anyone with the `@officer` role, an Administrator permission, or the server owner can click. First click wins; the buttons are removed and the embed color/title update to reflect the decision.
6. **Approve** → both `@member` and `@officer` roles granted; the user is DM'd; audit line logged to journalctl.
7. **Deny** → **no roles granted**, not even `@member`. Denial means the officer doesn't believe the verification, and the email code alone proved only inbox control. The user is DM'd. Log line written.

The reviewer's user ID is written to `review_queue.resolved_by`. All decisions are permanent and race-safe: the DB update is guarded by `resolved_at IS NULL`, so a second officer clicking the same case gets an "already resolved" message.

---

## Security notes

The bot handles a bot token, an SMTP password, and a Google service-account key. Treat all three as production secrets.

- **File permissions.** `.env` and `service-account.json` must be `chmod 600` and owned by `srelbot`.
- **Gmail app password.** Scoped to SMTP only, revocable individually at https://myaccount.google.com/apppasswords without touching the account password. Revoke on any compromise.
- **Google service account.** Read-only scope (`spreadsheets.readonly`), shared with only the roster sheet. Rotate the key by generating a new one and deleting the old in the GCP console.
- **Discord token.** If leaked, reset it in the developer portal. The bot restarts and picks up the new token.
- **Unprivileged LXC.** UID mapping already isolates the container from the host. Keep `keyctl` and `nesting` disabled.
- **systemd sandboxing.** The unit file locks the service down (`ProtectSystem=strict`, `ReadWritePaths=/opt/srel-bot`, no capabilities, seccomp filter). The bot cannot write outside `/opt/srel-bot`.
- **Outbound only.** No inbound ports needed. Firewall accordingly.
- **Race safety.** The dedup check is repeated inside `handle_code_submit`, so a race between two people requesting the same callsign is resolved when the code is entered, not when it's requested.
- **What the emails contain.** The 6-digit code, the callsign it's for, and the TTL. No personal data.
- **What the database contains.** Roster snapshot (callsign, full name, email), claim ledger (callsign → Discord user ID + email), and verification codes. No FCC lookup data (v1 does no live callsign lookup).
- **Log hygiene.** Codes are never logged. SMTP failures are logged with the Discord user ID but not the address.

---

## Backups

The database is one file. Back it up nightly with a cron job on the container:

```bash
cat >/etc/cron.d/srel-bot-backup <<'EOF'
30 3 * * * srelbot sqlite3 /opt/srel-bot/bot.db ".backup '/opt/srel-bot/backup/bot-$(date +\%F).db'"
0  4 * * * root    find /opt/srel-bot/backup -name 'bot-*.db' -mtime +30 -delete
EOF
mkdir -p /opt/srel-bot/backup
chown srelbot:srelbot /opt/srel-bot/backup
```

Then rsync or scp `/opt/srel-bot/backup/` to somewhere off the container. Proxmox itself can also snapshot or back up the whole LXC — pick one, don't skip both.

**Verify restore once, before you need it:** stop the service, copy a backup over `bot.db`, start the service, confirm `/roster-sync` and a re-verification still work.

---

## Roster sheet format

Header row (row 1), any column order, any extra columns:

| callsign | full_name | email | is_officer |
|---|---|---|---|
| KD8AAA | Jim Flattery | jim@example.com | TRUE |
| KD8BBB | Jane Doe | jane@example.fr | FALSE |

Rules the sync enforces:

- Rows missing `callsign` or `email` are skipped.
- Duplicate callsigns keep the first row.
- Callsigns are uppercased; emails lowercased.
- If a resync would drop more than 25% of the rows, it is refused and the previous snapshot is retained. (Guards against "someone opened the sheet and hit undo.")

---

## Troubleshooting

**`missing required env vars: …` on startup.** Fill in `.env`. If the file is present but the service can't read it, check `chmod 600` and that it's owned by `srelbot`.

**`403 Missing Permissions` / role assignment fails.** The bot's own role in Discord is below the role it's trying to grant. Drag the bot's role above `@unverified`, `@guest`, and `@member`.

**Slash commands don't appear.** Kick and re-invite the bot with the OAuth2 URL from the prerequisites (make sure `applications.commands` scope was included). Commands are guild-scoped and appear within seconds of first `on_ready`.

**Verification email doesn't arrive.** Check `journalctl -u srel-bot`. Common causes: Gmail app password typo (paste all 16 chars), 2-Step Verification turned off on the sending account, wrong `FROM_EMAIL` (Gmail rewrites it to `SMTP_USER` unless it is a verified alias), or the recipient's provider silently rejecting mail from a new sender. `535 Username and Password not accepted` means the app password is wrong or was revoked.

**Google Sheets sync fails.** The sheet must be shared with the service account's email address. Check `/roster-sync` output for the underlying error.

**"That callsign is already registered."** Use `/claim-who <callsign>` to see who has it and `/claim-release <callsign>` to free it up.

---

## What's deliberately missing (and how to add it later)

This MVP intentionally skips several requirements from `callsign-onboarding-bot-requirements-v2.md`. Each is straightforward to bolt on:

- **Rules acceptance** — add a second view between callsign entry and email send.
- **Public verification log** — the welcome announcement covers the social half; a machine-readable audit log is still unbuilt.
- **Nightly CSV export** — `sqlite3 bot.db ".mode csv" ".output roster.csv" "SELECT * FROM roster;"` in the existing cron.
- **Review-queue staleness alerts** — background task that re-pings `#verification-review` when a case has been open >48h.
- **Roster-miss handling** — currently rejected at submit. Doc §9 suggests queuing these too (a visiting ham from another club).
