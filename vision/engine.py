"""
vision/engine.py

Sandboxed Vision Engine for PRIMUS.

Design principles:
- Async-first: never blocks UI or Orchestrator
- Memory-safe: screenshots live only in RAM as BytesIO objects
- OCR graceful degradation: engine remains functional without Tesseract
- Private Mode: disables all capture and immediately wipes buffers
- Lazy loading: heavy modules imported only on first use
"""
from __future__ import annotations

import io
import asyncio
import time
from typing import Optional
from loguru import logger
from config.settings import settings
from audit.trail import audit


class OCRProvider:
    """
    Abstraction layer for OCR engines.
    Concrete implementations can be swapped (Tesseract, EasyOCR, cloud) without
    touching the rest of the Vision Engine.
    """
    @property
    def available(self) -> bool:
        raise NotImplementedError

    def extract_text(self, image_bytes: bytes) -> str:
        raise NotImplementedError


class TesseractOCRProvider(OCRProvider):
    """Tesseract-backed OCR provider. Gracefully marks itself unavailable if binary not found."""

    def __init__(self):
        self._available = False
        self._pytesseract = None
        self._pil_image = None
        self._probe()

    def _probe(self):
        """Detect Tesseract binary without crashing."""
        try:
            import pytesseract
            from PIL import Image
            # Attempt to call tesseract version; raises on missing binary
            pytesseract.get_tesseract_version()
            self._pytesseract = pytesseract
            self._pil_image = Image
            self._available = True
            logger.info("Tesseract OCR detected — OCR features enabled.")
        except Exception as e:
            logger.warning(f"Tesseract OCR unavailable: {e}. Vision Engine running in screenshot-only mode.")
            self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def extract_text(self, image_bytes: bytes) -> str:
        if not self._available:
            return ""
        try:
            img = self._pil_image.open(io.BytesIO(image_bytes))
            return self._pytesseract.image_to_string(img)
        except Exception as e:
            logger.error(f"OCR extraction failed: {e}")
            return ""


class VisionEngine:
    """
    Sandboxed async vision system for PRIMUS.
    """

    def __init__(self):
        self._ocr: Optional[OCRProvider] = None
        self._mss = None
        self._cv2 = None
        self._loaded = False
        self.ocr_available = False

    def _lazy_load(self):
        """
        Deferred heavy module loading — only runs when vision is first invoked.
        Keeps idle memory footprint near zero.
        """
        if self._loaded:
            return
        try:
            import mss
            import cv2
            import numpy as np
            self._mss = mss
            self._cv2 = cv2
            self._np = np
            logger.info("Vision Engine modules loaded.")
        except ImportError as e:
            logger.error(f"Vision Engine module load failed: {e}")
            raise

        # Probe OCR provider
        self._ocr = TesseractOCRProvider()
        self.ocr_available = self._ocr.available
        self._loaded = True

    async def capture_screenshot(self) -> Optional[bytes]:
        """
        Captures a screenshot into an in-memory buffer.
        Returns raw PNG bytes or None on failure.
        Never writes to disk.
        """
        if settings.private_mode:
            logger.warning("Vision Engine: Screenshot suppressed — Private Mode active.")
            return None

        loop = asyncio.get_running_loop()
        try:
            self._lazy_load()
            image_bytes = await loop.run_in_executor(None, self._capture_sync)
            audit.log_event("VISION_SCREENSHOT_CAPTURED", {"private_mode": False})
            return image_bytes
        except Exception as e:
            logger.error(f"Screenshot capture failed: {e}")
            return None

    def _capture_sync(self) -> bytes:
        """Blocking screenshot call — always run in executor thread."""
        with self._mss.mss() as sct:
            monitor = sct.monitors[1]  # Primary monitor
            raw = sct.grab(monitor)
            # Convert to PNG bytes in memory
            from PIL import Image
            img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue()

    async def extract_text(self, image_bytes: bytes) -> str:
        """
        Runs OCR on in-memory image bytes.
        Returns empty string if OCR is unavailable.
        """
        if not self.ocr_available:
            return ""
        if settings.private_mode:
            return ""

        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, self._ocr.extract_text, image_bytes)
        return text.strip()

    async def analyze_desktop(self) -> dict:
        """
        High-level desktop analysis — captures screenshot and extracts text.
        Returns a structured context dict.
        """
        self._lazy_load()
        result = {
            "timestamp": time.time(),
            "screenshot_available": False,
            "ocr_text": "",
            "ocr_available": self.ocr_available,
        }

        image_bytes = await self.capture_screenshot()
        if image_bytes:
            result["screenshot_available"] = True
            result["ocr_text"] = await self.extract_text(image_bytes)
            # Explicitly release buffer reference
            del image_bytes

        return result


# Module-level singleton — only instantiated on first use
_vision_engine: Optional[VisionEngine] = None


def get_vision_engine() -> VisionEngine:
    global _vision_engine
    if _vision_engine is None:
        _vision_engine = VisionEngine()
    return _vision_engine
