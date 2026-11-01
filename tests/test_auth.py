"""
tests/test_auth.py

Comprehensive Test Suite for PRIMUS Voice Authentication and Fallback Security (Phase 3.5).
Covers:
- Argon2id password verification & enrollment
- Retry limits and temporary lockout rules
- 15-minute idle session auto-expiry
- Secure Vault re-locking and memory wiping
- CPU-friendly anti-spoofing heuristics (energy variance, correlation lag, spectral ratio)
- Voice profile enrollment and re-enrollment lifecycle
- Confidence scores and password fallback triggers
- Microphone calibration validation (silent, noisy, normal signals)
- Degraded mode operation when voice hardware fails
- Python/QML UI bridge signals and slots flow
"""
import sys
import os
import time
import asyncio
import unittest
from unittest.mock import MagicMock, patch
import numpy as np

# Set up Qt Application context for QObject testing
from PyQt6.QtCore import QCoreApplication
app = QCoreApplication.instance() or QCoreApplication(sys.argv)

from config.settings import settings, BASE_DIR
from security.auth_manager import auth_manager, AuthState
from security.vault import vault
from voice.auth import authenticator, ConfidenceLevel, AuthDecision
from ui.engine import ui_engine, UIBridge
from core.orchestrator import PrimusOrchestrator


class TestPrimusAuthSecurity(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        # 1. Back up settings
        self.orig_vault_path = settings.vault_path
        self.orig_password_hash = settings.master_password_hash
        self.orig_session_sec = settings.session_duration_sec
        self.orig_lockout_sec = settings.auth_lockout_sec

        # 2. Configure temporary paths for isolation
        self.test_vault_path = BASE_DIR / "security" / "vault_test.enc"
        settings.vault_path = self.test_vault_path
        settings.master_password_hash = ""
        settings.session_duration_sec = 2  # Short duration for expiry testing
        settings.auth_lockout_sec = 3      # Short lockout duration for testing

        if self.test_vault_path.exists():
            os.remove(self.test_vault_path)

        # Reset managers to clean unverified state
        auth_manager.current_state = AuthState.UNVERIFIED
        auth_manager.failed_attempts = 0
        auth_manager.lockout_until = 0.0
        auth_manager.authenticated_at = 0.0
        auth_manager.last_activity = 0.0

        # Mock env update to prevent changing local config file
        auth_manager._update_env_file = MagicMock()

        vault.clear_cached_keys()
        authenticator._enrolled = False
        authenticator._profile_vec = None
        
        # Backup global loop and callbacks
        self.orig_bridge_loop = ui_engine.bridge._loop
        self.orig_bridge_callback = ui_engine.bridge._pending_password_fallback

    async def asyncTearDown(self):
        # Clean up temporary test files
        vault.lock()
        if self.test_vault_path.exists():
            try:
                os.remove(self.test_vault_path)
            except OSError:
                pass

        # Restore settings
        settings.vault_path = self.orig_vault_path
        settings.master_password_hash = self.orig_password_hash
        settings.session_duration_sec = self.orig_session_sec
        settings.auth_lockout_sec = self.orig_lockout_sec

        # Restore bridge loop
        ui_engine.bridge._loop = self.orig_bridge_loop
        ui_engine.bridge._pending_password_fallback = self.orig_bridge_callback

    # ── 1. PASSWORD SECURITY & ARGON2ID VERIFICATION ──────────────────────────

    def test_argon2_password_enrollment_and_verification(self):
        """Verify password hashing, minimum length checks, and verification flow."""
        # Minimum character validation
        self.assertFalse(auth_manager.enroll_password("short"))
        self.assertFalse(auth_manager.is_enrolled)

        # Successful enrollment
        password = "secure_test_password"
        self.assertTrue(auth_manager.enroll_password(password))
        self.assertTrue(auth_manager.is_enrolled)
        self.assertNotEqual(settings.master_password_hash, "")
        self.assertNotEqual(settings.master_password_hash, password)

        # Incorrect password verification
        self.assertFalse(auth_manager.verify_password("wrong_password"))
        self.assertEqual(auth_manager.current_state, AuthState.UNVERIFIED)

        # Correct password verification
        self.assertTrue(auth_manager.verify_password(password))
        self.assertEqual(auth_manager.current_state, AuthState.VERIFIED)
        self.assertGreater(auth_manager.authenticated_at, 0)
        self.assertGreater(auth_manager.last_activity, 0)

    # ── 2. RETRY LIMITS & AUTHENTICATION LOCKOUTS ──────────────────────────────

    def test_auth_lockout_threshold_and_cooldown(self):
        """Verify that 5 failed attempts trigger a 5-minute lockout."""
        password = "secure_test_password"
        self.assertTrue(auth_manager.enroll_password(password))

        # Perform 4 failed verification attempts
        for i in range(4):
            self.assertFalse(auth_manager.verify_password("incorrect_pass"))
            self.assertEqual(auth_manager.failed_attempts, i + 1)
            self.assertEqual(auth_manager.current_state, AuthState.UNVERIFIED)
            self.assertFalse(auth_manager.is_locked_out())

        # 5th failed attempt -> lock out
        self.assertFalse(auth_manager.verify_password("incorrect_pass"))
        self.assertEqual(auth_manager.current_state, AuthState.BLOCKED)
        self.assertTrue(auth_manager.is_locked_out())
        self.assertGreater(auth_manager.get_lockout_remaining(), 0)

        # Correct password must still be rejected while locked out
        self.assertFalse(auth_manager.verify_password(password))

        # Fast forward time to expire lockout
        auth_manager.lockout_until = time.time() - 1
        self.assertFalse(auth_manager.is_locked_out())
        self.assertEqual(auth_manager.get_lockout_remaining(), 0)

        # Correct password should now unlock session
        self.assertTrue(auth_manager.verify_password(password))
        self.assertEqual(auth_manager.current_state, AuthState.VERIFIED)

    # ── 3. SESSION EXPIRY & SECURE VAULT RE-LOCKING ────────────────────────────

    async def test_session_expiry_and_vault_relocking(self):
        """Verify that 15 minutes of inactivity clears keys and locks vault."""
        password = "secure_test_password"
        self.assertTrue(auth_manager.enroll_password(password))
        self.assertTrue(auth_manager.verify_password(password))
        
        # Vault is unlocked, verify writing and reading secrets
        vault.set_secret("test_api_key", "secret_value_123")
        self.assertEqual(vault.get_secret("test_api_key"), "secret_value_123")
        self.assertIsNotNone(vault._key)

        # Check session expiry within timeout
        self.assertFalse(auth_manager.check_session_expiry())
        self.assertEqual(auth_manager.current_state, AuthState.VERIFIED)

        # Simulate idle timeout (session_duration_sec configured as 2)
        auth_manager.last_activity = time.time() - 5
        self.assertTrue(auth_manager.check_session_expiry())
        self.assertEqual(auth_manager.current_state, AuthState.UNVERIFIED)

        # Verify vault key and memory secrets are scrubbed from RAM
        self.assertIsNone(vault._key)
        self.assertEqual(vault._data, {})

        # Accessing secrets should now raise PermissionError
        with self.assertRaises(PermissionError):
            vault.get_secret("test_api_key")
        with self.assertRaises(PermissionError):
            vault.set_secret("another", "val")

    # ── 4. VOICE ANTI-SPOOFING HEURISTIC ENGINE ────────────────────────────────

    def _generate_normal_voice_audio(self) -> bytes:
        # Modulated white noise to simulate varying voice energy without periodicity
        t = np.linspace(0, 3.0, 3 * 16000)
        amplitude = 0.5 + 0.4 * np.sin(2 * np.pi * 1.0 * t)
        wave = amplitude * np.random.uniform(-1.0, 1.0, len(t))
        return (wave * 32767).astype(np.int16).tobytes()

    def _generate_flat_voice_audio(self) -> bytes:
        # DC offset flat wave
        wave = np.ones(3 * 16000, dtype=np.float32) * 0.5
        return (wave * 32767).astype(np.int16).tobytes()

    def _generate_repeated_voice_audio(self) -> bytes:
        # 0.1-second block repeated exactly
        t_block = np.linspace(0, 0.1, int(0.1 * 16000))
        block = 0.5 * np.sin(2 * np.pi * 220.0 * t_block)
        wave = np.tile(block, 30)
        return (wave * 32767).astype(np.int16).tobytes()

    def _generate_high_freq_voice_audio(self) -> bytes:
        # High-frequency vocoder artifacts
        t = np.linspace(0, 3.0, 3 * 16000)
        wave = 0.5 * np.sin(2 * np.pi * 7500.0 * t)
        return (wave * 32767).astype(np.int16).tobytes()

    def test_voice_anti_spoofing_heuristics(self):
        """Test anti-spoofing engine detection on duration, flat energy, loops, and spectral ratio."""
        # 1. Normal voice sample (should pass)
        normal = self._generate_normal_voice_audio()
        self.assertIsNone(authenticator._anti_spoofing_check(normal))

        # 2. Too short (should reject)
        short_wave = (np.sin(2 * np.pi * 220.0 * np.linspace(0, 0.5, 8000)) * 32767).astype(np.int16).tobytes()
        self.assertIn("too short", authenticator._anti_spoofing_check(short_wave).lower())

        # 3. Flat energy variance (should reject)
        flat = self._generate_flat_voice_audio()
        self.assertIn("flat energy std", authenticator._anti_spoofing_check(flat).lower())

        # 4. Repeated correlation signature / loop (should reject)
        repeated = self._generate_repeated_voice_audio()
        self.assertIn("repeated correlation signature", authenticator._anti_spoofing_check(repeated).lower())

        # 5. Excessive high frequency / synthetic ratio (should reject)
        high_freq = self._generate_high_freq_voice_audio()
        self.assertIn("high-frequency vocoder", authenticator._anti_spoofing_check(high_freq).lower())

    # ── 5. VOICE ENROLLMENT & RE-ENROLLMENT LIFECYCLE ──────────────────────────

    async def test_voice_enrollment_and_re_enrollment(self):
        """Verify voice biometrics enrollment, averaging, vault encryption, and re-enrollment."""
        # Setup password
        self.assertTrue(auth_manager.enroll_password("secure_test_password"))

        # Generate 3 valid enrollment samples
        samples = [self._generate_normal_voice_audio() for _ in range(3)]

        # Mock lazy load & SpeechBrain encoder to avoid loading actual heavy models
        authenticator._loaded = True
        dummy_vector = np.random.randn(192).astype(np.float32)
        dummy_vector /= np.linalg.norm(dummy_vector)
        authenticator._embed = MagicMock(return_value=dummy_vector)

        # Perform enrollment
        self.assertTrue(await authenticator.enroll(samples))
        self.assertTrue(authenticator._enrolled)
        self.assertIsNotNone(authenticator._profile_vec)

        # Check stored credentials in vault
        vault_data = vault.get_secret(authenticator.VAULT_KEY)
        self.assertIsNotNone(vault_data)

        # Test re-enrollment deletes previous profile
        new_dummy_vector = np.random.randn(192).astype(np.float32)
        new_dummy_vector /= np.linalg.norm(new_dummy_vector)
        authenticator._embed = MagicMock(return_value=new_dummy_vector)

        self.assertTrue(await authenticator.enroll(samples))
        self.assertTrue(np.allclose(authenticator._profile_vec, new_dummy_vector))

    # ── 6. CONFIDENCE MATCH RULES & PASSWORD FALLBACK ──────────────────────────

    async def test_auth_confidence_scores_and_fallback_prompt(self):
        """Test speaker similarity levels mapping to correct actions and triggering password fallback."""
        self.assertTrue(auth_manager.enroll_password("secure_test_password"))
        authenticator._loaded = True
        
        # Enrolled profile is a unit vector [1, 0, 0, ...]
        profile = np.zeros(192, dtype=np.float32)
        profile[0] = 1.0
        authenticator._profile_vec = profile
        authenticator._enrolled = True

        # Mock UI bridge loop to avoid thread-safe queue crashes
        loop = asyncio.get_running_loop()
        ui_engine.bridge._loop = loop

        # 1. VERIFIED similarity score (cosine >= 0.85)
        # Mock embed to return exact profile vector
        authenticator._embed = MagicMock(return_value=profile)
        decision = await authenticator.authenticate(self._generate_normal_voice_audio())
        self.assertEqual(decision.level, ConfidenceLevel.VERIFIED)
        self.assertTrue(decision.allows_high_risk())

        # 2. LOW_CONFIDENCE similarity score (0.70 <= cosine < 0.85)
        # Mock embed to return a vector with similarity ~ 0.75
        low_conf_vec = np.zeros(192, dtype=np.float32)
        low_conf_vec[0] = 0.75
        low_conf_vec[1] = np.sqrt(1 - 0.75**2)
        authenticator._embed = MagicMock(return_value=low_conf_vec)
        
        decision = await authenticator.authenticate(self._generate_normal_voice_audio())
        self.assertEqual(decision.level, ConfidenceLevel.LOW_CONFIDENCE)
        self.assertFalse(decision.allows_high_risk())
        self.assertTrue(decision.allows_low_risk())

        # Test fallback password prompt behavior during orchestrator command gating
        def mock_fallback_side_effect(*args, **kwargs):
            auth_manager.transition_to(AuthState.VERIFIED)
            return True

        with patch.object(ui_engine.bridge, 'request_password_fallback', side_effect=mock_fallback_side_effect) as mock_fallback:
            orchestrator = PrimusOrchestrator(ui_engine)
            orchestrator.async_loop = loop
            
            # Unverified execution triggers manual password fallback
            auth_manager.current_state = AuthState.UNVERIFIED
            res = await orchestrator.process_keyboard_command_task("echo hello")
            mock_fallback.assert_called_once()
            self.assertEqual(auth_manager.current_state, AuthState.VERIFIED)

    # ── 7. MICROPHONE CALIBRATION VALIDATION ───────────────────────────────────

    @patch('sounddevice.rec')
    @patch('sounddevice.wait')
    async def test_microphone_calibration_thresholds(self, mock_wait, mock_rec):
        """Test calibrator behavior for silent, noisy, and normal environments."""
        # Initialize loop reference
        loop = asyncio.get_running_loop()
        ui_engine.bridge._loop = loop

        # Monitor step updates
        emitted_args = []
        def on_step_update(step, total, phrase, statusMsg):
            emitted_args.append((step, total, phrase, statusMsg))
        ui_engine.bridge.enrollmentStepUpdate.connect(on_step_update)

        try:
            # 1. Silent environment check (peak < 10)
            silent_signal = np.zeros((32000, 1), dtype=np.int16)
            mock_rec.return_value = silent_signal
            
            emitted_args.clear()
            await ui_engine.bridge._run_calibration_step()
            # Assert last emit contains calibration failed message due to silent signal
            self.assertTrue(len(emitted_args) > 0)
            last_args = emitted_args[-1]
            self.assertIn("Microphone signal is silent", last_args[2])

            # 2. Noisy environment check (RMS > 800)
            noisy_signal = (np.random.randn(32000, 1) * 1200).astype(np.int16)
            mock_rec.return_value = noisy_signal

            emitted_args.clear()
            await ui_engine.bridge._run_calibration_step()
            self.assertTrue(len(emitted_args) > 0)
            last_args = emitted_args[-1]
            self.assertIn("Room noise is too loud", last_args[2])

            # 3. Valid normal environment (Peak > 10, RMS < 800)
            normal_signal = (np.random.randn(32000, 1) * 300).astype(np.int16)
            # Ensure there is some amplitude
            normal_signal[0, 0] = 500
            mock_rec.return_value = normal_signal

            emitted_args.clear()
            await ui_engine.bridge._run_calibration_step()
            self.assertTrue(len(emitted_args) > 0)
            last_args = emitted_args[-1]
            self.assertIn("Calibration passed", last_args[3])
            # Check step is updated to 2 (record step)
            self.assertEqual(last_args[0], 2)
        finally:
            ui_engine.bridge.enrollmentStepUpdate.disconnect(on_step_update)

    # ── 8. DEGRADED MODE USABILITY ─────────────────────────────────────────────

    @patch('sounddevice.InputStream')
    async def test_degraded_mode_on_mic_failure(self, mock_stream):
        """Ensure system falls back to text-only mode and doesn't crash when mic fails."""
        # Setup InputStream to throw exception (simulating missing mic/audio drivers)
        mock_stream.side_effect = Exception("No audio devices found")

        orchestrator = PrimusOrchestrator(ui_engine)
        loop = asyncio.get_running_loop()
        orchestrator.async_loop = loop

        # Start continuous listening loop and check it catches the error and exits gracefully
        with patch.object(ui_engine.bridge, 'push_status') as mock_push:
            await orchestrator.continuous_listening_loop()
            # Assert degraded status is pushed to HUD
            mock_push.assert_any_call("SYSTEM: ACTIVE [MIC UNAVAILABLE]")

        # Text mode command execution remains functional
        auth_manager.current_state = AuthState.VERIFIED
        with patch('sandbox.executor.executor.execute', return_value={"stdout": "success", "blocked": False}) as mock_exec:
            await orchestrator.process_keyboard_command_task("echo hello")
            mock_exec.assert_called_once_with("echo hello", "KeyboardAgent")

    # ── 9. PYTHON/QML UI BRIDGE FLOWS ─────────────────────────────────────────

    async def test_ui_bridge_signals_and_slots(self):
        """Verify slots and signals flow successfully for passwords and wizards."""
        bridge = UIBridge()
        loop = asyncio.get_running_loop()
        bridge.set_loop(loop)

        # 1. Test Password fallback cancel flow
        bridge._pending_password_fallback = loop.create_future()
        
        hide_emitted = False
        def on_hide():
            nonlocal hide_emitted
            hide_emitted = True
        bridge.hidePasswordPrompt.connect(on_hide)

        try:
            bridge.onPasswordCancel()
            result = await bridge._pending_password_fallback
            self.assertFalse(result)
            self.assertTrue(hide_emitted)
        finally:
            bridge.hidePasswordPrompt.disconnect(on_hide)

        # 2. Test Password fallback success flow
        bridge._pending_password_fallback = loop.create_future()
        
        # Enforcing mock settings hash so enroll succeeds
        auth_manager._update_env_file = MagicMock()
        auth_manager.enroll_password("secure_test_password")

        with patch.object(auth_manager, 'verify_password', return_value=True):
            bridge.onPasswordSubmit("secure_test_password")
            result = await bridge._pending_password_fallback
            self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
