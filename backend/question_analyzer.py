"""
Question Analyzer
Takes a screenshot of the answer region and uses Vision API (Ark Responses)
to analyze the number of individual answer blanks and their descriptions.

Before capturing, minimizes the 随心一阅 window so the screenshot captures
the exam page, not the 随心一阅 UI. Restores the window after analysis.
"""

from __future__ import annotations

import json
import re
import time
import base64
from io import BytesIO

import mss
import httpx
import pygetwindow as gw
from PIL import Image


MAX_IMAGE_DIM = 1280


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


def _minimize_app_window():
    """Find and minimize the 随心一阅 window. Returns the window for later restore."""
    for w in gw.getAllWindows():
        title = w.title or ''
        if '随心一阅' in title and not w.isMinimized:
            try:
                w.minimize()
                return w
            except Exception:
                pass
    # Fallback: minimize active window if it's large enough
    try:
        active = gw.getActiveWindow()
        if active and not active.isMinimized and active.width > 300 and active.height > 200:
            active.minimize()
            return active
    except Exception:
        pass
    return None


def _restore_window(window):
    if window is None:
        return
    time.sleep(0.2)
    try:
        window.restore()
        window.activate()
    except Exception:
        pass


ANALYZE_PROMPT = """请仔细分析这张答题卡图片，识别出所有需要学生手写填写的**独立填空位置**。

关键规则：
- 按每一个独立的填空位置来拆分，不要合并！
- 如果一道大题有①、②、③等多个需要分别填写的位置，每个都算一个独立的空
- 每个空单独输出一条记录

对每个空提取：
1. 编号（按顺序标记为：第1空、第2空、第3空……）
2. 该空需要填写内容的简要描述

例如，答题卡上有：
  第20.4题包含①说明测量溶液温度变化与反应速率变化对比的作用；
  ②解释NaCl使反应速率加快的原因；③判断反应步骤的先后顺序
→ 应输出3个条目：第1空（作用说明）、第2空（原因解释）、第3空（顺序判断）

请严格按照以下 JSON 数组格式输出，max_score 固定为 0，不要输出任何其他内容：
[
  {"number": "第1空", "description": "填写内容描述", "max_score": 0},
  {"number": "第2空", "description": "填写内容描述", "max_score": 0}
]

即使只有一个填空位置，也输出单元素数组。"""


class AnalysisConfig:
    """Configuration for QuestionAnalyzer (compatibility export)."""
    def __init__(self, api_key="", base_url="https://ark.cn-beijing.volces.com/api/v3",
                 model="doubao-seed-2-0-mini-260428"):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model


class QuestionAnalyzer:
    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip('/')
        self.model = model

    def analyze(self, region: tuple[int, int, int, int]) -> list[dict]:
        # Minimize 随心一阅 so screenshot sees the exam page
        win = _minimize_app_window()
        time.sleep(0.3)

        try:
            # 1. Screenshot
            x, y, w, h = (int(float(v or 0)) for v in region)
            monitor = {"left": x, "top": y, "width": w, "height": h}
            with mss.mss() as sct:
                screenshot = sct.grab(monitor)
                img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
                img = _resize_image(img)
                buf = BytesIO()
                img.save(buf, format="JPEG", quality=75)
                buf.seek(0)

            img_base64 = base64.b64encode(buf.read()).decode("utf-8")

            # 2. Call Vision API
            response_text = self._call_vision_api(img_base64)

            # 3. Parse JSON
            return self._parse_response(response_text)
        finally:
            _restore_window(win)

    def _call_vision_api(self, img_base64: str) -> str:
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
                        {"type": "input_text", "text": ANALYZE_PROMPT},
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
                return _extract_text(data)
            except httpx.HTTPError as e:
                last_error = e
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
        raise last_error

    @staticmethod
    def _parse_response(text: str) -> list[dict]:
        try:
            result = json.loads(text)
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            try:
                result = json.loads(match.group(0))
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass

        raise ValueError(f"Cannot parse question list JSON: {text[:200]}")
