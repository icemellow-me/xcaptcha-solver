#!/usr/bin/env python3
"""
xCaptcha Solver Service — Standalone solving service for xCaptcha challenges.

Supports:
  - text:   2×4 emoji grid, select 2 matching reference (VLM-powered)
  - custom: Click symbols at coordinates (API-leaked answer)
  - empty:  No-op with leaked answer hash

2captcha-compatible API on port 8899:
  POST /in.php   — submit task
  GET  /res.php  — poll result
  GET  /health   — health check
  POST /solve    — direct solve (JSON in/out)

Uses Cloudflare Workers AI (llama-3.2-11b-vision) for emoji matching.
"""

import asyncio
import aiohttp
from aiohttp import web
import base64
import json
import hashlib
import io
import os
import re
import sys
import time
import uuid
import logging
import urllib.request
import urllib.parse
from PIL import Image
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional, List, Tuple

# ──────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("xcaptcha-solver")

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
API_BASE = "https://api.xcaptcha.com"
SITE_KEYS = {
    "text":     "11aa62606fb968f3674742df60598957",
    "dynamics": "506195d06393f98584931a6ede3cb64c",
    "custom":   "5b4fc1a221c3e79c9bac190363808884",
    "empty":    "a537c95d43097aed9cd8a295ecdc2a79",
}

# Cloudflare Workers AI config — read at call time to allow env vars set after import
def _cf_token():
    return os.environ.get("CF_API_TOKEN", "")
def _cf_account():
    return os.environ.get("CF_ACCOUNT_ID", "")
CF_VISION_MODEL = "@cf/meta/llama-3.2-11b-vision-instruct"
CF_TEXT_MODEL = "@cf/meta/llama-4-scout-17b-16e-instruct"

# Solver state
SOLVER_API_KEY = os.environ.get("SOLVER_API_KEY", "1")
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "120"))


# ──────────────────────────────────────────────
# Task model
# ──────────────────────────────────────────────
class TaskStatus(str, Enum):
    PENDING = "processing"
    SOLVED = "solved"
    FAILED = "failed"


@dataclass
class SolverTask:
    task_id: str
    status: str = TaskStatus.PENDING
    result: str = ""
    error: str = ""
    created_at: float = field(default_factory=time.time)
    solved_at: float = 0.0
    # Input
    sitekey: str = ""
    pageurl: str = ""
    method: str = "wcaptcha"
    # Internal
    _future: object = field(default=None, repr=False)


# Task store
tasks: Dict[str, SolverTask] = {}


# ──────────────────────────────────────────────
# Image Deobfuscation
# ──────────────────────────────────────────────
def deobfuscate_image(img_b64: str) -> bytes:
    """Reverse xCaptcha's PNG byte obfuscation."""
    raw = base64.b64decode(img_b64)
    raw_str = raw.decode("latin-1")
    deobfuscated = raw_str.replace("|b|", "/").replace("(a)", "&")
    return deobfuscated.encode("latin-1")


def deobfuscated_to_image(img_b64: str) -> Image.Image:
    """Deobfuscate and return PIL Image."""
    png_bytes = deobfuscate_image(img_b64)
    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


# ──────────────────────────────────────────────
# Answer Formatting
# ──────────────────────────────────────────────
def format_text_answer(selected_cells: list, blocks_x: int = 2) -> str:
    """
    Build answer for text-type: btoa(JSON.stringify({btoa(col+"x"+row): getNum}))
    selected_cells: list of (col, row) tuples, 1-based
    """
    checked = {}
    for col, row in selected_cells:
        key = base64.b64encode(f"{col}x{row}".encode()).decode()
        num = (row - 1) * blocks_x + col
        checked[key] = num
    return base64.b64encode(json.dumps(checked).encode()).decode()


def format_custom_answer(coords: list) -> str:
    """Build answer for custom-type: btoa(JSON.stringify(clicks))."""
    return base64.b64encode(json.dumps(coords).encode()).decode()


# ──────────────────────────────────────────────
# Bfp Fingerprint Generation
# ──────────────────────────────────────────────
def generate_bfp(
    audio_hash: str = "124.04347527516074",
    canvas_hash: str = "149822569",
    webgl_renderer: str = "ANGLE (Google, Vulkan 1.3.0 (SwiftShader Device (Subzero) (0x0000C0DE)), SwiftShader driver)",
    locale: str = "en-US",
) -> str:
    """Generate a Bfp (Browser Fingerprint) header — double-base64."""
    audio_b64 = base64.b64encode(audio_hash.encode()).decode()
    canvas_b64 = base64.b64encode(canvas_hash.encode()).decode()
    webgl_b64 = base64.b64encode(webgl_renderer.encode()).decode()
    inner = f"{audio_b64}:{canvas_b64}:{webgl_b64}:{locale}"
    return base64.b64encode(inner.encode()).decode()


# ──────────────────────────────────────────────
# Cloudflare Workers AI VLM
# ──────────────────────────────────────────────
async def cf_ensure_agreement(session: aiohttp.ClientSession):
    """Accept model license terms if not already accepted."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{_cf_account()}/ai/models/search"
    headers = {"Authorization": f"Bearer {_cf_token()}"}
    async with session.get(url, headers=headers) as resp:
        await resp.read()  # consume


async def cf_vlm_analyze(image_b64: str, prompt: str) -> str:
    """
    Send image + prompt to Cloudflare Workers AI vision model.
    image_b64: standard base64 of PNG/RGB image
    Returns: model text response
    """
    if not _cf_token() or not _cf_account():
        raise ValueError("CF_API_TOKEN and CF_ACCOUNT_ID must be set for VLM solving")

    url = f"https://api.cloudflare.com/client/v4/accounts/{_cf_account()}/ai/run/{CF_VISION_MODEL}"
    headers = {"Authorization": f"Bearer {_cf_token()}"}
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
        "max_tokens": 256,
    }

    async with aiohttp.ClientSession() as cf_session:
        # Try with agreement header
        headers["cf-model-agreement"] = "true"
        async with cf_session.post(url, headers=headers, json=payload) as resp:
            if resp.status == 403:
                # License not accepted — agree first via curl
                agreement_url = f"https://api.cloudflare.com/client/v4/accounts/{_cf_account()}/ai/models/license_agreement/{CF_VISION_MODEL}"
                proc = await asyncio.create_subprocess_exec(
                    "curl", "-s", "-X", "POST", agreement_url,
                    "-H", f"Authorization: Bearer {_cf_token()}",
                    "-H", "Content-Type: application/json",
                    "-d", json.dumps({"agreement": True}),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
                # Retry
                async with cf_session.post(url, headers=headers, json=payload) as resp2:
                    data = await resp2.json()
            else:
                data = await resp.json()

    result = data.get("result", {})
    errors = data.get("errors", [])
    if errors:
        log.error(f"CF API errors: {errors}")
    if result is None:
        raise Exception(f"CF API returned null result (errors: {errors})")
    if isinstance(result, dict) and "response" in result:
        resp = result["response"]
        # CF API may return parsed JSON (list/dict) or string
        if isinstance(resp, (list, dict)):
            return json.dumps(resp)
        return str(resp)
    # Try nested format
    if isinstance(result, dict):
        choices = result.get("choices", [])
        if choices:
            content = choices[0].get("message", {}).get("content", "")
            if isinstance(content, (list, dict)):
                return json.dumps(content)
            return str(content)
    if isinstance(result, (list, dict)):
        return json.dumps(result)
    return str(result)


def image_to_b64(img: Image.Image, fmt: str = "PNG") -> str:
    """Convert PIL Image to base64 string."""
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode()


# ──────────────────────────────────────────────
# xCaptcha API Client
# ──────────────────────────────────────────────
class XcaptchaClient:
    """Async client for xCaptcha API with solving."""

    def __init__(self, site_key: str):
        self.site_key = site_key
        self.session: Optional[aiohttp.ClientSession] = None
        self.captcha_session: Optional[str] = None
        self.bfp: Optional[str] = None
        self.task_data: Optional[dict] = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={
                "Origin": "https://xcaptcha.com",
                "Referer": "https://xcaptcha.com/",
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
            }
        )
        return self

    async def __aexit__(self, *args):
        if self.session:
            await self.session.close()

    async def init_session(self):
        """Initialize: fetch iframe → extract session → send init."""
        # 1. Fetch iframe page to get CAPTCHA_SESSION
        iframe_url = f"{API_BASE}/captcha/{self.site_key}/?lang=en&orig_lang=en"
        async with self.session.get(iframe_url) as resp:
            html = await resp.text()

        match = re.search(r"CAPTCHA_SESSION\s*=\s*'([^']+)'", html)
        if not match:
            raise Exception("Could not extract CAPTCHA_SESSION from iframe")
        self.captcha_session = match.group(1)
        log.info(f"Session: {self.captcha_session}")

        # 2. Generate Bfp
        self.bfp = generate_bfp()

        # 3. Send /init
        init_headers = {
            "Captcha-Session": self.captcha_session,
            "Bfp": self.bfp,
            "Dn": "",
            "client": f"{int(time.time()*1000)}.{hashlib.md5(self.captcha_session.encode()).hexdigest()[:6]}",
            "wparams": "20.1280.720.1280.1",
        }
        async with self.session.get(
            f"{API_BASE}/captcha/{self.site_key}/init",
            headers=init_headers,
        ) as resp:
            init_data = await resp.json()
            log.info(f"Init response: {init_data}")
        return init_data

    async def get_task(self, lang: str = "en") -> dict:
        """Fetch a new task."""
        if not self.captcha_session:
            await self.init_session()

        url = f"{API_BASE}/captcha/{self.site_key}/task?lang={lang}"
        async with self.session.get(url, headers={
            "Captcha-Session": self.captcha_session,
            "D-id": self.bfp or generate_bfp(),
        }) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise Exception(f"Task API error {resp.status}: {text[:200]}")
            self.task_data = await resp.json()
        return self.task_data

    async def check_answer(self, answer: str, task_key: str = None) -> dict:
        """Submit answer for verification."""
        key = task_key or self.task_data["key"]
        # URL-encode the answer to safely embed base64 in path (avoids +/= corruption)
        encoded_answer = urllib.parse.quote(answer, safe="")
        url = f"{API_BASE}/captcha/{self.site_key}/task/{encoded_answer}"
        async with self.session.get(url, headers={
            "Wcaptcha-Key": key,
            "Captcha-Session": self.captcha_session,
        }) as resp:
            text = await resp.text()
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                log.warning(f"Non-JSON response ({resp.status}): {text[:200]}")
                return {"success": False, "error": f"HTTP {resp.status}", "raw": text[:200]}


# ──────────────────────────────────────────────
# Text-Type VLM Solver
# ──────────────────────────────────────────────
def extract_cells(img: Image.Image, bx: int = 2, by: int = 4) -> Tuple[List[dict], Image.Image]:
    """
    Extract individual cell images and instruction area from text-type task.
    Returns: (list of cell dicts, instruction PIL Image)
    """
    cell_w, cell_h = 140, 55
    cells = []
    for row in range(1, by + 1):
        for col in range(1, bx + 1):
            y_offset = (row - 1) * 55 + 5
            x_offset = (col - 1) * 140
            cell_img = img.crop((
                x_offset, y_offset,
                x_offset + cell_w, y_offset + cell_h
            ))
            key = base64.b64encode(f"{col}x{row}".encode()).decode()
            num = (row - 1) * bx + col
            cells.append({
                "col": col, "row": row,
                "getNum": num, "key": key,
                "image": cell_img,
            })

    # Instruction area: y=220 to y=320
    instruction_img = img.crop((0, 220, 280, 320))
    return cells, instruction_img


async def solve_text_vlm(task: dict, client: XcaptchaClient) -> str:
    """
    Solve text-type captcha using Cloudflare VLM.
    Returns the formatted answer string.
    """
    from PIL import ImageDraw, ImageFont
    img = deobfuscated_to_image(task["img"])
    bx = task["blocks"]["x"]
    by = task["blocks"]["y"]

    cells, instruction_img = extract_cells(img, bx, by)
    log.info(f"Grid: {bx}×{by}, {len(cells)} cells extracted")

    # ── Reconstruct image for VLM: instruction on TOP, labeled grid below ──
    # This matches the visual layout the human sees and makes the VLM's job easier
    cell_w, cell_h = 140, 55
    label_h = 18  # space for row/col labels
    grid_display_w = bx * cell_w
    grid_display_h = by * cell_h
    canvas_w = max(280, grid_display_w + label_h)
    canvas_h = instruction_img.height + 10 + label_h + grid_display_h + 10

    canvas = Image.new("RGB", (canvas_w, canvas_h), (255, 255, 255))
    draw = ImageDraw.Draw(canvas)

    # 1) Paste instruction emoji at top
    instr_resized = instruction_img.resize((280, 80), Image.LANCZOS)
    canvas.paste(instr_resized, (0, 0))

    # 2) Add column headers
    y_start = 90
    for col in range(1, bx + 1):
        x = label_h + (col - 1) * cell_w + cell_w // 2 - 5
        draw.text((x, y_start), f"C{col}", fill="blue")

    # 3) Paste each cell with row labels
    for cell in cells:
        col, row = cell["col"], cell["row"]
        x = label_h + (col - 1) * cell_w
        y = y_start + label_h + (row - 1) * cell_h
        canvas.paste(cell["image"].resize((cell_w, cell_h), Image.LANCZOS), (x, y))
        # Row label on the left
        draw.text((2, y + cell_h // 2 - 6), f"R{row}", fill="blue")

    labeled_b64 = image_to_b64(canvas)

    prompt = (
        "This CAPTCHA shows a reference emoji at the top. Below is a grid of 8 emoji cells "
        f"arranged in {by} rows (R1-R{by}) and {bx} columns (C1-C{bx}). "
        "Your task: Find exactly 2 cells that contain the SAME emoji as the reference. "
        "Reply with ONLY the cell positions as JSON: "
        '[{"col":C,"row":R},{"col":C,"row":R}] '
        "where col is 1-2 and row is 1-4. Example: [{\"col\":1,\"row\":2},{\"col\":2,\"row\":3}] "
        "Do NOT explain. Output ONLY the JSON array."
    )

    log.info("Sending labeled captcha image to VLM...")
    vlm_response = await cf_vlm_analyze(labeled_b64, prompt)
    log.info(f"VLM response: {vlm_response}")

    # Parse VLM response
    selected = parse_vlm_cells(vlm_response, bx, by)

    if not selected or len(selected) != 2:
        # Method 2: Try sending instruction + each cell individually for matching
        log.info("Full-image VLM didn't give 2 cells, trying per-cell comparison...")
        selected = await solve_text_per_cell(cells, instruction_img)

    if not selected or len(selected) != 2:
        raise Exception(f"VLM failed to identify 2 matching cells (got {len(selected) if selected else 0})")

    log.info(f"Selected cells: {[(c['col'], c['row']) for c in selected]}")
    answer = format_text_answer([(c["col"], c["row"]) for c in selected], bx)
    log.info(f"Formatted answer: {answer}")
    return answer


def parse_vlm_cells(response: str, bx: int, by: int) -> list:
    """Parse VLM response into list of cell dicts with col/row."""
    # 1) Try direct JSON parse (response might already be valid JSON)
    try:
        parsed = json.loads(response)
        if isinstance(parsed, list):
            cells = parsed
        elif isinstance(parsed, dict):
            cells = [parsed]
        else:
            cells = []
    except (json.JSONDecodeError, TypeError):
        # 2) Extract JSON array from text
        json_match = re.search(r'\[.*?\]', response, re.DOTALL)
        if not json_match:
            # 3) Try to find individual {"col":C,"row":R} objects
            single_cells = re.findall(r'\{[^}]*"col"\s*:\s*\d+[^}]*"row"\s*:\s*\d+[^}]*\}', response)
            if not single_cells:
                single_cells = re.findall(r'\{[^}]*"row"\s*:\s*\d+[^}]*"col"\s*:\s*\d+[^}]*\}', response)
            if single_cells:
                try:
                    cells = [json.loads(c) for c in single_cells]
                except json.JSONDecodeError:
                    return []
            else:
                return []
        else:
            try:
                cells = json.loads(json_match.group())
            except json.JSONDecodeError:
                return []

    selected = []
    for cell in cells:
        if isinstance(cell, dict) and "col" in cell and "row" in cell:
            col, row = int(cell["col"]), int(cell["row"])
            if 1 <= col <= bx and 1 <= row <= by:
                selected.append({"col": col, "row": row,
                                 "getNum": (row - 1) * bx + col})

    return selected


async def solve_text_per_cell(cells: list, instruction_img: Image.Image) -> list:
    """
    Fallback: Send instruction image + each cell individually to VLM.
    Ask which 2 cells contain the same emoji as the instruction.
    """
    instr_b64 = image_to_b64(instruction_img)

    # Create a combined image: instruction on top, cells in grid below
    cell_imgs = [c["image"] for c in cells]
    # Create labeled grid
    label_size = 140 * 2, 55 * 4
    grid_img = Image.new("RGB", label_size, (255, 255, 255))
    for i, c in enumerate(cells):
        row_offset = (c["row"] - 1) * 55
        col_offset = (c["col"] - 1) * 140
        grid_img.paste(c["image"], (col_offset, row_offset))

    # Combine instruction + grid
    combined = Image.new("RGB", (280, 100 + 55 * 4 + 20), (255, 255, 255))
    # Resize instruction to fit
    instr_resized = instruction_img.resize((280, 100), Image.LANCZOS)
    combined.paste(instr_resized, (0, 0))
    combined.paste(grid_img, (0, 110))

    combined_b64 = image_to_b64(combined)

    prompt = (
        "TOP: reference emoji. BOTTOM: 2×4 grid of 8 emoji cells (columns 1-2, rows 1-4). "
        "Pick exactly 2 cells that show the SAME emoji as the reference on top. "
        "Reply ONLY: [{\"col\":C,\"row\":R},{\"col\":C,\"row\":R}] 1-based positions. "
        "No explanation, just JSON."
    )

    vlm_response = await cf_vlm_analyze(combined_b64, prompt)
    log.info(f"Per-cell VLM response: {vlm_response}")
    return parse_vlm_cells(vlm_response, 2, 4)


# ──────────────────────────────────────────────
# Custom-Type Solver (Leaked Answer)
# ──────────────────────────────────────────────
def solve_custom_leaked(task: dict) -> str:
    """Custom type: API leaks ground-truth coordinates."""
    coords = task.get("coords", [])
    if not coords:
        raise ValueError("No coords leaked — API may have been patched")
    if isinstance(coords, str):
        coords = json.loads(coords)
    log.info(f"LEAKED {len(coords)} target coordinates")
    clicks = [{"x": c["x"], "y": c["y"]} for c in coords]
    return format_custom_answer(clicks)


# ──────────────────────────────────────────────
# Empty-Type Solver (Leaked Answer)
# ──────────────────────────────────────────────
def solve_empty_leaked(task: dict) -> str:
    """Empty type: API leaks the answer hash directly."""
    answer = task.get("answer", "")
    if not answer:
        raise ValueError("No answer hash leaked")
    log.info(f"LEAKED answer hash: {answer}")
    return answer


# ──────────────────────────────────────────────
# Main Solver Orchestrator
# ──────────────────────────────────────────────
async def solve_xcaptcha(sitekey: str, max_retries: int = 3) -> dict:
    """Full solve flow: init → get task → solve → verify. Retries on text-type failure."""
    for attempt in range(1, max_retries + 1):
        async with XcaptchaClient(sitekey) as client:
            await client.init_session()
            task = await client.get_task()
            task_type = task.get("type", "unknown")
            task_key = task.get("key", "")

            log.info(f"[Attempt {attempt}/{max_retries}] Task type: {task_type}")

            try:
                if task_type == "text":
                    answer = await solve_text_vlm(task, client)
                elif task_type == "custom":
                    answer = solve_custom_leaked(task)
                elif task_type == "empty":
                    answer = solve_empty_leaked(task)
                elif task_type == "dynamics":
                    return {"success": False, "error": "dynamics type requires browser automation"}
                else:
                    return {"success": False, "error": f"unknown type: {task_type}"}

                # Verify answer
                result = await client.check_answer(answer, task_key)
                log.info(f"Verification result: {result}")

                if result.get("success"):
                    return {"success": True, "token": result.get("answer", ""), "type": task_type, "attempts": attempt}
                else:
                    # Text type: VLM may have misidentified — retry with fresh session
                    if task_type == "text" and attempt < max_retries:
                        log.warning(f"VLM failed attempt {attempt}, retrying with fresh session...")
                        await asyncio.sleep(1)  # brief pause
                        continue
                    return {
                        "success": False,
                        "error": "verification failed",
                        "hint": result.get("c", ""),
                        "type": task_type,
                        "attempts": attempt,
                    }
            except Exception as e:
                if attempt < max_retries:
                    log.warning(f"Solve error attempt {attempt}: {e}, retrying...")
                    await asyncio.sleep(1)
                    continue
                return {"success": False, "error": str(e), "type": task_type, "attempts": attempt}

    return {"success": False, "error": f"failed after {max_retries} attempts", "type": "text", "attempts": max_retries}


# ──────────────────────────────────────────────
# 2captcha-compatible API handlers
# ──────────────────────────────────────────────
async def handle_inphp(request: web.Request):
    """POST /in.php — 2captcha-compatible task submission."""
    try:
        data = await request.post()
    except Exception:
        data = {}

    key = data.get("key", "")
    method = data.get("method", "wcaptcha")
    sitekey = data.get("sitekey", data.get("wcaptcha", ""))
    pageurl = data.get("pageurl", "")

    if key != SOLVER_API_KEY:
        return web.Response(text="ERROR_WRONG_KEY", status=403)

    if not sitekey:
        return web.Response(text="ERROR_WRONG_SITEKEY", status=400)

    # Create task
    task_id = str(uuid.uuid4().int)[:8]
    task = SolverTask(
        task_id=task_id,
        sitekey=sitekey,
        pageurl=pageurl,
        method=method,
    )
    tasks[task_id] = task

    # Start solving in background
    loop = asyncio.get_event_loop()
    task._future = loop.create_task(_run_solver(task))

    log.info(f"Task {task_id} created: method={method} sitekey={sitekey}")
    return web.Response(text=f"OK|{task_id}")


async def handle_resphp(request: web.Request):
    """GET /res.php — 2captcha-compatible result polling."""
    key = request.query.get("key", "")
    task_id = request.query.get("id", "")
    action = request.query.get("action", "")

    if key != SOLVER_API_KEY:
        return web.Response(text="ERROR_WRONG_KEY", status=403)

    if action == "getbalance":
        return web.Response(text="$0.00")

    task = tasks.get(task_id)
    if not task:
        return web.Response(text="ERROR_NO_SUCH_CAPTCHA_ID", status=404)

    if task.status == TaskStatus.PENDING:
        return web.Response(text="CAPCHA_NOT_READY")
    elif task.status == TaskStatus.SOLVED:
        return web.Response(text=f"OK|{task.result}")
    else:
        return web.Response(text=f"ERROR|{task.error}")


async def handle_solve(request: web.Request):
    """POST /solve — Direct solve endpoint (JSON in/out)."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    sitekey = body.get("sitekey", "")
    method = body.get("method", "wcaptcha")

    if not sitekey:
        # Try to resolve from method type
        if method in SITE_KEYS:
            sitekey = SITE_KEYS[method]
        else:
            return web.json_response({"error": "sitekey required"}, status=400)

    try:
        result = await solve_xcaptcha(sitekey)
        return web.json_response(result)
    except Exception as e:
        log.error(f"Solve error: {e}", exc_info=True)
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def handle_health(request: web.Request):
    """GET /health — Health check."""
    return web.json_response({
        "status": "ok",
        "service": "xcaptcha-solver",
        "version": "1.0.0",
        "vlm_provider": "cloudflare" if _cf_token() else "none",
        "cf_model": CF_VISION_MODEL if _cf_token() else "",
        "supported_types": ["text", "custom", "empty"],
    })


# ──────────────────────────────────────────────
# Background solver runner
# ──────────────────────────────────────────────
async def _run_solver(task: SolverTask):
    """Run solver in background, update task status on completion."""
    try:
        result = await solve_xcaptcha(task.sitekey)
        if result.get("success"):
            task.result = result.get("token", "")
            task.status = TaskStatus.SOLVED
            task.solved_at = time.time()
            log.info(f"Task {task.task_id} SOLVED: {task.result[:40]}...")
        else:
            task.error = result.get("error", "unknown error")
            task.status = TaskStatus.FAILED
            log.warning(f"Task {task.task_id} FAILED: {task.error}")
    except Exception as e:
        task.error = str(e)
        task.status = TaskStatus.FAILED
        log.error(f"Task {task.task_id} EXCEPTION: {e}", exc_info=True)


# ──────────────────────────────────────────────
# App factory
# ──────────────────────────────────────────────
def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/in.php", handle_inphp)
    app.router.add_get("/res.php", handle_resphp)
    app.router.add_post("/solve", handle_solve)
    app.router.add_get("/health", handle_health)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8899"))
    log.info(f"xCaptcha Solver starting on port {port}")
    log.info(f"Cloudflare AI: {'configured' if _cf_token() else 'NOT configured'}")
    web.run_app(create_app(), host="0.0.0.0", port=port)
