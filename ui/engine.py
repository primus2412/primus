"""
ui/engine.py

PyQt6 UI Engine + Python-QML Bridge for PRIMUS.

Responsibilities:
  - Launch the QML overlay window
  - Expose pyBridge to QML for bidirectional communication
  - Handle confirmation prompts (Phase 2)
  - Handle voice enrollment wizard (Phase 3)
  - Push telemetry and status updates to HUD

Threading model:
  - PyQt / QML MUST run on the main thread
  - All asyncio work runs on a background thread via asyncio.run_coroutine_threadsafe
"""
from __future__ import annotations

import sys
import asyncio
from pathlib import Path
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtQml import QQmlApplicationEngine
from PyQt6.QtCore import QObject, pyqtSlot, pyqtSignal, QTimer
from loguru import logger
from config.settings import settings
from audit.trail import audit


class UIBridge(QObject):
    """
    Central bridge between Python and QML.
    Signals flow Python → QML.
    Slots  flow QML   → Python.
    """

    # ── Confirmation prompt signals ───────────────────────────────
    showPrompt  = pyqtSignal(str, str, str, str, str)  # risk, agent, action, target, command
    hidePrompt  = pyqtSignal()
    overlayVisibilityChanged = pyqtSignal(bool)        # Toggle overlay visibility

    # ── Enrollment wizard signals ─────────────────────────────────
    showEnrollment        = pyqtSignal()
    hideEnrollment        = pyqtSignal()
    enrollmentStepUpdate  = pyqtSignal(int, int, str, str)  # step, total, phrase, statusMsg
    enrollmentRecording   = pyqtSignal(bool)                 # isRecording
    enrollmentComplete    = pyqtSignal(bool)                 # success

    # ── Password fallback signals ──────────────────────────────────
    showPasswordPrompt    = pyqtSignal(str)                  # errorMsg
    hidePasswordPrompt    = pyqtSignal()
    authStateChanged      = pyqtSignal(str)                  # UNVERIFIED, VERIFIED, etc.
    confidenceUpdated     = pyqtSignal(float)                # score
    lockoutStarted        = pyqtSignal(int)                  # remaining seconds
    lockoutEnded          = pyqtSignal()

    # ── HUD update signals ────────────────────────────────────────
    telemetryUpdate       = pyqtSignal(str, str, str)        # cpu, ram, gpu
    statusUpdate          = pyqtSignal(str)
    ocrUnavailableNotice  = pyqtSignal()

    # ── Enrollment state ──────────────────────────────────────────
    ENROLLMENT_PHRASES = [
        "PRIMUS, initialize all systems.",
        "Authenticate owner and begin operations.",
        "Activate tactical overlay and report status.",
        "Secure the workspace and run diagnostics.",
        "I am the owner. Authorization confirmed.",
    ]

    def __init__(self):
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending_confirmation: asyncio.Future | None = None
        self._pending_password_fallback: asyncio.Future | None = None
        self._enrollment_password: str = ""

        # Asyncio locks — must be created after event loop is set
        # We create them lazily but store references to avoid race conditions
        self._confirm_lock: asyncio.Lock | None = None
        self._pass_lock: asyncio.Lock | None = None

        # Enrollment state
        self._enrollment_step: int = 0
        self._enrollment_samples: list[bytes] = []
        self._enrollment_callback = None  # asyncio.Future
        self.text_command_callback = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    # ──────────────────────────────────────────────────────────────
    # Confirmation prompt (Phase 2)
    # ──────────────────────────────────────────────────────────────

    async def request_confirmation(self, risk: str, agent: str, action: str,
                                   target: str, command: str) -> bool:
        logger.warning(f"Requesting UI confirmation — {risk} risk from {agent}")
        if self._confirm_lock is None:
            self._confirm_lock = asyncio.Lock()
        async with self._confirm_lock:
            self._pending_confirmation = asyncio.Future()
            self.showPrompt.emit(risk, agent, action, target, command)
            return await self._pending_confirmation

    @pyqtSlot(bool)
    def onPromptResponse(self, approved: bool):
        logger.info(f"Confirmation: {'APPROVED' if approved else 'DENIED'}")
        audit.log_event("CONFIRMATION_RESPONSE", {"approved": approved})
        self.hidePrompt.emit()
        if self._pending_confirmation and not self._pending_confirmation.done():
            if self._loop:
                self._loop.call_soon_threadsafe(self._pending_confirmation.set_result, approved)

    # ──────────────────────────────────────────────────────────────
    # Password Fallback (Section 14)
    # ──────────────────────────────────────────────────────────────

    async def request_password_fallback(self, reason: str = "Verification required.") -> bool:
        """Prompts the QML password overlay and awaits user input."""
        logger.warning(f"Requesting manual password fallback: {reason}")
        if self._pass_lock is None:
            self._pass_lock = asyncio.Lock()

        async with self._pass_lock:
            from security.auth_manager import auth_manager
            if auth_manager.is_locked_out():
                logger.error("Authentication locked out. Cannot request password.")
                return False
                
            self._pending_password_fallback = asyncio.Future()
            self.showPasswordPrompt.emit(reason)
            result = await self._pending_password_fallback
            self.hidePasswordPrompt.emit()
            return result

    @pyqtSlot(str)
    def onPasswordSubmit(self, password: str):
        from security.auth_manager import auth_manager
        
        # Verify password (unlocks vault automatically on success)
        success = auth_manager.verify_password(password)
        if success:
            logger.info("Password verification successful.")
            if self._pending_password_fallback and not self._pending_password_fallback.done():
                if self._loop:
                    self._loop.call_soon_threadsafe(self._pending_password_fallback.set_result, True)
        else:
            if auth_manager.is_locked_out():
                lockout_sec = auth_manager.get_lockout_remaining()
                logger.critical(f"System locked out for {lockout_sec}s.")
                self.lockoutStarted.emit(lockout_sec)
                self.showPasswordPrompt.emit(f"TOO MANY ATTEMPTS: Locked out for {lockout_sec}s.")
            else:
                retries = max(0, 5 - auth_manager.failed_attempts)
                self.showPasswordPrompt.emit(
                    f"Invalid master password. {retries} retries remaining."
                )

    @pyqtSlot()
    def onPasswordCancel(self):
        logger.info("Password fallback cancelled by user.")
        self.hidePasswordPrompt.emit()
        if self._pending_password_fallback and not self._pending_password_fallback.done():
            if self._loop:
                self._loop.call_soon_threadsafe(self._pending_password_fallback.set_result, False)

    @pyqtSlot()
    def onFallbackRequested(self):
        """Called by QML when user manually clicks Lock/Unlock to authenticate."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(self.request_password_fallback("Manual verification requested."), self._loop)

    @pyqtSlot(str)
    def onTextCommandSubmit(self, command: str):
        """Receives keyboard command from degraded console prompt."""
        logger.info(f"Console command entered: {command}")
        if self.text_command_callback is not None and self._loop is not None:
            asyncio.run_coroutine_threadsafe(
                self._dispatch_text_command(command), self._loop
            )

    async def _dispatch_text_command(self, command: str):
        """Wrapper to call text_command_callback safely on the asyncio thread."""
        if self.text_command_callback:
            self.text_command_callback(command)

    # ──────────────────────────────────────────────────────────────
    # Voice Enrollment Wizard (Phase 3)
    # ──────────────────────────────────────────────────────────────

    async def run_enrollment(self) -> bool:
        """
        Drives the QML enrollment wizard from Python.
        Returns True when enrollment succeeds.
        """
        self._enrollment_step = 0
        self._enrollment_samples = []
        self._enrollment_password = ""
        self._enrollment_callback = asyncio.Future()

        self.showEnrollment.emit()
        result = await self._enrollment_callback
        self.hideEnrollment.emit()
        return result

    @pyqtSlot(str)
    def onEnrollmentBegin(self, password: str):
        """QML calls this when user enters password and clicks 'Create Account'."""
        if len(password) < 8:
            self.enrollmentStepUpdate.emit(0, 2, "", "Password must be at least 8 characters.")
            return

        self._enrollment_password = password
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._complete_password_setup(), self._loop)

    async def _complete_password_setup(self):
        """Password-only first-time setup (no voice biometrics)."""
        from security.auth_manager import auth_manager
        success = auth_manager.enroll_password(self._enrollment_password)
        self.enrollmentComplete.emit(success)
        if self._enrollment_callback and not self._enrollment_callback.done():
            self._loop.call_soon_threadsafe(self._enrollment_callback.set_result, success)

    async def _run_calibration_step(self):
        """Measures ambient noise level and validates microphone is active."""
        import sounddevice as sd
        import numpy as np

        SAMPLE_RATE = 16000
        DURATION = 2.0

        self.enrollmentRecording.emit(True)
        self.enrollmentStepUpdate.emit(1, 7, "CALIBRATION: Please stay silent to calibrate microphone...", "Calibrating ambient noise...")

        loop = asyncio.get_running_loop()
        try:
            audio = await loop.run_in_executor(
                None,
                lambda: sd.rec(int(DURATION * SAMPLE_RATE), samplerate=SAMPLE_RATE, channels=1, dtype='int16')
            )
            await loop.run_in_executor(None, sd.wait)
            self.enrollmentRecording.emit(False)

            audio_np = audio.flatten()
            peak = np.max(np.abs(audio_np))
            rms = np.sqrt(np.mean(audio_np.astype(np.float64)**2))
            logger.info(f"Calibration metrics: peak={peak}, rms={rms:.2f}")

            if peak < 10:
                self.enrollmentStepUpdate.emit(1, 7, "CALIBRATION FAILED: Microphone signal is silent.", "Please check microphone connections and retry.")
                return

            if rms > 800:
                self.enrollmentStepUpdate.emit(1, 7, f"CALIBRATION FAILED: Room noise is too loud ({rms:.0f} RMS > 800).", "Please move to a quiet room and retry.")
                return

            # Calibration passed!
            self._enrollment_step = 2
            phrase = self.ENROLLMENT_PHRASES[0]
            self.enrollmentStepUpdate.emit(
                2, 7, phrase,
                f"Calibration passed ({rms:.0f} RMS). Click Record to read Phrase 1."
            )
        except Exception as e:
            self.enrollmentRecording.emit(False)
            logger.error(f"Calibration failed: {e}")
            self.enrollmentStepUpdate.emit(1, 7, f"CALIBRATION FAILED: {str(e)}", "Please check hardware and retry.")

    @pyqtSlot()
    def onEnrollmentRetry(self):
        """Triggered from QML to retry mic calibration."""
        self._enrollment_step = 1
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._run_calibration_step(), self._loop)

    @pyqtSlot()
    def onEnrollmentRecord(self):
        """QML calls this when user clicks 'Record Sample'."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._record_sample(), self._loop)

    async def _record_sample(self):
        """Records one 3-second audio sample from the microphone."""
        import sounddevice as sd
        import numpy as np

        SAMPLE_RATE = 16000
        DURATION    = 3

        step = self._enrollment_step
        # Phrase index matches step - 2 since step 1 is calibration
        phrase = self.ENROLLMENT_PHRASES[step - 2]

        self.enrollmentRecording.emit(True)
        self.enrollmentStepUpdate.emit(step, 7, phrase, "Recording…")

        loop = asyncio.get_running_loop()
        audio = await loop.run_in_executor(
            None,
            lambda: sd.rec(int(DURATION * SAMPLE_RATE), samplerate=SAMPLE_RATE,
                           channels=1, dtype='int16')
        )
        await loop.run_in_executor(None, sd.wait)

        self.enrollmentRecording.emit(False)

        # Convert to bytes for the auth module
        audio_bytes = audio.tobytes()
        self._enrollment_samples.append(audio_bytes)

        if step < 6:
            # Move to next phrase sample
            self._enrollment_step += 1
            next_phrase = self.ENROLLMENT_PHRASES[self._enrollment_step - 2]
            self.enrollmentStepUpdate.emit(
                self._enrollment_step, 7,
                next_phrase, f"Sample {step - 1} captured. Click Record for next."
            )
        else:
            # All samples collected — save password and enroll voice profile
            self.enrollmentStepUpdate.emit(
                step, 7, phrase, "Processing secure voice profile…"
            )
            
            # 1. Setup/Enroll password
            from security.auth_manager import auth_manager
            pwd_success = auth_manager.enroll_password(self._enrollment_password)
            if not pwd_success:
                logger.error("Enrollment failed: password setup error.")
                self.enrollmentComplete.emit(False)
                if self._enrollment_callback and not self._enrollment_callback.done():
                    self._loop.call_soon_threadsafe(self._enrollment_callback.set_result, False)
                return

            # 2. Enroll Voice Profile
            from voice.auth import authenticator
            success = await authenticator.enroll(self._enrollment_samples, SAMPLE_RATE)
            self.enrollmentComplete.emit(success)
            if self._enrollment_callback and not self._enrollment_callback.done():
                self._loop.call_soon_threadsafe(self._enrollment_callback.set_result, success)

    @pyqtSlot()
    def onEnrollmentClose(self):
        """QML calls this when user clicks Close on the completion page."""
        self.hideEnrollment.emit()
        if self._enrollment_callback and not self._enrollment_callback.done():
            if self._loop:
                self._loop.call_soon_threadsafe(self._enrollment_callback.set_result, False)

    # ──────────────────────────────────────────────────────────────
    # HUD helpers (called from Orchestrator / workers)
    # ──────────────────────────────────────────────────────────────

    def push_telemetry(self, cpu: float, ram: float, gpu: float):
        self.telemetryUpdate.emit(f"{cpu:.0f}", f"{ram:.0f}", f"{gpu:.0f}")

    def push_status(self, msg: str):
        self.statusUpdate.emit(msg)

    def notify_ocr_unavailable(self):
        self.ocrUnavailableNotice.emit()


# ─────────────────────────────────────────────────────────────────────────────
# UI Engine (boots the Qt application on the main thread)
# ─────────────────────────────────────────────────────────────────────────────

class UIEngine:
    def __init__(self):
        self.app:    QGuiApplication | None  = None
        self.engine: QQmlApplicationEngine | None = None
        self.bridge: UIBridge = UIBridge()

    def start_ui(self):
        logger.info("Initializing UI Engine…")
        import os
        os.environ["QT_QUICK_CONTROLS_STYLE"] = "Basic"
        self.app = QGuiApplication(sys.argv)
        self.engine = QQmlApplicationEngine()

        # Expose bridge to QML as 'pyBridge'
        self.engine.rootContext().setContextProperty("pyBridge", self.bridge)
        
        # Log QML warnings/errors to diagnose load failures
        def handle_qml_warnings(warnings):
            for warning in warnings:
                logger.error(f"QML Warning: {warning.toString()}")
        self.engine.warnings.connect(handle_qml_warnings)

        qml_file = Path(__file__).resolve().parent.parent / "themes" / f"{settings.theme}.qml"
        if not qml_file.exists():
            logger.critical(f"Theme file not found: {qml_file}")
            sys.exit(1)

        self.engine.load(str(qml_file))
        if not self.engine.rootObjects():
            logger.critical("Failed to load QML root objects.")
            sys.exit(1)

        # Overlay starts visible; ensure QML accepts keyboard input immediately
        QTimer.singleShot(0, lambda: self.bridge.overlayVisibilityChanged.emit(True))

        logger.info("UI overlay active.")
        sys.exit(self.app.exec())


# Global singleton
ui_engine = UIEngine()
