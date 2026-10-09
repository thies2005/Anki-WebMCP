# Anki-WebMCP

> A self-hosted, headless **multi-user** Anki MCP service in Docker: one Python process serving per-user collections through the official `anki` library, a web portal for tokens and AnkiWeb sync, an admin UI, and an authenticated Streamable HTTP Model Context Protocol (MCP) endpoint for AI clients.

---

## Table of Contents

1. [Overview & Philosophy](#overview--philosophy)
2. [Prerequisites](#prerequisites)
3. [Architecture](#architecture)
4. [Write Safety & Synchronization Model](#write-safety--synchronization-model)
5. [Quick Start](#quick-start)
6. [First-Run Setup: Portal, Users & AnkiWeb](#first-run-setup-portal-users--ankiweb)
7. [Exposure Modes](#exposure-modes)
   - [Mode 1: Cloudflare Tunnel](#mode-1-cloudflare-tunnel)
   - [Mode 2: Coolify HTTPS Reverse Proxy](#mode-2-coolify-https-reverse-proxy)
8. [MCP Client Integration](#mcp-client-integration)
9. [Available MCP Tools](#available-mcp-tools)
10. [Security Architecture](#security-architecture)
11. [Troubleshooting](#troubleshooting)
12. [Operations](#operations)
13. [Testing & Verification](#testing--verification)
14. [History](#history)

---

## Overview & Philosophy

### What this is
- **One headless service** (no GUI, no browser desktop) that owns Anki collections **in-process** via the official `anki` Python library (pinned `anki==26.9.3`).
- **Multi-user by design**: every account gets its own collection, its own MCP token, and its own AnkiWeb credentials. Built for a household or small group (comfortable at 2–5 users; no hard cap).
- **Web portal and admin UI** on the same port: users log in to view/copy their MCP token, enter AnkiWeb credentials, trigger a sync, and see deck stats; admins manage accounts at `/admin`.
- **Official AnkiWeb Cloud Sync**, explicit and per-user.
- **Remote Streamable HTTP MCP server** at `/mcp`, authenticated with per-user bearer tokens, for AI assistants (Claude, Cursor, custom agents).

### What this is NOT
- There is **no Anki Desktop in the browser anymore** — the streamed GUI (KasmVNC) and AnkiConnect were removed in the headless redesign. **Reviewing happens on your other Anki clients** (desktop app, AnkiDroid, AnkiMobile) after an explicit sync pushes/pulls changes through AnkiWeb.
- This is **not** a replacement Anki backend, custom sync server, or web scraper.
- Mutations made through MCP are **local to the service** until an explicit sync runs — see [Write Safety & Synchronization Model](#write-safety--synchronization-model).

---

## Prerequisites

- **Docker Engine 24+ and Docker Compose v2.24+** (the exposure-mode override files use the `!override` compose tag, which requires 2.24+).
- **amd64 or arm64 host** — both are supported natively: the pinned `anki` wheel ships `manylinux_2_35` builds for x86_64 and aarch64 on the multi-arch `python:3.12-slim` base.
- ~450 MB image and roughly **150–350 MB RAM per open collection** (the pool keeps at most `MAX_OPEN_COLLECTIONS` collections open, LRU-bounded).
- A free host port `8000` (configurable mapping).
- A Cloudflare account with a Zero Trust tunnel (only for Exposure Mode 1).
- `openssl` to generate the session secret (and a strong password for the bootstrap admin).

---

## Architecture

One containerized service and an optional exposure gateway:

```
┌──────────────────────────────────────────────────────────────────┐
│                         EXPOSURE LAYER                           │
│                                                                  │
│   Option A: Cloudflare Tunnel (cloudflared)                      │
│   Option B: Coolify Reverse Proxy (Traefik with Let's Encrypt)   │
└──────────────────────────────┬───────────────────────────────────┘
                               │ HTTPS
                               ▼
              ┌─────────────────────────────────┐
              │            anki-mcp             │
              │      (single service, :8000)    │
              │                                 │
              │  • Streamable HTTP (/mcp)       │
              │  • Token portal (/)             │
              │  • Admin UI (/admin)            │
              │  • Per-user auth middleware     │
              │  • CollectionRegistry + pool    │
              │  • Idempotency ledgers          │
              │  • Explicit AnkiWeb sync        │
              │  • Volume: mcp_data (/data)     │
              └─────────────────────────────────┘
```

### Component Details
1. **`anki-mcp`** (the only service):
   - Python 3.12 on `python:3.12-slim`, running the official MCP SDK (`mcp`) and the official **`anki` wheel 26.9.3** — collections are real Anki collections opened in-process, no separate app, no X11, no add-ons.
   - Serves **three surfaces on port 8000**: the Streamable HTTP MCP endpoint at `/mcp`, the user **token portal** at `/`, and the **admin UI** at `/admin`.
   - Data layout on the `/data` volume:
     ```
     /data/users.json                     # account store (0600)
     /data/users/<username>/collection.anki2
     /data/users/<username>/collection.media/
     /data/users/<username>/backups/      # pre-sync snapshots (BACKUP_RETENTION)
     /data/ledgers/<username>/ledger.json # per-user idempotency ledger
     /data/deleted/<username>-<timestamp> # archived on account deletion
     ```
   - Non-root runtime (dedicated `anki` user, uid 1000), `/data` created `0700` in the image.
   - Sanitizes and redacts secrets from logs; `LOG_LEVEL` controls verbosity.
2. **Exposure gateway** (optional): cloudflared or Coolify's Traefik terminates TLS in front of port 8000. Direct plain-HTTP exposure beyond localhost is not recommended — see [Security Architecture](#security-architecture).

---

## Write Safety & Synchronization Model

The core change from v1: **mutations never sync automatically.** Every write is applied locally to the acting user's collection and reported immediately; changes reach other Anki clients only when an **explicit sync** runs (the `sync_collection` MCP tool or the portal's Sync button). Debounced background auto-sync is a documented backlog idea, **not implemented**.

```mermaid
sequenceDiagram
    autonumber
    actor Client as AI Client (MCP)
    participant S as anki-mcp service
    participant L as Per-user Ledger
    participant C as Per-user Collection (/data)
    participant Cloud as Official AnkiWeb

    Note over Client,Cloud: Mutations are LOCAL ONLY — sync_status is reported as "skipped"
    Client->>S: add_notes / update_note_fields / add_tags (+ idempotency_key?)
    activate S
    Note over S: 1. Maintenance-mode gate
    S->>L: 2. Check idempotency key
    alt Key already committed
        L-->>S: Cached result
        S-->>Client: Replay cached committed result
    end
    Note over S: 3. Acquire that user's collection lock
    S->>C: 4. Validate & apply mutation
    C-->>S: Outcome (note IDs / updated fields)
    S->>L: 5. Commit record (atomic fs write)
    S-->>Client: 6. local_status + sync_status ("skipped") reported separately
    deactivate S

    Note over Client,Cloud: Sync is EXPLICIT — sync_collection tool or the portal Sync button
    Client->>S: sync_collection
    S->>C: 7. Snapshot collection into the user's backups/
    S->>Cloud: 8. Full AnkiWeb sync (user's own credentials, bounded by MAX_CONCURRENT_SYNCS)
    Cloud-->>S: Sync result
    S-->>Client: success, or outcome-UNKNOWN warning
```

### Safety Guarantees
- **No blind retries**: mutations are never automatically retried; an optional client-supplied `idempotency_key` gets exactly-once semantics via the per-user ledger (replays return the cached committed result or cached error).
- **One writer per collection**: all of a user's reads and writes serialize through the registry's per-user lock. Never point another Anki instance (desktop, second replica) at the same `/data` files.
- **Duplicate Prevention**: `add_notes` pre-checks each candidate against the target deck using the model's first field (exact-match search) before creating anything.
- **Crash-Window Semantics**: ledger records are flushed atomically (`tempfile` + `fsync` + `os.replace`). If a power loss lands between the collection write and the ledger commit, the first-field duplicate check prevents accidental duplicates on retry (when `allow_duplicates` is false).
- **Ambiguous-outcome semantics**: if a sync drops mid-flight with an unknown outcome, the response carries an outcome-UNKNOWN warning and the idempotency key is deliberately **not** recorded — verify collection state before retrying. Deterministic failures (e.g. duplicate rejection) do persist a `failed` record.
- **Bounded state**: the ledger keeps at most 1000 recent keys; at most `BACKUP_RETENTION` pre-sync backups per user; at most `MAX_OPEN_COLLECTIONS` collections open at once (idle ones close after `IDLE_TIMEOUT_MINUTES`).

---

## Quick Start

### 1. Clone & Configure
```bash
git clone <your-repository-url>
cd Anki-WebMCP

# Copy environment template
cp .env.example .env
```

### 2. Generate Secrets
Edit `.env` and set at least:
```bash
openssl rand -hex 24     # Use for SESSION_SECRET
openssl rand -base64 16  # Use for ADMIN_PASSWORD (or pick a strong passphrase)
```

Example `.env`:
```env
SESSION_SECRET=<output of: openssl rand -hex 24>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<output of: openssl rand -base64 16>
TZ=UTC
```

`ADMIN_USERNAME` / `ADMIN_PASSWORD` are **first-boot bootstrap only**: they create the initial admin when the account store is empty, and do nothing afterwards.

### 3. Start the Service
```bash
docker compose up -d --build
```

Verify it is healthy:
```bash
docker compose ps
curl http://localhost:8000/health
```

Expected health output:
```json
{"status":"healthy","service":"anki-mcp","anki_connected":true,"maintenance_mode":false,"auth_enabled":true}
```

`/health` returns **200** when healthy and **503** with `"status":"degraded"` when the users store is unavailable or the data directory is not writable — there is no slow GUI startup anymore (the Docker healthcheck grace period is 10 s).

---

## First-Run Setup: Portal, Users & AnkiWeb

1. Open **http://localhost:8000** and log in with `ADMIN_USERNAME` / `ADMIN_PASSWORD` from `.env`.
2. Go to **/admin** and create an account for each user (username, initial password, role). Each account immediately gets its own MCP token and empty collection.
3. As each user: log in at the portal, **copy your MCP token** (shown on the portal page; use it in your MCP client — see [MCP Client Integration](#mcp-client-integration)).
4. To connect AnkiWeb, enter your **AnkiWeb email/password** under portal → Credentials, then press **Sync**. The first sync pulls your existing collection into `/data/users/<you>/`; from then on, explicit syncs move changes between this service and AnkiWeb.
5. Reviewing happens on your normal Anki clients (desktop/AnkiDroid/AnkiMobile): sync them against the same AnkiWeb account. There is no browser-based reviewer here.

---

## Exposure Modes

Keep the core application identical while choosing your exposure strategy:

### Mode 1: Cloudflare Tunnel

Recommended for self-hosters without a public IP or open router ports.

1. Create a Tunnel in your [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/) (`Networks` -> `Tunnels`).
2. Configure the Public Hostname in the Cloudflare Dashboard:
   - `anki-mcp.yourdomain.com` -> `HTTP` -> `anki-mcp:8000`
   (This single ingress rule covers `/mcp`, the portal, and `/admin`.)
3. Add your Tunnel token to `.env`:
   ```env
   CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoi...
   ```
4. Start with the Cloudflare override:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.cloudflare.yml up -d
   ```
5. Consider **Cloudflare Access** (Zero Trust SSO / Email OTP) in front of the hostname for an extra factor on the portal and `/admin` (MCP clients would then need to satisfy Access too).

### Mode 2: Coolify HTTPS Reverse Proxy

Recommended for self-hosters running Coolify.

1. Merge the base and override files into a single stack file Coolify can consume:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.coolify.yml config > coolify-stack.yml
   ```
2. In Coolify, create a new project, add a resource of type **Docker Compose**, and paste the contents of `coolify-stack.yml`.
3. Set the environment variables in Coolify's Environment settings:
   ```env
   ANKI_MCP_DOMAIN=anki-mcp.yourdomain.com
   SESSION_SECRET=<output of: openssl rand -hex 24>
   ADMIN_USERNAME=admin
   ADMIN_PASSWORD=<output of: openssl rand -base64 16>
   ```
4. Coolify's Traefik reverse proxy will automatically issue Let's Encrypt certificates and route:
   - `https://anki-mcp.yourdomain.com` -> `anki-mcp:8000` (portal, `/admin`, and Streamable HTTP MCP)

> **Note:** Coolify must be able to route to the container via its Traefik proxy network. If the domain does not resolve, attach the `anki-mcp` service to Coolify's proxy network in the Coolify UI.

---

## MCP Client Integration

AI clients connect over the **Remote Streamable HTTP transport** at `/mcp`. **Every user uses their own token** from the portal — the token identifies which collection the tools operate on. Both `Authorization: Bearer <token>` and `X-API-Key: <token>` headers are accepted.

### Claude.ai (custom connector)
Settings -> Connectors -> Add custom connector:
- **URL**: `https://anki-mcp.yourdomain.com/mcp`
- **Authentication**: add the header `Authorization: Bearer <user-token-from-portal>`

### Claude Desktop

Claude Desktop has no native remote-HTTP MCP transport, so bridge it with [`mcp-remote`](https://www.npmjs.com/package/mcp-remote). Add the following to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "anki": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "https://anki-mcp.yourdomain.com/mcp",
        "--header",
        "Authorization: Bearer <user-token-from-portal>"
      ]
    }
  }
}
```

### Claude Code
```bash
claude mcp add --transport http anki https://anki-mcp.yourdomain.com/mcp \
  --header "Authorization: Bearer <user-token-from-portal>"
```

### Generic Streamable HTTP clients
- **URL**: `https://anki-mcp.yourdomain.com/mcp`
- **Headers**:
  ```http
  Authorization: Bearer <user-token-from-portal>
  Accept: application/json, text/event-stream
  ```

---

## Available MCP Tools

All tools operate on the **authenticated user's** collection — the token decides whose data is touched, and users cannot see each other's notes.

| Tool | Parameters | Description |
|---|---|---|
| `list_decks` | *none* | Lists all decks in the collection with their names and IDs. |
| `search_notes` | `query: string`<br>`limit?: int (1-100, default 50)`<br>`offset?: int (default 0)` | Search notes using standard Anki query syntax (e.g. `deck:Default tag:vocab`) with bounded pagination. |
| `get_note` | `note_id: int` | Fetches complete field values, model name, and tags for a note. |
| `get_collection_stats` | *none* | Returns reliably supported statistics: cards reviewed today, deck tree counts, and new/learn/review totals. |
| `sync_collection` | *none* | **Explicitly** synchronizes the collection with the user's AnkiWeb account (snapshot to `backups/` first, then full sync). Use this after mutations when changes should reach other clients — mutations do **not** auto-sync. |
| `add_notes` | `notes: list[NoteInput]`<br>`idempotency_key?: string`<br>`allow_duplicates?: bool` | Creates flashcards locally with duplicate detection, per-user locking, and ledger-backed exactly-once semantics. |
| `update_note_fields` | `note_id: int`<br>`fields: dict[str, str]` | Updates specific note fields under the per-user write lock. |
| `add_tags` | `note_ids: list[int]`<br>`tags: list[str]` | Adds tags to one or more notes under the per-user write lock. |

*Safety restriction*: Card/note deletion, raw database execution, and arbitrary add-on passthrough are deliberately omitted to preserve collection integrity.

---

## Security Architecture

1. **Fail-Closed Startup**:
   - The service exits unless `SESSION_SECRET` is set to a real (non-placeholder) value — sessions cannot be signed without it.
   - A fresh deployment with zero accounts must be bootstrapped: the service exits unless `ADMIN_USERNAME` and `ADMIN_PASSWORD` are set, so a bare instance can never come up unauthenticated.
2. **Accounts & Passwords**:
   - Passwords are stored only as salted **scrypt** hashes; verification is constant-time and burns equivalent work for unknown usernames (no enumeration oracle).
   - Login rate limiting: **5 consecutive failures lock the (username, IP) pair out for 15 minutes**.
3. **MCP Authentication**:
   - Per-user tokens; only their **sha256 hashes** are used for authentication (constant-time comparison).
   - `Authorization: Bearer <token>` and `X-API-Key: <token>` are the only accepted transports for the token (never query parameters).
   - Unknown paths require a valid token; only `/health` and the web-session routes are exempt.
4. **Web Sessions**:
   - Signed, `HttpOnly`, `SameSite=Strict` session cookies; every state-changing form carries a CSRF token bound to the session.
   - Deleting an account invalidates its token immediately (auth checks the store on every MCP request).
5. **Documented trade-offs (read before exposing publicly)**:
   - The portal **re-displays the raw MCP token** on demand, so tokens are stored **reversibly** in `users.json` — a must-redisplay secret cannot be hash-only. The file is `0600` on the service's volume, whose bytes are therefore token-grade sensitive. Records from before this field existed show "regenerate to view" until the token is regenerated.
   - **AnkiWeb credentials are also stored at rest** in the same `users.json` (the service must be able to sync unattended). Same file, same caveats: anyone who can read the volume can read them.
   - The service enforces a **single writer per collection**: do not open the same `/data/users/<name>/collection.anki2` from another Anki instance.
   - **HTTPS is required in front of the portal** for any non-localhost access (logins and tokens cross the wire). Use the Cloudflare or Coolify exposure modes, or your own TLS reverse proxy.
   - `/health` is intentionally unauthenticated; it reports only liveness/degradation state, never collection data.
   - In Cloudflare Tunnel mode, ingress rules live in the Cloudflare dashboard — route only port 8000.
6. **Log Redaction**: automatic filters mask Authorization headers, bearer tokens, passwords, API keys, and AnkiWeb credentials before writing to container logs.

---

## Troubleshooting

- **`/health` returns 503 / `degraded`**: the users store is unavailable (e.g. `users.json` unreadable/corrupt) or the data directory is missing/not writable (volume not mounted, permissions). Check `docker compose logs -f anki-mcp` for the `Health probe:` warnings.
- **`anki-mcp` restart-loops with `FATAL: SESSION_SECRET ...`**: the secret is empty or still the `.env.example` placeholder. Generate one (`openssl rand -hex 24`) and recreate the container.
- **`anki-mcp` exits with `no user accounts exist ... ADMIN_USERNAME`**: first boot without bootstrap credentials. Set both `ADMIN_USERNAME` and `ADMIN_PASSWORD` for the first start.
- **Portal login locked out**: 5 consecutive failures trigger a 15-minute lock per (username, source IP). Wait it out or correct the password; a successful login clears the counter.
- **Forgot the admin password**: use the recovery CLI from inside the container —
  ```bash
  docker compose stop anki-mcp
  docker compose run --rm anki-mcp python -m src.useradmin list
  docker compose run --rm anki-mcp python -m src.useradmin reset-password admin
  docker compose start anki-mcp
  ```
  Stop the service first: a running service holds the store in memory and would overwrite external edits on its next save.
- **Tool errors mention collection locks**: another process is writing the same `collection.anki2` (second replica, or a desktop Anki pointed at the volume). The service enforces one writer per collection — remove the competing writer.
- **Sync warnings about credentials / auth**: the user's AnkiWeb credentials are missing or were rejected — re-enter them in the portal (Credentials) and sync again.
- **A retry unexpectedly says `Previous attempt with this idempotency key failed`**: the key was recorded as `failed` after an ambiguous loss. Use a new idempotency key; old entries age out of the 1000-entry ledger.
- **`Bind for 0.0.0.0:8000 failed: port already allocated`**: another service holds the port — change the published port in `docker-compose.yml`.
- **After upgrading the image, older Anki clients complain**: the pinned `anki` wheel version defines the collection schema; see the upgrade notes in [Operations](#operations).

---

## Operations

- **Backup**: stop the service first (`docker compose stop anki-mcp`) for a consistent snapshot —
  ```bash
  docker run --rm -v mcp_data:/data -v "$PWD":/backup alpine tar czf /backup/anki-data-$(date +%F).tgz -C /data .
  ```
  In a pinch you may rely on the automatic pre-sync snapshots in `/data/users/<name>/backups/` (kept per `BACKUP_RETENTION`) and back up live, but a stopped-service volume backup is the safe path.
- **Restore**: `docker run --rm -v mcp_data:/data -v "$PWD":/backup alpine sh -c "cd /data && tar xzf /backup/anki-data-<date>.tgz"` then `docker compose start anki-mcp`.
- **User data**: everything for a user lives under `/data/users/<username>/` (collection, media, backups). Deleting an account in `/admin` **moves** that directory to `/data/deleted/<username>-<timestamp>` — nothing is destroyed; reclaim space manually when you are sure.
- **Upgrade**:
  ```bash
  git pull && docker compose build --pull && docker compose up -d
  ```
  **Read before upgrading**: the image pins `anki==26.9.3` exactly. Anki upgrades collection schemas **one-way** — once a collection is opened by a newer wheel it cannot be opened by an older one, so **downgrades are unsupported**. Take a volume backup first, and expect the first post-upgrade sync with older Anki clients to require updating those clients.
- **Logs**: `docker compose logs -f anki-mcp`

---

## Testing & Verification

The repository includes a comprehensive unit suite covering the user store, web auth/portal/admin routes, authentication middleware, collection registry, per-user tools, sync manager, idempotency persistence, and end-to-end MCP protocol behavior (including two-user isolation over the wire):

```bash
# Set up a Python environment and run tests
python3 -m venv .venv
source .venv/bin/activate
pip install -r docker/mcp-service/requirements.txt pytest pytest-asyncio httpx

PYTHONPATH=docker/mcp-service pytest tests/ -v
```

(`httpx` is test-only — it drives the ASGI in-process transports; the service itself does not import it.)

### End-to-End Verification Checklist
- [x] Single headless image builds natively on amd64 and arm64 (manylinux `anki` wheels).
- [x] Service starts fail-closed: refuses to run without `SESSION_SECRET`, and without bootstrap credentials on an empty store.
- [x] Streamable HTTP MCP server on port 8000 requires a valid per-user bearer token.
- [x] Per-user isolation verified at protocol level: two users' sessions on one instance see only their own notes and collections.
- [x] All 8 MCP tools execute against real per-user collections with per-user locking and ledger-backed idempotency.
- [x] `/health` reports 200 healthy and 503 degraded (users store unavailable / data dir not writable).

---

## History

v1 of this project ran a full **Anki Desktop GUI** in a KasmVNC-streamed container and drove it remotely over the **AnkiConnect** HTTP add-on — single-user, x86_64-only, ~4.6 GB image. The current design is the headless multi-user rewrite: the desktop (and AnkiConnect with it) was removed, collections are owned in-process by the `anki` Python wheel, and accounts, tokens, and sync are first-class. This is a **re-architecture, not an upgrade** — v1 volumes and GUI settings do not carry over.
