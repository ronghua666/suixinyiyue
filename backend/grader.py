"""
Grader Engine
Handles the core RPA pipeline:
  1. Screenshot the answer region via mss.
  2. Send image + per-question config to Vision API (Ark Responses).
  3. Parse JSON response for score & reason.
  4. Use pyautogui to input the total score and click submit.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import base64
from io import BytesIO

import mss
import pyautogui
import pygetwindow as gw
import httpx
from PIL import Image

pyautogui.PAUSE = 0.1


def _minimize_app_window():
    """Find and minimize the 随心一阅 window. Returns the window for later restore."""
    import logging
    log = logging.getLogger("suixin.local")
    for w in gw.getAllWindows():
        title = w.title or ''
        if ('随心一阅' in title or '8766' in title) and not w.isMinimized:
            try:
                log.info(f"Minimizing app window: title={title!r}")
                w.minimize()
                return w
            except Exception:
                pass
    log.warning("Could not find app window by title, trying active window fallback")
    try:
        active = gw.getActiveWindow()
        if active and not active.isMinimized and active.width > 300 and active.height > 200:
            log.info(f"Fallback: minimizing active window: title={active.title!r}")
            active.minimize()
            return active
    except Exception:
        pass
    return None


def _keep_minimized(win):
    """Re-minimize a window if it was restored. Never touches other windows.
    Returns the window (refreshed) or None if the window no longer exists."""
    if win is None:
        return None
    import logging
    log = logging.getLogger("suixin.local")
    try:
        title = win.title or ''
        if not win.isMinimized:
            log.info(f"Re-minimizing restored window: title={title!r}")
            win.minimize()
        return win
    except Exception:
        # Window might have been closed
        log.warning(f"Window handle stale, re-scanning for app window")
        for w in gw.getAllWindows():
            t = w.title or ''
            if ('随心一阅' in title or '8766' in t) and not w.isMinimized:
                try:
                    w.minimize()
                    return w
                except Exception:
                    pass
        return win


def _restore_window(window):
    if window is None:
        return
    time.sleep(0.1)
    try:
        window.restore()
        window.activate()
    except Exception:
        pass


MAX_IMAGE_DIM = 1280


def _coerce_region(value, n: int = 4):
    """Convert a region dict {x,y,w,h} or {x,y} to a plain tuple. Pass-through tuples as-is."""
    def _to_int(v) -> int:
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    if isinstance(value, dict):
        if n == 4:
            return (_to_int(value.get("x", 0)), _to_int(value.get("y", 0)),
                    _to_int(value.get("w", 0)), _to_int(value.get("h", 0)))
        return (_to_int(value.get("x", 0)), _to_int(value.get("y", 0)))
    if isinstance(value, (list, tuple)):
        items = list(value)[:n]
        items.extend([0] * (n - len(items)))
        return tuple(_to_int(item) for item in items)
    return value


class GraderConfig:
    """Configuration for GraderEngine."""
    def __init__(self, answer_region=None, score_region=None, submit_region=None,
                 api_key="", base_url="", model="", standard_answer="",
                 grading_rubric="", questions=None):
        self.answer_region = _coerce_region(answer_region, 4) or (0, 0, 0, 0)
        self.score_region = _coerce_region(score_region, 2) or (0, 0)
        self.submit_region = _coerce_region(submit_region, 2) or (0, 0)
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.standard_answer = standard_answer
        self.grading_rubric = grading_rubric
        self.questions = questions or []


def _resize_image(img: Image.Image) -> Image.Image:
    """Resize image if larger than MAX_IMAGE_DIM to avoid connection resets."""
    w, h = img.size
    if w <= MAX_IMAGE_DIM and h <= MAX_IMAGE_DIM:
        return img
    ratio = MAX_IMAGE_DIM / max(w, h)
    return img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)


def _extract_text(data: dict) -> str:
    """Extract assistant text from Ark Responses API output."""
    for item in data.get("output", []):
        if item.get("type") == "message":
            for c in item.get("content", []):
                if c.get("type") == "output_text":
                    return c["text"]
    raise ValueError(f"Unexpected response format: {json.dumps(data, ensure_ascii=False)[:500]}")


PROMPT_TEMPLATE = """你是阅卷老师。根据以下标准答案和评分细则，对图片中的学生作答逐空评分。

【标准答案与评分细则】
{grading_content}

硬性评分规则：
- 每个空只能按该空配置的“满分”给分，score 必须在 0 到该空 max_score 之间。
- 任何一个空的 score 都不得超过该空 max_score。
- total_score 必须等于所有空的 score 之和。
- max_total 必须等于所有空的 max_score 之和。

逐空给出得分和扣分理由。严格输出JSON，不要其他内容：
{{
  "questions": [
    {{"number": "第1空", "score": <得分>, "max_score": <满分>, "reason": "<扣分理由>"}},
    {{"number": "第2空", "score": <得分>, "max_score": <满分>, "reason": "<扣分理由>"}}
  ],
  "total_score": <总分>,
  "summary": "<评语>"
}}"""


class GraderEngine:
    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        standard_answer: str = "",
        grading_rubric: str = "",
        questions: list = None,
        answer_region: tuple[int, int, int, int] = (0, 0, 0, 0),
        score_region: tuple[int, int] = (0, 0),
        submit_region: tuple[int, int] = (0, 0),
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip('/') if base_url else ""
        self.model = model
        self.standard_answer = standard_answer
        self.grading_rubric = grading_rubric
        self.questions = questions or []
        self.answer_region = _coerce_region(answer_region, 4) or (0, 0, 0, 0)
        self.score_region = _coerce_region(score_region, 2) or (0, 0)
        self.submit_region = _coerce_region(submit_region, 2) or (0, 0)

    @classmethod
    def from_config(cls, config) -> "GraderEngine":
        """Create engine from a GraderConfig dataclass/object."""
        return cls(
            api_key=config.api_key,
            base_url=config.base_url,
            model=config.model,
            standard_answer=config.standard_answer,
            grading_rubric=config.grading_rubric,
            questions=config.questions,
            answer_region=config.answer_region,
            score_region=config.score_region,
            submit_region=config.submit_region,
        )

    def run(self) -> dict:
        """Single-shot grading with window management."""
        win = _minimize_app_window()
        time.sleep(0.3)
        self._activate_exam_page()
        try:
            return self.run_one()
        finally:
            _restore_window(win)

    def run_one(self) -> dict:
        """Grade one paper. Assumes exam page is already visible and active."""
        self._activate_exam_page()

        # 1. Screenshot
        img_buf, img = self._capture_answer_region()

        # 2. Detect possible blanks, but do not auto-score them locally.
        # Sparse handwriting, a large selected region, or pale ink can look
        # "blank" to a pixel heuristic. Let the AI make the final decision.
        blank_suspected = self._is_blank(img)

        img_base64 = base64.b64encode(img_buf.read()).decode("utf-8")

        # 3. Build grading content from questions (preferred) or flat strings
        if self.questions:
            parts = []
            for q in self.questions:
                qnum = q.get("number", "")
                qans = q.get("standard_answer", "")
                qrub = q.get("grading_rubric", "")
                qmax = q.get("max_score", 0)
                block = f"【{qnum}】（本空满分{qmax}分，得分不得超过{qmax}分）\n标准答案：{qans}"
                if qrub:
                    block += f"\n评分细则：{qrub}"
                parts.append(block)
            grading_content = "\n\n".join(parts)
        else:
            grading_content = self.standard_answer
            if self.grading_rubric:
                grading_content += "\n\n【评分细则】\n" + self.grading_rubric

        # 4. Call Vision API
        response_text, usage = self._call_vision_api(img_base64, grading_content)

        # 5. Parse JSON
        result = self._parse_response(response_text)
        result = self._enforce_score_limits(result)
        result["usage"] = usage
        result["blank_suspected"] = blank_suspected

        # 6. RPA - reactivate exam page, input score, submit
        self._activate_exam_page()
        total = result.get("total_score", 0)
        if isinstance(total, (int, float)):
            self._input_score(self._clean_score(total))
            self._click_submit()

        return result

    def _activate_exam_page(self) -> None:
        """Click the answer region to ensure the exam page has focus."""
        ax, ay, aw, ah = self.answer_region
        pyautogui.click(ax + aw // 2, ay + ah // 2)
        time.sleep(0.15)

    def capture_hash(self) -> int:
        """Return a hash of the current answer region for change detection."""
        x, y, w, h = self.answer_region
        monitor = {"left": x, "top": y, "width": w, "height": h}
        with mss.mss() as sct:
            img = sct.grab(monitor)
            # Hash raw pixels (sample every 4th pixel for speed)
            raw = img.bgra
            return hash(raw[::16])

    def _capture_answer_region(self) -> tuple[BytesIO, Image.Image]:
        x, y, w, h = self.answer_region
        monitor = {"left": x, "top": y, "width": w, "height": h}
        with mss.mss() as sct:
            screenshot = sct.grab(monitor)
            img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
            img = _resize_image(img)
            buf = BytesIO()
            img.save(buf, format="JPEG", quality=75)
            buf.seek(0)
            return buf, img

    @staticmethod
    def _is_blank(img: Image.Image) -> bool:
        """Check if image is mostly blank (no student writing)."""
        gray = img.convert("L")
        # Count pixels darker than threshold (likely ink/handwriting)
        dark = sum(1 for p in gray.getdata() if p < 140)
        ratio = dark / (gray.width * gray.height)
        return ratio < 0.015  # less than 1.5% dark pixels = blank

    def _call_vision_api(self, img_base64: str, grading_content: str) -> tuple[str, dict]:
        prompt = PROMPT_TEMPLATE.format(grading_content=grading_content)

        body = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": f"data:image/jpeg;base64,{img_base64}",
                        },
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ],
        }

        url = f"{self.base_url}/responses"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        last_error = None
        for attempt in range(3):
            try:
                with httpx.Client(timeout=120.0) as client:
                    resp = client.post(url, json=body, headers=headers)
                    resp.raise_for_status()
                    data = resp.json()
                text = _extract_text(data)
                usage = data.get("usage", {})
                return text, usage
            except httpx.HTTPError as e:
                last_error = e
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        raise last_error

    @staticmethod
    def _parse_response(text: str) -> dict:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Cannot parse grading JSON: {text[:200]}")

    @staticmethod
    def _to_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clean_score(value: float) -> float | int:
        value = round(float(value), 2)
        return int(value) if value.is_integer() else value

    def _enforce_score_limits(self, result: dict) -> dict:
        """Clamp AI scores to the configured per-blank max scores."""
        if not isinstance(result, dict):
            return {"questions": [], "total_score": 0, "max_total": 0, "summary": "AI返回格式异常"}

        configured = []
        for q in self.questions:
            number = str(q.get("number", "")).strip()
            max_score = self._to_float(q.get("max_score", 0), 0)
            if number and max_score > 0:
                configured.append({"number": number, "max_score": max_score})

        configured_by_number = {q["number"]: q["max_score"] for q in configured}
        output_questions = result.get("questions") if isinstance(result.get("questions"), list) else []

        normalized = []
        total_score = 0.0

        for idx, out_q in enumerate(output_questions):
            if not isinstance(out_q, dict):
                continue
            number = str(out_q.get("number") or (configured[idx]["number"] if idx < len(configured) else f"第{idx + 1}空")).strip()
            max_score = configured_by_number.get(number)
            if max_score is None and idx < len(configured):
                max_score = configured[idx]["max_score"]
            if max_score is None:
                max_score = self._to_float(out_q.get("max_score", 0), 0)

            score = self._to_float(out_q.get("score", 0), 0)
            if max_score > 0:
                score = min(max(score, 0.0), max_score)
            else:
                score = max(score, 0.0)

            normalized_q = dict(out_q)
            normalized_q["number"] = number
            normalized_q["score"] = self._clean_score(score)
            normalized_q["max_score"] = self._clean_score(max_score)
            normalized.append(normalized_q)
            total_score += score

        # If the AI omitted some configured blanks, add zero-score rows so totals stay correct.
        seen = {q["number"] for q in normalized}
        for q in configured:
            if q["number"] in seen:
                continue
            normalized.append({
                "number": q["number"],
                "score": 0,
                "max_score": self._clean_score(q["max_score"]),
                "reason": "AI未返回该空评分，按0分处理",
            })

        total_score = sum(self._to_float(q.get("score", 0), 0) for q in normalized)
        if configured:
            max_total = sum(q["max_score"] for q in configured)
        else:
            max_total = sum(self._to_float(q.get("max_score", 0), 0) for q in normalized)

        result["questions"] = normalized
        result["total_score"] = self._clean_score(total_score)
        result["max_total"] = self._clean_score(max_total)
        return result

    def _input_score(self, score) -> None:
        x, y = self.score_region
        pyautogui.click(x, y)
        time.sleep(0.05)
        pyautogui.hotkey("ctrl", "a")
        # Use clipboard paste for instant input (much faster than typewrite)
        subprocess.run(["clip"], input=str(score), text=True, shell=False)
        pyautogui.hotkey("ctrl", "v")

    def _click_submit(self) -> None:
        x, y = self.submit_region
        pyautogui.click(x, y)


def wait_for_next_paper(
    engine: GraderEngine,
    prev_hash: int,
    timeout: float = 30.0,
    stable_seconds: float = 1.0,
    min_ready_seconds: float = 2.0,
    blank_grace_seconds: float = 5.0,
) -> bool:
    """Wait for the next paper to load and settle after submit.

    Some grading sites briefly show an empty answer area while turning pages. If
    the loop grades during that gap, the blank-paper detector gives 0. Wait for
    the region to change, stay stable, and avoid accepting early blank frames.
    """
    deadline = time.time() + timeout
    first_change_at = None
    last_hash = None
    stable_since = None

    while time.time() < deadline:
        time.sleep(0.35)
        try:
            current_hash = engine.capture_hash()
        except Exception:
            continue

        now = time.time()
        if current_hash == prev_hash:
            first_change_at = None
            last_hash = None
            stable_since = None
            continue

        if first_change_at is None:
            first_change_at = now
            last_hash = current_hash
            stable_since = now
            continue

        if current_hash != last_hash:
            last_hash = current_hash
            stable_since = now
            continue

        if now - stable_since < stable_seconds:
            continue
        if now - first_change_at < min_ready_seconds:
            continue

        try:
            _, img = engine._capture_answer_region()
            if engine._is_blank(img) and now - first_change_at < blank_grace_seconds:
                continue
        except Exception:
            pass

        time.sleep(0.3)
        return True

    return False
