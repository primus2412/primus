"""
security/auth_manager.py

Centralized Session and Authentication Manager for PRIMUS.
Controls verification states, Argon2id credentials, lockouts, and session expirations.
"""
import os
import time
from enum import Enum
from loguru import logger
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from config.settings import settings, BASE_DIR
from audit.trail import audit


class AuthState(str, Enum):
    UNVERIFIED        = "UNVERIFIED"
    VERIFYING         = "VERIFYING"
    VERIFIED          = "VERIFIED"
    LOW_CONFIDENCE    = "LOW_CONFIDENCE"
    FALLBACK_REQUIRED = "FALLBACK_REQUIRED"
    BLOCKED           = "BLOCKED"


class SessionAuthManager:
    """
    Tracks and gates user authorization.
    Verifies master password, tracks rate limits, and automatically clears 
    decrypted vault keys on idle session timeouts.
    """
    def __init__(self):
        self.current_state = AuthState.UNVERIFIED
        self.failed_attempts = 0
        self.lockout_until = 0.0
        self.authenticated_at = 0.0
        self.last_activity = 0.0
        self._hasher = PasswordHasher()
        self.on_state_changed = None # Callback to signal UI/Orchestrator

    @property
    def is_enrolled(self) -> bool:
        """Returns True if the owner has set up a master password."""
        return bool(settings.master_password_hash)

    def transition_to(self, state: AuthState):
        """Changes the authentication state and triggers optional callback."""
        if self.current_state != state:
            old_state = self.current_state
            self.current_state = state
            logger.info(f"Auth state transition: {old_state} -> {state}")
            audit.log_event("AUTH_STATE_CHANGED", {"old_state": old_state.value, "new_state": state.value})
            if self.on_state_changed:
                self.on_state_changed(state)

    def is_locked_out(self) -> bool:
        """Returns True if the authentication is currently rate-limited/locked."""
        if self.lockout_until > 0.0:
            if time.time() < self.lockout_until:
                return True
            else:
                # Lockout time has elapsed
                self.lockout_until = 0.0
                self.failed_attempts = 0
                logger.info("Authentication lockout ended.")
                audit.log_event("AUTH_LOCKOUT_ENDED", {})
        return False

    def get_lockout_remaining(self) -> int:
        """Returns remaining lockout time in seconds."""
        if self.is_locked_out():
            return max(0, int(self.lockout_until - time.time()))
        return 0

    def verify_password(self, password: str) -> bool:
        """
        Validates the master password using Argon2id.
        On success, unlocks the vault and establishes a session.
        """
        if self.is_locked_out():
            logger.warning("Authentication attempt blocked: System is locked out.")
            return False

        if not self.is_enrolled:
            logger.error("Authentication failed: No master password set.")
            return False

        try:
            # Verify password using Argon2
            self._hasher.verify(settings.master_password_hash, password)
        except VerifyMismatchError:
            self._register_failed_attempt()
            return False
        except Exception as e:
            logger.error(f"Error during password verification: {e}")
            self._register_failed_attempt()
            return False

        # Password verified — now unlock the vault
        try:
            from security.vault import vault
            vault.unlock(password)
        except Exception as e:
            # Master password hash matches but vault was created with a different password
            logger.warning(f"Vault unlock failed; re-keying vault to match master password: {e}")
            try:
                from security.vault import vault
                vault.rekey(password)
            except Exception as rekey_err:
                logger.error(f"Vault re-key failed: {rekey_err}")
                return False
            audit.log_event("VAULT_REKEYED_AFTER_AUTH", {})

        # Reset counters & establish verified session
        self.failed_attempts = 0
        self.authenticated_at = time.time()
        self.last_activity = time.time()
        self.transition_to(AuthState.VERIFIED)

        logger.info("Master password authenticated successfully.")
        audit.log_event("PASSWORD_AUTH_SUCCESS", {})
        return True

    def _register_failed_attempt(self):
        """Increments failed attempt counters and triggers lockout if threshold met."""
        self.failed_attempts += 1
        remaining_retries = max(0, 5 - self.failed_attempts)
        logger.warning(f"Failed password attempt. Retries remaining: {remaining_retries}")
        audit.log_event("PASSWORD_AUTH_FAILED", {"attempt": self.failed_attempts})

        if self.failed_attempts >= 5:
            lockout_duration = settings.auth_lockout_sec
            self.lockout_until = time.time() + lockout_duration
            self.transition_to(AuthState.BLOCKED)
            logger.critical(f"Too many failed attempts! Auth locked for {lockout_duration // 60} minutes.")
            audit.log_event("AUTH_LOCKOUT_STARTED", {"duration_sec": lockout_duration})

    def enroll_password(self, password: str) -> bool:
        """
        Hashes the password with Argon2id, updates settings, 
        and initializes a new Secure Vault.
        """
        if len(password) < 8:
            logger.error("Master password setup failed: Password must be at least 8 characters.")
            return False

        try:
            # Hash password
            hashed = self._hasher.hash(password)
            
            # Store in settings and write to .env
            settings.master_password_hash = hashed
            self._update_env_file("MASTER_PASSWORD_HASH", f'"{hashed}"')
            
            # Initialize/unlock vault
            from security.vault import vault
            if vault.vault_path.exists():
                try:
                    os.remove(vault.vault_path)
                    logger.info("Removed stale vault file to initialize fresh vault during password enrollment.")
                except Exception as ex:
                    logger.warning(f"Could not remove stale vault file: {ex}")
            vault.unlock(password)
            
            logger.info("Master password successfully enrolled.")
            audit.log_event("PASSWORD_ENROLLMENT_SUCCESS", {})
            return True
        except Exception as e:
            logger.error(f"Failed to enroll master password: {e}")
            return False

    def reset_session(self):
        """Manually invalidates the active session and locks the vault."""
        self.authenticated_at = 0.0
        self.last_activity = 0.0
        
        # Clear vault memory buffers
        from security.vault import vault
        vault.lock()
        
        self.transition_to(AuthState.UNVERIFIED)
        logger.info("Active session invalidated. Vault locked.")
        audit.log_event("SESSION_INVALIDATED", {})

    def update_activity(self):
        """Keeps session alive by updating activity timestamp."""
        if self.current_state == AuthState.VERIFIED:
            self.last_activity = time.time()

    def check_session_expiry(self) -> bool:
        """
        Checks if the verified session has expired due to idle inactivity.
        Returns True if session was expired and locked.
        """
        if self.current_state != AuthState.VERIFIED:
            return False

        idle_time = time.time() - self.last_activity
        if idle_time >= settings.session_duration_sec:
            logger.warning(f"Inactivity timeout reached ({idle_time:.0f}s >= {settings.session_duration_sec}s). Locking session.")
            audit.log_event("SESSION_EXPIRED", {"idle_sec": int(idle_time)})
            self.reset_session()
            return True
        return False

    @staticmethod
    def _update_env_file(key: str, value: str):
        """Atomically write a key=value pair to the .env file."""
        env_path = BASE_DIR / ".env"
        lines = []
        found = False
        if env_path.exists():
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip().startswith(f"{key}="):
                        lines.append(f"{key}={value}\n")
                        found = True
                    else:
                        lines.append(line)
        if not found:
            lines.append(f"{key}={value}\n")

        # Atomic write via temp file + rename
        tmp_path = env_path.with_suffix(".env.tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            os.replace(str(tmp_path), str(env_path))
        except Exception as e:
            logger.error(f"Failed to update .env file: {e}")
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


# Global Auth Manager instance
auth_manager = SessionAuthManager()
