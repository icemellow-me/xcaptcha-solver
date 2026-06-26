# xCaptcha Solver — Standalone Solving Service

Standalone microservice for solving [xCaptcha](https://xcaptcha.com) challenges with a **2captcha-compatible API**. Designed to run as a Docker container alongside the [Universal Captcha Solver](https://github.com/icemellow-me/universal-captcha-solver).

## Supported Challenge Types

| Type | Method | Description | Accuracy |
|------|--------|-------------|----------|
| `text` | VLM (Cloudflare Workers AI) | 2×4 emoji grid — select 2 matching reference | ~70-80% |
| `custom` | API leaked answer | Click symbols at coordinates | 100% |
| `empty` | API leaked answer hash | No-op challenge | 100% |
| `dynamics` | ❌ Not supported | WebSocket-based slide/puzzle | — |

## Architecture

```
┌─────────────────────────┐     ┌──────────────────────────┐     ┌─────────────────────┐
│  Universal Captcha       │────→│  xCaptcha Solver :8899   │────→│  api.xcaptcha.com   │
│  Solver (main server)    │     │                          │     │                     │
│  Forwards wcaptcha type  │     │  ┌─ init_session() ────┐ │     │  GET /init          │
│                          │     │  │  Get CAPTCHA_SESSION│ │     │  GET /task          │
│  POST /in.php ──────────→│     │  │  Gen Bfp fingerprint│ │     │  GET /task/{answer} │
│  GET  /res.php ←────────│     │  └─────────────────────┘ │     │                     │
│                          │     │                          │     └─────────────────────┘
│                          │     │  ┌─ solve_text_vlm() ──┐ │              │
│                          │     │  │  Deobfuscate image  │ │              │
│                          │     │  │  Extract 8 cells    │ │     ┌────────┴────────┐
│                          │     │  │  CF VLM → match     │ │     │ Cloudflare AI   │
│                          │     │  │  Format answer      │ │     │ llama-3.2-11b   │
│                          │     │  └─────────────────────┘ │     │ vision-instruct │
│                          │     │                          │     └─────────────────┘
│                          │     │  ┌─ solve_custom() ───┐ │
│                          │     │  │  API leaks coords! │ │
│                          │     │  └─────────────────────┘ │
│                          │     │                          │
│                          │     │  ┌─ solve_empty() ────┐ │
│                          │     │  │  API leaks answer! │ │
│                          │     │  └─────────────────────┘ │
└─────────────────────────┘     └──────────────────────────┘
```

## Quick Start

### Docker

```bash
docker build -t xcaptcha-solver .

docker run -d \
  --name xcaptcha-solver \
  -p 8899:8899 \
  -e CF_API_TOKEN=cfut_your_token_here \
  -e CF_ACCOUNT_ID=your_account_id \
  -e SOLVER_API_KEY=1 \
  xcaptcha-solver
```

### Direct

```bash
pip install aiohttp Pillow

CF_API_TOKEN=cfut_... CF_ACCOUNT_ID=... python xcaptcha_solver.py
```

## API

### 2captcha-compatible

**Submit task:**
```
POST /in.php
key=1&method=wcaptcha&sitekey=11aa62606fb968f3674742df60598957&pageurl=https://example.com

→ OK|12345678
```

**Poll result:**
```
GET /res.php?key=1&id=12345678

→ CAPCHA_NOT_READY  (still processing)
→ OK|verification_token  (solved!)
```

### Direct solve (JSON)

```bash
curl -X POST http://localhost:8899/solve \
  -H "Content-Type: application/json" \
  -d '{"sitekey": "11aa62606fb968f3674742df60598957"}'

# Response:
# {"success": true, "token": "verification_token", "type": "text"}
```

### Health check

```bash
curl http://localhost:8899/health

# {"status":"ok","service":"xcaptcha-solver","version":"1.0.0","vlm_provider":"cloudflare","cf_model":"@cf/meta/llama-3.2-11b-vision-instruct","supported_types":["text","custom","empty"]}
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8899` | Server port |
| `CF_API_TOKEN` | — | Cloudflare Workers AI API token (`cfut_...`) |
| `CF_ACCOUNT_ID` | — | Cloudflare account ID |
| `SOLVER_API_KEY` | `1` | API key for 2captcha-compatible endpoints |
| `TASK_TIMEOUT` | `120` | Max seconds per task |

## Text-Type Solving Strategy

The text-type captcha shows a 2×4 grid (8 cells) of emojis with one reference emoji at the top. The instruction: *"Assemble from 2 elements the same code as shown above"* — pick exactly 2 cells matching the reference.

### Two-Phase VLM Approach

1. **Full-image analysis** — Send the complete captcha image to Cloudflare's `llama-3.2-11b-vision-instruct` model with a prompt asking it to identify the 2 matching cells by (col, row) position
2. **Per-cell fallback** — If the full-image approach fails (wrong number of cells), restructure the image with clear grid labels and send again with a more specific prompt

### Answer Format

```javascript
// Frontend JS:
checked = {}
checked[btoa("1x2")] = 3     // col=1, row=2 → cell number 3
checked[btoa("2x3")] = 6     // col=2, row=3 → cell number 6
answer = btoa(JSON.stringify(checked))  // → "eyJNWG...oyfQ=="

// Submission:
GET /captcha/{siteKey}/task/{answer}
Headers: Wcaptcha-Key: {task.key}, Captcha-Session: {session}
```

## xCaptcha API Vulnerabilities (Security Research)

### 1. Image "Obfuscation" is Trivial

The `img` field uses a simple byte substitution on PNG:
- `/` → `|b|` and `&` → `(a)`
- Deobfuscation is hardcoded in the publicly shipped `app.js`
- No cryptographic key material — fully deterministic

### 2. Custom Type Leaks Ground-Truth Coordinates

The `/task` API returns `coords` with exact `{x, y, letter}` values. No image recognition needed.

### 3. Empty Type Leaks Answer Hash

The `/task` API returns `answer` field with the verification token directly.

## Research

Full reverse-engineering details in [xcaptcha-research](https://github.com/icemellow-me/xcaptcha-research).

## License

MIT
