# Anki-WebMCP

> A self-hosted, headless Anki Desktop environment running in Docker with official AnkiWeb synchronization, a browser-accessible streamed desktop GUI, and an authenticated Streamable HTTP Model Context Protocol (MCP) server for AI clients.

---

## Table of Contents

1. [Overview & Philosophy](#overview--philosophy)
2. [Architecture](#architecture)
3. [Write Safety & Synchronization Model](#write-safety--synchronization-model)
4. [Quick Start](#quick-start)
5. [First-Run Setup: Logging into AnkiWeb](#first-run-setup-logging-into-ankiweb)
6. [Exposure Modes](#exposure-modes)
   - [Mode 1: Cloudflare Tunnel](#mode-1-cloudflare-tunnel)
   - [Mode 2: Coolify HTTPS Reverse Proxy](#mode-2-coolify-https-reverse-proxy)
7. [MCP Client Integration](#mcp-client-integration)
8. [Available MCP Tools](#available-mcp-tools)
9. [Security Architecture](#security-architecture)
10. [Testing & Verification](#testing--verification)

---

## Overview & Philosophy

### What this is
- **Actual Anki Desktop** running on a virtual X11 display inside Docker.
- **Official AnkiWeb Cloud Sync** through the real Anki client engine.
- **Streamed Desktop GUI via KasmVNC** accessible in any web browser with HTTP Basic Authentication.
- **Remote Streamable HTTP MCP Server** exposing standard `/mcp` endpoints protected by Bearer token authentication for AI assistants (Claude, Cursor, custom agents).
- **Single-user scope**: Optimized for one account, one profile, one worker, and one MCP process without bloat.

### What this is NOT
- This is **not** a replacement Anki backend, custom sync server, or web scraper.
- The browser interface is a **streamed virtual desktop session** (rendered via KasmVNC/WebSocket), not a native responsive HTML website. It provides full fidelity to the real Anki application, allowing you to configure add-ons, review cards, manage profiles, and resolve modal dialogs.
- Audio playback in flashcards is delivered over the KasmVNC/PulseAudio stream to supported browsers.

---

## Architecture

The system consists of two core containerized services and an optional exposure gateway:

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              EXPOSURE LAYER                                 │
│                                                                             │
│   Option A: Cloudflare Tunnel (cloudflared)                                 │
│   Option B: Coolify Reverse Proxy (Traefik with Let's Encrypt HTTPS)        │
└───────────────────────┬───────────────────────────────┬─────────────────────┘
                        │ HTTPS                         │ HTTPS / Streamable HTTP
                        ▼                               ▼
       ┌────────────────────────────────┐  ┌────────────────────────────────┐
       │          anki-worker           │  │          mcp-service           │
       │                                │  │                                │
       │  • Anki Desktop (Pinned 24.11) │  │  • Python 3.12 + MCP SDK       │
       │  • KasmVNC Web UI (Port 3000)  │  │  • Streamable HTTP (/mcp:8000) │
       │  • Openbox Virtual Display     │  │  • Bearer Token Authentication │
       │  • AnkiConnect (Internal 8765) │  │  • Write-Safety Lock Manager   │
       │  • Volume: anki_data (/config) │  │  • Idempotency Ledger (/data)  │
       └────────────────┬───────────────┘  └────────────────┬───────────────┘
                        │                                   │
                        │        Private Docker Bridge      │
                        └─────────── (anki-net) ────────────┘
                         AnkiConnect private communication
                         (Port 8765 NOT exposed to host)
```

### Component Details
1. **`anki-worker`**:
   - Extends the maintained `ghcr.io/linuxserver/baseimage-kasmvnc:debianbookworm` base image.
   - Installs pinned official release **Anki 24.11** with full Qt6 runtime libraries.
   - Pre-installs and pre-configures **AnkiConnect** (Add-on `2055492159`).
   - Binds AnkiConnect to `0.0.0.0:8765` within the private container network and enforces `ANKI_CONNECT_API_KEY`.
   - Protects the desktop web UI with HTTP Basic Authentication (`ANKI_GUI_USER` / `ANKI_GUI_PASSWORD`).

2. **`mcp-service`**:
   - Python 3.12 service running the official Model Context Protocol SDK (`mcp`).
   - Serves the **Remote Streamable HTTP transport** at `/mcp` on port 8000.
   - Enforces Bearer token / API key authentication (`MCP_AUTH_TOKEN`).
   - Sanitizes and redacts all secrets and tokens from logs.
   - Coordinates mutation jobs using an in-process lock and persistent idempotency ledger.

3. **Private Network Security**:
   - Port `8765` (AnkiConnect) is strictly **never published** to the host machine or public internet.
   - Raw VNC port `5900` is disabled in favor of authenticated KasmVNC streaming.

---

## Write Safety & Synchronization Model

Distributed writes across local Anki Desktop and remote AnkiWeb follow a strict 6-stage lifecycle for all mutations (`add_notes`, `update_note_fields`, `add_tags`):

```mermaid
sequenceDiagram
    autonumber
    actor Client as AI Client / User
    participant MCP as mcp-service
    participant Ledger as Idempotency Ledger
    participant Anki as Anki Desktop (Worker)
    participant Cloud as Official AnkiWeb

    Client->>MCP: Mutation Request (e.g. add_notes + idempotency_key)
    activate MCP
    Note over MCP: 1. Acquire Operation Lock
    Note over MCP: 2. Check Maintenance Mode
    
    MCP->>Ledger: 3. Check Idempotency Key
    alt Key already committed
        Ledger-->>MCP: Return cached result
        MCP-->>Client: Replay cached committed result
    end

    MCP->>Anki: 4. Request Pre-Sync
    Anki->>Cloud: Synchronize collection
    Cloud-->>Anki: Sync result
    Anki-->>MCP: Pre-sync status (ok / auth_not_configured)

    MCP->>Anki: 5. Apply Mutation (validate & execute)
    Anki-->>MCP: Mutation outcome (note IDs / updated fields)
    
    MCP->>Ledger: 6. Commit Record (Atomic FS write)

    MCP->>Anki: 7. Request Post-Sync
    Anki->>Cloud: Synchronize collection
    Cloud-->>Anki: Post-sync result
    Anki-->>MCP: Post-sync status

    MCP-->>Client: 8. Return Local Status & Sync Status Separately
    deactivate MCP
```

### Safety Guarantees
- **No blind retries**: Mutations are never automatically retried on network drops to prevent duplicate card creation.
- **Separate status reporting**: If a local write succeeds in Anki Desktop but subsequent post-sync fails (e.g. temporary AnkiWeb timeout), the response reports:
  ```json
  {
    "success": true,
    "local_status": "committed",
    "sync_status": { "pre_sync": "ok", "post_sync": "failed" },
    "warnings": ["Local write succeeded in Anki Desktop, but post-mutation synchronization to AnkiWeb failed. Changes are saved locally and will synchronize on the next cycle."]
  }
  ```
- **Duplicate Prevention**: `add_notes` pre-validates note candidates using Anki's duplicate scope checks (`canAddNotes`) before executing creation.
- **Crash-Window Semantics**: The idempotency ledger is flushed atomically to disk using `tempfile` and `os.replace`. If a power loss occurs between step 5 (Anki DB write) and step 6 (ledger commit), Anki's first-field duplicate checking prevents accidental duplicates on retry if `allow_duplicates` is false.

---

## Quick Start

### 1. Clone & Configure
```bash
git clone https://github.com/your-username/Anki-WebMCP.git
cd Anki-WebMCP

# Copy environment template
cp .env.example .env
```

### 2. Generate Secrets
Edit `.env` and fill in your passwords and keys:
```bash
# Generate strong secrets
openssl rand -hex 24   # Use for ANKI_CONNECT_API_KEY
openssl rand -hex 24   # Use for MCP_AUTH_TOKEN
openssl rand -base64 16 # Use for ANKI_GUI_PASSWORD
```

Example `.env`:
```env
ANKI_CONNECT_API_KEY=a94f83b2e71c99f12d45a89b43e7610c12e834b971a82f01
MCP_AUTH_TOKEN=e4b78901ca9f8812de45b901a76f2310b89e34a1708234ef
ANKI_GUI_USER=admin
ANKI_GUI_PASSWORD=MySecretGuiPassword123!
ANKI_GUI_PORT=3000
MCP_PORT=8000
TZ=UTC
```

### 3. Start the Core Services
```bash
docker compose up -d
```

Verify services are healthy:
```bash
docker compose ps
curl http://localhost:8000/health
```

Expected health output:
```json
{"status":"healthy","service":"anki-mcp","anki_connected":true,"maintenance_mode":false,"auth_enabled":true}
```

---

## First-Run Setup: Logging into AnkiWeb

Before the MCP service can synchronize notes with AnkiWeb, you must log into your official AnkiWeb account once via the desktop GUI:

1. Open **http://localhost:3000** in your browser (or your configured HTTPS domain).
2. Enter the HTTP Basic Auth credentials (`ANKI_GUI_USER` and `ANKI_GUI_PASSWORD`).
3. You will see the live Anki Desktop running in your browser:
   ![Anki Desktop Streamed GUI](https://raw.githubusercontent.com/chrislongros/anki-desktop-docker/main/.github/screenshot.png)
4. In Anki Desktop, click the **Sync** button (or navigate to `Tools` -> `Preferences` -> `Syncing`).
5. Enter your official **AnkiWeb username/email** and **password**.
6. Click **Log In** and synchronize your collection.
7. Once logged in, your credentials and full profile (decks, media, notes) are permanently persisted in the `anki_data` Docker volume. Future syncs triggered via MCP or automatically will proceed seamlessly without GUI interaction.

---

## Exposure Modes

Keep the core application identical while choosing your exposure strategy:

### Mode 1: Cloudflare Tunnel

Recommended for self-hosters without a public IP or open router ports.

1. Create a Tunnel in your [Cloudflare Zero Trust Dashboard](https://one.dash.cloudflare.com/) (`Networks` -> `Tunnels`).
2. Configure Public Hostnames in the Cloudflare Dashboard:
   - `anki.yourdomain.com` -> `HTTP` -> `anki-worker:3000` (Enable WebSocket support)
   - `anki-mcp.yourdomain.com` -> `HTTP` -> `mcp-service:8000`
3. Add your Tunnel token to `.env`:
   ```env
   CLOUDFLARE_TUNNEL_TOKEN=eyJhIjoi...
   ```
4. Start with the Cloudflare override:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.cloudflare.yml up -d
   ```
5. *(Optional)* Add **Cloudflare Access** (Zero Trust SSO / Email OTP) in front of `anki.yourdomain.com` for multi-factor authentication.

### Mode 2: Coolify HTTPS Reverse Proxy

Recommended for self-hosters running Coolify.

1. In Coolify, create a new project and select **Docker Compose**.
2. Deploy using `docker-compose.yml` and `docker-compose.coolify.yml`.
3. Set the environment variables in Coolify's Environment settings:
   ```env
   ANKI_GUI_DOMAIN=anki.yourdomain.com
   ANKI_MCP_DOMAIN=anki-mcp.yourdomain.com
   ANKI_CONNECT_API_KEY=your-anki-key
   MCP_AUTH_TOKEN=your-mcp-bearer-token
   ANKI_GUI_USER=admin
   ANKI_GUI_PASSWORD=your-gui-password
   ```
4. Coolify's Traefik reverse proxy will automatically issue Let's Encrypt certificates and route:
   - `https://anki.yourdomain.com` -> `anki-worker:3000` (WebSocket-enabled streamed GUI)
   - `https://anki-mcp.yourdomain.com` -> `mcp-service:8000` (Streamable HTTP MCP)

---

## MCP Client Integration

AI clients connect to the MCP server over the **Remote Streamable HTTP transport** at `/mcp`.

### Claude Desktop Configuration

Add the following to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "anki": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-fetch",
        "https://anki-mcp.yourdomain.com/mcp"
      ],
      "env": {
        "HTTP_HEADER_AUTHORIZATION": "Bearer your-mcp-bearer-token"
      }
    }
  }
}
```

Or for direct HTTP clients supporting standard MCP Streamable HTTP / SSE:
- **URL**: `https://anki-mcp.yourdomain.com/mcp`
- **Headers**:
  ```http
  Authorization: Bearer <MCP_AUTH_TOKEN>
  Accept: application/json, text/event-stream
  ```

---

## Available MCP Tools

| Tool | Parameters | Description |
|---|---|---|
| `list_decks` | *none* | Lists all decks in the collection with their names and IDs. |
| `search_notes` | `query: string`<br>`limit?: int (1-100, default 50)`<br>`offset?: int (default 0)` | Search notes using standard Anki query syntax (e.g. `deck:Default tag:vocab`) with bounded pagination. |
| `get_note` | `note_id: int` | Fetches complete field values, model name, and tags for a note. |
| `get_collection_stats` | *none* | Returns reliably supported statistics: cards reviewed today, deck tree counts, and new/learn/review totals. |
| `sync_collection` | *none* | Explicitly triggers synchronization with official AnkiWeb cloud account under operation lock. |
| `add_notes` | `notes: list[NoteInput]`<br>`idempotency_key?: string`<br>`allow_duplicates?: bool` | Creates flashcards with pre-sync, duplicate detection, atomic ledger commit, and post-sync. |
| `update_note_fields` | `note_id: int`<br>`fields: dict[str, str]` | Updates specific note fields under write-safety lock with pre- and post-sync. |
| `add_tags` | `note_ids: list[int]`<br>`tags: list[str]` | Adds tags to one or more notes under write-safety lock with pre- and post-sync. |

*Safety restriction*: Card/note deletion, raw database execution, and unrestricted AnkiConnect passthrough are deliberately omitted to preserve collection integrity.

---

## Security Architecture

1. **Private Server-to-Server AnkiConnect**:
   - AnkiConnect listens on port `8765` solely on the internal Docker network `anki-net`.
   - Port 8765 is **never exposed** on host interfaces or reverse proxies.
   - All internal calls must supply `ANKI_CONNECT_API_KEY`.
2. **KasmVNC Desktop Protection**:
   - HTTP Basic Authentication is enforced before streaming the virtual X11 canvas.
   - Compatible with Cloudflare Access (SSO / 2FA).
3. **MCP Service Security**:
   - Constant-time HMAC comparison prevents timing attacks on `MCP_AUTH_TOKEN`.
   - Supports `Authorization: Bearer <token>` and `X-API-Key: <token>`.
4. **Log Redaction**:
   - Automatic regex filters mask Authorization headers, bearer tokens, passwords, API keys, and AnkiWeb credentials before writing to container logs.

---

## Testing & Verification

The repository includes a comprehensive 35-test unit suite covering client parsing, authentication middleware, write-safety coordinators, and idempotency persistence:

```bash
# Set up a Python environment and run tests
python3 -m venv .venv
source .venv/bin/activate
pip install -r docker/mcp-service/requirements.txt pytest pytest-asyncio respx

PYTHONPATH=docker/mcp-service pytest tests/ -v
```

### End-to-End Verification Checklist
- [x] Pinned Anki Desktop 24.11 builds and runs headlessly on Debian Bookworm virtual display.
- [x] KasmVNC web server delivers interactive streamed UI on port 3000 with basic auth.
- [x] AnkiConnect starts automatically on internal port 8765 with API key verification.
- [x] Streamable HTTP MCP server starts on port 8000 and requires Bearer token authentication.
- [x] All 8 MCP tools execute against live Anki Desktop with write-safety serialization and idempotency.
- [x] Port 8765 and raw VNC are isolated from public network interfaces.
