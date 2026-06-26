# xCaptcha Solver — Standalone Solving Service

Standalone microservice for solving [xCaptcha](https://xcaptcha.com) challenges with a **2captcha-compatible API**. Designed to run as a Docker container alongside the [Universal Captcha Solver](https://github.com/icemellow-me/universal-captcha-solver).

## Supported Challenge Types

- **text** — VLM (Cloudflare Workers AI) — 2×4 emoji grid, select 2 matching reference → ~70-80% accuracy
- **custom** — API leaked answer — Click symbols at coordinates → 100% accuracy
- **empty** — API leaked answer hash — No-op challenge → 100% accuracy
- **dynamics** — Not yet supported — WebSocket-based slide/puzzle (requires browser automation)

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
  -e SOLVER_API_KEY=your_api_key \
  xcaptcha-solver
```

### Direct

```bash
pip install aiohttp Pillow

CF_API_TOKEN=cfut_... CF_ACCOUNT_ID=... SOLVER_API_KEY=your_key python xcaptcha_solver.py
```

## API — 2captcha-compatible endpoints

This solver implements the **2captcha API protocol** (`/in.php` + `/res.php`), so it works as a drop-in replacement for any client that supports 2captcha/anti-captcha. Works with [CaptchaPlugin](https://captchaplugin.com) API keys out of the box.

### Submit a task

```
POST /in.php
key=YOUR_API_KEY&method=wcaptcha&sitekey=SITEKEY&pageurl=https://example.com
```

- `key` — Your API key (set via `SOLVER_API_KEY` env var, defaults to CaptchaPlugin key)
- `method` — Must be `wcaptcha`
- `sitekey` — The xCaptcha sitekey from the target page
- `pageurl` — The URL where the captcha is embedded

**Response:**
```
OK|12345678          ← task ID for polling
ERROR_WRONG_KEY     ← invalid API key
ERROR_WRONG_SITEKEY ← missing sitekey
```

### Poll for result

```
GET /res.php?key=YOUR_API_KEY&id=12345678
```

**Responses:**
```
CAPCHA_NOT_READY           ← still processing (retry after 5s)
OK|verification_token      ← solved! token is the xCaptcha response
ERROR|error_description    ← failed
```

### Check balance (compatibility)

```
GET /res.php?key=YOUR_API_KEY&action=getbalance
→ $0.00
```

### Example: Full solve cycle with curl

```bash
# 1. Submit
TASK_ID=$(curl -s -X POST http://localhost:8899/in.php \
  -d "key=8010000000ccojr5nrbg516w5jvw1wu9" \
  -d "method=wcaptcha" \
  -d "sitekey=a537c95d43097aed9cd8a295ecdc2a79" \
  -d "pageurl=https://xcaptcha.com/demo" | cut -d'|' -f2)

# 2. Poll until solved
while true; do
  RESULT=$(curl -s "http://localhost:8899/res.php?key=8010000000ccojr5nrbg516w5jvw1wu9&id=$TASK_ID")
  if echo "$RESULT" | grep -q "^OK|"; then
    echo "Solved: $RESULT"
    break
  fi
  sleep 5
done
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

## Integration with Universal Captcha Solver

The [Universal Captcha Solver](https://github.com/icemellow-me/universal-captcha-solver) automatically forwards xCaptcha tasks to this service. Set the `XCAPTCHA_SOLVER_URL` env var:

```bash
export XCAPTCHA_SOLVER_URL=http://localhost:8899
```

Then submit xCaptcha tasks to the universal solver using `method=wcaptcha`:

```bash
# Via universal solver (port 8855) — auto-forwards to xCaptcha solver (port 8899)
curl -X POST http://localhost:8855/in.php \
  -d "key=YOUR_API_KEY&method=wcaptcha&sitekey=SITEKEY&pageurl=https://example.com"
```

## CaptchaPlugin Compatibility

The default `SOLVER_API_KEY` is a CaptchaPlugin 2captcha-compatible API key. This means:

- Any tool that supports 2captcha API can point to this solver
- CaptchaPlugin browser extension users can use the same key for both reCAPTCHA v2 solving and xCaptcha solving
- No separate 2captcha subscription needed — CaptchaPlugin keys work directly

## Environment Variables

- `PORT` — Server port (default: `8899`)
- `CF_API_TOKEN` — Cloudflare Workers AI API token, `cfut_...` format (required for text-type VLM solving)
- `CF_ACCOUNT_ID` — Cloudflare account ID (required for text-type VLM solving)
- `SOLVER_API_KEY` — API key for 2captcha-compatible endpoints (default: CaptchaPlugin key)
- `TASK_TIMEOUT` — Max seconds per task (default: `120`)

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

## Test Results

All supported types tested against the [xCaptcha demo page](https://xcaptcha.com/demo) (sitekey `a537c95d43097aed9cd8a295ecdc2a79`):

- **empty** — ✅ Solved in ~10s
- **text** — ✅ Solved in ~10s (VLM-powered)
- **custom** — ✅ Solved in ~15s
- **dynamics** — ❌ Not yet supported (requires browser automation)

## License

MIT
