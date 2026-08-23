# Telehack — Hackathon Operations System

A self-hosted, web-based system for running hackathons: breakout rooms with video conferencing, automatic recording of every room, and passwordless email authentication against a participant roster.

## Features

- **Passwordless auth**: participants enter their email address; if it is on the roster, they receive a one-time login URL by email (15 min, single use). No accounts, no passwords.
- **Hackathon-scoped rosters & teams**: everything (roster, teams, rooms, works, votes) belongs to a hackathon. Creating a new hackathon starts fresh; team IDs are recreated per event. Creating a team auto-creates its breakout room.
- **Breakout rooms**: team rooms are created automatically; free rooms and a main hall exist too. Camera / mic / screen share, active-speaker highlight, click-to-enlarge tiles, auto-focus on screen share.
- **Recording**: rooms with *auto-record* enabled start recording (grid composite MP4) the moment the first participant joins and finalize when the last one leaves. Manual start/stop is also available. Files are downloadable from the admin console via signed URLs.
- **Organizer controls** (one button each):
  - broadcast a text announcement to every room (toast on all screens)
  - stream the organizer's camera / screen to every room (a special *stage* room fanned out by the SFU; every client watches it in an overlay)
  - summon everyone to the main hall, or dispatch everyone back to their team rooms
  - a shared countdown timer visible in every room
- **Presentation mode**: teams present in random order, each with a preset time slot streamed live to all participants via the stage; when the timer runs out the next team is automatically put on stage.
- **Works gallery**: each team registers the URL of what they built. When another participant plays a work, their game screen *and* their webcam are automatically recorded (a dedicated auto-recorded room). Author names can be shown or anonymized by the organizer.
- **Voting & awards**: participants vote for their favorite work from another team. The organizer reveals the top 3 one rank at a time (3rd → 2nd → 1st), each with a synthesized drumroll ceremony on every participant's screen.
- **Roster console**: hackathon switcher, team management, search, inline editing, team assignment, admin-rights toggle, bulk selection (send login links / delete), CSV import/export (`email,name,team,admin` — teams auto-created), and live status per participant (last login, logged in, currently in which room).

## Architecture

| Component | Role |
|---|---|
| [LiveKit Server](https://github.com/livekit/livekit) (Docker) | WebRTC SFU — ports 7880 (WS/API), 7881 (TCP fallback), 50000-50199/udp (media) |
| [LiveKit Egress](https://github.com/livekit/egress) (Docker) | Room composite recording → `recordings/*.mp4` |
| Redis (Docker) | LiveKit ↔ Egress messaging |
| Caddy (Docker) | HTTPS termination + reverse proxy (app + `/rtc` WebSocket), automatic Let's Encrypt |
| FastAPI (`server/`) | Roster, auth, rooms, recording control, web UI — port 8800 |

Data lives in SQLite (`data/telehack.db`). The frontend is plain static HTML/JS in `server/static/` (LiveKit JS SDK vendored, no build step).

## Setup

Requirements: Docker + Docker Compose, Python 3.10+.

```bash
git clone https://github.com/shi3z/telehack.git
cd telehack

# 1. Create your config from the templates
cp .env.example .env
cp livekit/livekit.yaml.example livekit/livekit.yaml
cp livekit/egress.yaml.example livekit/egress.yaml
cp caddy/Caddyfile.example caddy/Caddyfile

# 2. Generate a LiveKit API key/secret and put the SAME pair in all three files:
#    .env, livekit/livekit.yaml, livekit/egress.yaml
echo "key:    LK$(openssl rand -hex 8)"
echo "secret: $(openssl rand -hex 24)"

# 3. Edit .env: BASE_URL, ADMIN_EMAILS, SMTP settings
#    Edit caddy/Caddyfile: your domain (or <dashed-ip>.sslip.io) and email

# 4. Egress writes recordings as uid 1001 — make the dir writable
chmod o+rwx recordings

# 5. Run
./run.sh
```

Open your domain, log in with an address listed in `ADMIN_EMAILS`, and use the admin console (top right) to import the roster and create rooms.

### Email

While `SMTP_HOST` is empty the app runs in **dev mode**: login links are printed to the server log instead of being emailed. For production, fill in the SMTP settings (e.g. Gmail: `smtp.gmail.com:587` with an app password — note Gmail rejects direct unauthenticated sending, so a relay like this is required; sending limit is ~500/day).

### Exposing to the internet

- Browsers only allow camera/mic on HTTPS (or localhost) — the bundled Caddy handles certificates automatically once ports 80/443 are reachable and DNS resolves to your server. No domain? Use [sslip.io](https://sslip.io): `1-2-3-4.sslip.io` resolves to `1.2.3.4`.
- Open/forward: 80/tcp, 443/tcp, 7881/tcp, 50000-50199/udp.
- Set `use_external_ip: true` in `livekit.yaml` (default in the template) so media candidates advertise your public IP.
- `BASE_URL=https://your-domain` and `LIVEKIT_WS_URL=wss://your-domain` in `.env`.

## How recording works

The app receives LiveKit webhooks at `/api/lk-webhook`:

- `room_started` / `participant_joined` → if the room has auto-record on and no active recording, start a Room Composite Egress (grid layout, MP4).
- When the room empties, egress finalizes the file; `egress_ended` marks the recording *done* in the DB.

While recording, a ● REC badge is shown in the room and the lobby. Downloads use HMAC-signed URLs (12 h validity), so they work regardless of browser cookie policies.

## Notes

- All API endpoints require a session (admin endpoints require admin rights); LiveKit rooms require a server-issued join token, so exposing 7880/7881 directly is safe.
- Recording cost is CPU only — each active recording runs a headless-Chrome compositing pipeline in the egress container; budget roughly 2-4 cores per simultaneously recorded room.
- The default LiveKit config allows ~200 concurrent media ports; raise the UDP range for larger events.

## License

MIT
