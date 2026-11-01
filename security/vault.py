"""
security/vault.py

AES-128-CBC (Fernet) encrypted vault for storing API keys, credentials, and embeddings.
Uses PBKDF2HMAC-SHA256 with a per-installation random salt (prepended to vault file).
Thread-safe via a RLock.
"""
import os
import json
import base64
import shutil
import time
import threading
from pathlib import Path
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from config.settings import settings
from loguru import logger
from audit.trail import audit

# PBKDF2 configuration
_PBKDF2_ITERATIONS = 480_000
_SALT_SIZE = 32  # bytes — prepended to vault file


class SecureVault:
    """
    Fernet-encrypted vault for storing secrets and embeddings.
    Key derived via PBKDF2HMAC-SHA256 from the master password + a per-vault random salt.
    Salt is prepended (unencrypted) to the vault file; only the data payload is encrypted.
    Thread-safe — all operations protected by a reentrant lock.
    """

    def __init__(self):
        self.vault_path = settings.vault_path
        self._key: bytes | None = None
        self._data: dict = {}
        self._lock = threading.RLock()

    # ── Key Derivation ─────────────────────────────────────────────

    def _derive_key(self, password: str, salt: bytes) -> bytes:
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=_PBKDF2_ITERATIONS,
        )
        return base64.urlsafe_b64encode(kdf.derive(password.encode("utf-8")))

    # ── Persistence ────────────────────────────────────────────────

    def _load(self, password: str):
        """Load and decrypt vault from disk. Reads salt from file header."""
        with open(self.vault_path, "rb") as f:
            raw = f.read()

        if len(raw) < _SALT_SIZE + 1:
            raise ValueError("Vault file is corrupt or too small.")

        salt = raw[:_SALT_SIZE]
        ciphertext = raw[_SALT_SIZE:]

        self._key = self._derive_key(password, salt)
        fernet = Fernet(self._key)
        try:
            decrypted = fernet.decrypt(ciphertext)
        except Exception:
            self._key = None
            raise ValueError("Invalid vault password or corrupted vault data.")

        self._data = json.loads(decrypted.decode("utf-8"))

    def _save(self):
        """Encrypt and write vault to disk. Reads existing salt from file header."""
        if not self._key:
            raise PermissionError("Vault is locked")

        with open(self.vault_path, "rb") as f:
            raw = f.read()

        if len(raw) < _SALT_SIZE:
            raise ValueError("Cannot save: vault file has no salt header.")

        salt = raw[:_SALT_SIZE]
        fernet = Fernet(self._key)
        ciphertext = fernet.encrypt(json.dumps(self._data).encode("utf-8"))

        with open(self.vault_path, "wb") as f:
            f.write(salt + ciphertext)

    def _init_new_vault(self, password: str):
        """Create a fresh vault with a new random salt."""
        salt = os.urandom(_SALT_SIZE)
        self._key = self._derive_key(password, salt)
        self._data = {}

        fernet = Fernet(self._key)
        ciphertext = fernet.encrypt(json.dumps(self._data).encode("utf-8"))

        with open(self.vault_path, "wb") as f:
            f.write(salt + ciphertext)

    # ── Public API ─────────────────────────────────────────────────

    def rekey(self, password: str) -> Path | None:
        """
        Backup the existing vault file and create a new one keyed to password.
        Used when the Argon2 master password is correct but the vault salt/key differs.
        """
        with self._lock:
            backup_path = None
            if self.vault_path.exists():
                backup_path = self.vault_path.with_name(
                    f"vault.enc.bak-{int(time.time())}"
                )
                shutil.copy2(self.vault_path, backup_path)
                self.vault_path.unlink()
                logger.warning(f"Vault re-keyed; previous file backed up to {backup_path.name}")
            self._key = None
            self._data = {}
            self._init_new_vault(password)
            audit.log_event("VAULT_REKEYED", {"backup": backup_path.name if backup_path else None})
            return backup_path

    def unlock(self, password: str):
        """Unlock the vault using the master password."""
        with self._lock:
            if self.vault_path.exists():
                try:
                    self._load(password)
                    logger.info("Secure Vault unlocked successfully.")
                    audit.log_event("VAULT_UNLOCK", {"status": "success"})
                except Exception as e:
                    self._key = None
                    self._data = {}
                    logger.error(f"Failed to unlock vault: {e}")
                    audit.log_event("VAULT_UNLOCK_FAIL", {"reason": str(e)})
                    raise ValueError(f"Invalid vault password or corrupted vault: {e}")
            else:
                logger.info("Initializing new Secure Vault.")
                self._init_new_vault(password)
                audit.log_event("VAULT_INITIALIZED", {})

    def set_secret(self, key: str, value: str):
        with self._lock:
            if not self._key:
                raise PermissionError("Vault is locked")
            self._data[key] = value
            self._save()
            audit.log_event("VAULT_SECRET_STORED", {"key": key})

    def get_secret(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            if not self._key:
                raise PermissionError("Vault is locked")
            audit.log_event("VAULT_SECRET_ACCESSED", {"key": key})
            return self._data.get(key, default)

    def delete_secret(self, key: str):
        with self._lock:
            if not self._key:
                raise PermissionError("Vault is locked")
            if key in self._data:
                del self._data[key]
                self._save()
                audit.log_event("VAULT_SECRET_DELETED", {"key": key})

    def lock(self):
        """Wipe the derived key and loaded secrets from RAM."""
        self.clear_cached_keys()

    def clear_cached_keys(self):
        """Securely clears key and data memory buffers."""
        with self._lock:
            self._key = None
            self._data = {}
        logger.info("Secure Vault memory buffers cleared.")
        audit.log_event("VAULT_LOCKED", {})

    @property
    def is_unlocked(self) -> bool:
        return self._key is not None


# Global vault instance
vault = SecureVault()
