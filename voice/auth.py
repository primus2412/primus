"""
voice/auth.py

Owner Voice Authentication System for PRIMUS.

Architecture:
    Microphone → VAD → SpeechBrain ECAPA-TDNN → Embedding → Cosine Similarity
    → Confidence Level → Authorization Decision → Audit Log

Security guarantees:
  - Raw audio never persisted to disk
  - Voice embeddings stored encrypted via Secure Vault
  - All auth attempts logged to immutable audit trail
  - Replay/spoofing resistance via noise analysis + duration check
  - CPU-first; no CUDA assumptions (AMD iGPU compatible)

Confidence levels:
  VERIFIED      — cosine similarity > 0.85  (full access)
  LOW_CONFIDENCE — 0.70–0.85                (low-risk tasks only; fallback prompt)
  UNKNOWN       — 0.50–0.70                 (denied; fallback to PIN/password)
  BLOCKED       — < 0.50 or spoofing flag   (hard block + audit event)
"""
from __future__ import annotations

# ── torchaudio / SpeechBrain compatibility shims ─────────────────────────────
# torchaudio >= 2.1 removed list_audio_backends() and restructured torchaudio.io.
# SpeechBrain 1.0 still references these APIs at IMPORT TIME (not lazy).
# We must monkeypatch BEFORE any speechbrain import occurs.
import sys
import types
import os
import shutil

def _apply_torchaudio_patches():
    """Apply compatibility shims for torchaudio >= 2.1 used by SpeechBrain 1.0."""
    try:
        import torchaudio as _ta

        # Patch 1: list_audio_backends removed in torchaudio 2.1+
        if not hasattr(_ta, "list_audio_backends"):
            _ta.list_audio_backends = lambda: ["soundfile"]

        # Patch 2: torchaudio.io.StreamReader removed in torchaudio 2.2+
        if not hasattr(_ta, "io"):
            _io_mock = sys.modules.get("torchaudio.io")
            if _io_mock is None:
                _io_mock = types.ModuleType("torchaudio.io")
                _io_mock.StreamReader = object
                sys.modules["torchaudio.io"] = _io_mock
            _ta.io = _io_mock
        elif not hasattr(_ta.io, "StreamReader"):
            _ta.io.StreamReader = object

        # Patch 3: torchaudio.backend removed in torchaudio 2.2+
        if not hasattr(_ta, "backend"):
            _backend_mock = sys.modules.get("torchaudio.backend")
            if _backend_mock is None:
                _backend_mock = types.ModuleType("torchaudio.backend")
                _backend_mock.no_backend = object
                sys.modules["torchaudio.backend"] = _backend_mock
            _ta.backend = _backend_mock

    except ImportError:
        pass  # torchaudio not available — SpeechBrain will handle this gracefully

    # Patch 4: os.symlink on Windows fails without SeCreateSymbolicLinkPrivilege
    if not hasattr(os, "_original_symlink"):
        os._original_symlink = os.symlink
        def _safe_symlink(src, dst, target_is_directory=False):
            """Attempt to create a symlink; on failure fallback to copy.

            Handles relative source paths and normalizes Windows separators.
            Logs fallback activation for audit purposes.
            """
            # Resolve source relative to destination if needed
            if not os.path.isabs(src):
                src = os.path.normpath(os.path.join(os.path.dirname(dst), src))
            # Ensure destination parent exists
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                os._original_symlink(src, dst, target_is_directory)
            except (OSError, PermissionError) as e:
                logger.warning(f"os.symlink fallback activated: {e}. Copying instead.")
                if os.path.isdir(src):
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
                # Verify copy succeeded
                if not os.path.exists(dst):
                    raise RuntimeError(f"Fallback copy failed for {src} -> {dst}")
        os.symlink = _safe_symlink

    # Patch 5: pathlib.Path.symlink_to fallback for Windows without privilege
    import pathlib
    if not hasattr(pathlib.Path, "_original_symlink_to"):
        pathlib.Path._original_symlink_to = pathlib.Path.symlink_to
        def _safe_path_symlink(self, target, target_is_directory=False):
            """Fallback for Path.symlink_to when symlink creation is not permitted.
            Copies the source to the target location preserving directory structure.
            """
            src = self
            dst = pathlib.Path(target)
            # Resolve relative target against source parent if needed
            if not dst.is_absolute():
                dst = (src.parent / dst).resolve()
            # Ensure parent exists
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                src._original_symlink_to(target, target_is_directory)
            except (OSError, PermissionError) as e:
                logger.warning(f"Path.symlink_to fallback activated: {e}. Copying instead.")
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
                if not dst.exists():
                    raise RuntimeError(f"Fallback copy failed for {src} -> {dst}")
            return dst
        pathlib.Path.symlink_to = _safe_path_symlink

# Apply patches immediately at module import time
_apply_torchaudio_patches()

# ── Standard imports (after patches) ─────────────────────────────────────────
import io
import json
import time
import asyncio
import base64
from enum import Enum
from typing import Optional
from loguru import logger
from audit.trail import audit
from security.vault import vault
from config.settings import settings


class ConfidenceLevel(str, Enum):
    VERIFIED       = "VERIFIED"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    UNKNOWN        = "UNKNOWN"
    BLOCKED        = "BLOCKED"


class AuthDecision:
    """Returned by the authenticator after evaluating a voice sample."""
    def __init__(self, level: ConfidenceLevel, score: float, reason: str):
        self.level  = level
        self.score  = score
        self.reason = reason
        self.timestamp = time.time()

    def allows_high_risk(self) -> bool:
        return self.level == ConfidenceLevel.VERIFIED

    def allows_low_risk(self) -> bool:
        return self.level in (ConfidenceLevel.VERIFIED, ConfidenceLevel.LOW_CONFIDENCE)

    def __repr__(self) -> str:
        return f"AuthDecision({self.level.value}, score={self.score:.3f}, reason={self.reason})"


class VoiceAuthenticator:
    """
    Manages owner voice enrollment and runtime authentication
    using SpeechBrain's ECAPA-TDNN speaker encoder.

    Lazy-loaded: SpeechBrain is imported only on first use to keep
    startup fast on low-power hardware.
    """

    VAULT_KEY = "voice_profile_embedding"

    # Thresholds (cosine similarity, higher = more similar)
    VERIFIED_THRESHOLD       = 0.85
    LOW_CONF_THRESHOLD       = 0.70
    UNKNOWN_THRESHOLD        = 0.50

    # Anti-spoofing heuristics
    MIN_DURATION_SECONDS     = 1.0   # Reject sub-second recordings
    MAX_DURATION_SECONDS     = 15.0  # Reject unusually long samples

    def __init__(self):
        self._encoder       = None   # Lazy SpeechBrain encoder
        self._profile_vec   = None   # Enrolled embedding (numpy array)
        self._loaded        = False
        self._enrolled      = False

    # ── Internal helpers ──────────────────────────────────────────

    def _lazy_load(self):
        if self._loaded:
            return
        logger.info("Loading SpeechBrain ECAPA-TDNN encoder (CPU)...")
        try:
            # Ensure patches are applied again (belt-and-suspenders)
            _apply_torchaudio_patches()

            from speechbrain.inference.speaker import SpeakerRecognition
            self._encoder = SpeakerRecognition.from_hparams(
                source="speechbrain/spkrec-ecapa-voxceleb",
                savedir="models/speechbrain_ecapa",
                run_opts={"device": "cpu"}
            )
            self._loaded = True
            logger.info("SpeechBrain ECAPA-TDNN loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to load SpeechBrain: {e}")
            raise

    def _audio_bytes_to_tensor(self, audio_bytes: bytes, sample_rate: int = 16000):
        """Convert raw PCM bytes to a torchaudio-compatible tensor."""
        import torch
        import numpy as np
        audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        return torch.tensor(audio_np).unsqueeze(0)   # [1, samples]

    def _duration(self, audio_bytes: bytes, sample_rate: int = 16000) -> float:
        import numpy as np
        samples = len(np.frombuffer(audio_bytes, dtype=np.int16))
        return samples / sample_rate

    def _anti_spoofing_check(self, audio_bytes: bytes, sample_rate: int = 16000) -> Optional[str]:
        """
        Lightweight CPU-efficient heuristic checks to resist replay/spoofing attacks.
        Returns a rejection reason string if blocked, or None if passed.
        """
        import numpy as np

        duration = self._duration(audio_bytes, sample_rate)
        if duration < self.MIN_DURATION_SECONDS:
            return f"Sample too short ({duration:.2f}s)"
        if duration > self.MAX_DURATION_SECONDS:
            return f"Sample suspiciously long ({duration:.2f}s)"

        audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        if len(audio_np) == 0:
            return "Failed anti-spoofing: empty audio sample"

        # 1. Frame-based energy analysis
        frame_size = int(0.02 * sample_rate)  # 20ms frames
        hop_size   = int(0.01 * sample_rate)  # 10ms hop
        frames = [audio_np[i:i+frame_size] for i in range(0, len(audio_np)-frame_size, hop_size)]

        if not frames:
            return "Failed anti-spoofing: audio frame parsing failed"

        rms_values = np.array([np.sqrt(np.mean(f**2)) for f in frames])

        # Filter for speech frames
        speech_rms = rms_values[rms_values > 0.01]
        if len(speech_rms) < 5:
            return "Failed anti-spoofing: insufficient speech content"

        energy_std = np.std(speech_rms)

        flatness_suspicion = 0.0
        if energy_std < 0.002:
            flatness_suspicion = 0.8
        elif energy_std > 0.45:
            flatness_suspicion = 0.4

        # 2. Replay Signature Correlation Detection
        n_samples = len(audio_np)
        max_lag = min(n_samples // 2, sample_rate)
        lags = [int(sample_rate * t) for t in [0.05, 0.1, 0.2, 0.3, 0.5]]
        lags = [l for l in lags if l < max_lag]

        repeat_suspicion = 0.0
        for lag in lags:
            if lag >= len(audio_np):
                continue
            corr = np.corrcoef(audio_np[:-lag], audio_np[lag:])[0, 1]
            if not np.isnan(corr) and corr > 0.97:
                repeat_suspicion = 0.9
                break

        # 3. High-Frequency Spectral Ratio check
        diffs = np.diff(audio_np)
        diff_energy = np.mean(diffs**2)
        signal_energy = np.mean(audio_np**2) + 1e-10
        hf_ratio = diff_energy / signal_energy

        hf_suspicion = 0.0
        if hf_ratio > 3.8:
            hf_suspicion = 0.7

        suspicion_score = max(flatness_suspicion, repeat_suspicion, hf_suspicion)

        if suspicion_score > 0.6:
            reasons = []
            if flatness_suspicion > 0.5:
                reasons.append("flat energy std")
            if repeat_suspicion > 0.5:
                reasons.append("repeated correlation signature")
            if hf_suspicion > 0.5:
                reasons.append("excessive high-frequency vocoder artifacts")
            reason_str = ", ".join(reasons)

            logger.warning(f"Anti-spoofing block: Suspicion score {suspicion_score:.2f} ({reason_str})")
            audit.log_event("SPOOF_DETECTION_TRIGGERED", {
                "suspicion_score": round(suspicion_score, 2),
                "reasons": reasons
            })
            return f"Suspicious spoofing signature: {reason_str}"

        return None

    def _embed(self, audio_bytes: bytes) -> "np.ndarray":
        """Extract speaker embedding from audio bytes."""
        import numpy as np
        waveform = self._audio_bytes_to_tensor(audio_bytes)
        embedding = self._encoder.encode_batch(waveform)
        return embedding.squeeze().detach().cpu().numpy()

    @staticmethod
    def _cosine_similarity(a, b) -> float:
        import numpy as np
        return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))

    # ── Enrollment ────────────────────────────────────────────────

    async def enroll(self, audio_samples: list[bytes], sample_rate: int = 16000) -> bool:
        """
        Enroll the owner using 3–5 audio samples.
        Generates an averaged embedding and stores it encrypted in the Vault.

        Returns True on success.
        """
        if len(audio_samples) < 3:
            logger.error("Enrollment requires at least 3 audio samples.")
            return False

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self._lazy_load)
        except Exception as e:
            logger.error(f"SpeechBrain failed to load during enrollment: {e}")
            return False

        logger.info(f"Processing {len(audio_samples)} enrollment samples...")
        embeddings = []
        for i, sample in enumerate(audio_samples):
            issue = self._anti_spoofing_check(sample, sample_rate)
            if issue:
                logger.warning(f"Sample {i + 1} rejected during enrollment: {issue}")
                continue
            try:
                emb = await loop.run_in_executor(None, self._embed, sample)
                embeddings.append(emb)
            except Exception as e:
                logger.warning(f"Sample {i + 1} embedding failed: {e}")
                continue

        if len(embeddings) < 3:
            logger.error(f"Not enough valid samples after anti-spoofing filter ({len(embeddings)}/3 passed).")
            return False

        import numpy as np
        # Average all embeddings to build a robust profile
        profile = np.mean(embeddings, axis=0)
        profile = profile / (np.linalg.norm(profile) + 1e-10)   # L2 normalize

        # Encrypt and store in Vault (safely clearing old profile first)
        try:
            vault.delete_secret(self.VAULT_KEY)
        except Exception:
            pass

        profile_b64 = base64.b64encode(profile.tobytes()).decode("ascii")
        vault.set_secret(self.VAULT_KEY, json.dumps({
            "embedding_b64": profile_b64,
            "dtype": str(profile.dtype),
            "shape": list(profile.shape),
            "enrolled_at": time.time(),
            "samples_used": len(embeddings)
        }))

        self._profile_vec = profile
        self._enrolled = True
        audit.log_event("VOICE_ENROLLMENT_SUCCESS", {"samples": len(embeddings)})
        logger.info("Voice profile enrolled and stored in Secure Vault.")
        return True

    def load_profile(self) -> bool:
        """Load the encrypted voice profile from the Vault into memory."""
        try:
            raw = vault.get_secret(self.VAULT_KEY)
            if not raw:
                logger.info("No voice profile found in Vault.")
                return False
            data = json.loads(raw)
            import numpy as np
            dtype = np.dtype(data["dtype"])
            buf = base64.b64decode(data["embedding_b64"])
            self._profile_vec = np.frombuffer(buf, dtype=dtype).reshape(data["shape"])
            self._enrolled = True
            logger.info(f"Voice profile loaded from Vault (enrolled {int(time.time() - data['enrolled_at'])}s ago).")
            return True
        except Exception as e:
            logger.error(f"Failed to load voice profile: {e}")
            return False

    # ── Authentication ────────────────────────────────────────────

    async def authenticate(self, audio_bytes: bytes, sample_rate: int = 16000) -> AuthDecision:
        """
        Authenticate a voice sample against the enrolled profile.
        """
        # Anti-spoofing check first (no ML needed)
        issue = self._anti_spoofing_check(audio_bytes, sample_rate)
        if issue:
            audit.log_event("VOICE_AUTH_BLOCKED", {"reason": issue})
            return AuthDecision(ConfidenceLevel.BLOCKED, 0.0, f"Anti-spoofing: {issue}")

        # Load model lazily (in thread executor to avoid blocking event loop)
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._lazy_load)
        except Exception as e:
            return AuthDecision(ConfidenceLevel.BLOCKED, 0.0, f"Model unavailable: {e}")

        # Load profile if not already in memory
        if not self._enrolled:
            loaded = self.load_profile()
            if not loaded:
                return AuthDecision(ConfidenceLevel.BLOCKED, 0.0, "No voice profile enrolled.")

        # Compute similarity in thread pool to avoid blocking event loop
        loop = asyncio.get_running_loop()
        live_emb = await loop.run_in_executor(None, self._embed, audio_bytes)
        score = self._cosine_similarity(live_emb, self._profile_vec)

        # Map score to confidence level
        if score >= self.VERIFIED_THRESHOLD:
            level = ConfidenceLevel.VERIFIED
        elif score >= self.LOW_CONF_THRESHOLD:
            level = ConfidenceLevel.LOW_CONFIDENCE
        elif score >= self.UNKNOWN_THRESHOLD:
            level = ConfidenceLevel.UNKNOWN
        else:
            level = ConfidenceLevel.BLOCKED

        reason = f"Cosine similarity: {score:.3f}"
        audit.log_event("VOICE_AUTH_ATTEMPT", {
            "confidence": level.value,
            "score": round(score, 4)
        })
        logger.info(f"Voice Auth → {level.value} (score={score:.3f})")
        return AuthDecision(level, score, reason)


# ── Global singleton ──────────────────────────────────────────────
authenticator = VoiceAuthenticator()
