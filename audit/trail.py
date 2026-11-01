"""
audit/trail.py

Append-only, hash-chained, thread-safe audit trail for PRIMUS.
All security-critical events are logged here in JSONL format.
"""
import time
import json
import hashlib
import threading
from loguru import logger
from config.settings import settings


class AuditTrail:
    """
    Append-only immutable audit trail for security-critical events.
    Thread-safe via an internal lock.
    Uses SHA-256 hash chaining for tamper-evidence.
    """

    def __init__(self):
        self.log_path = settings.audit_log_path
        self._lock = threading.Lock()
        self._ensure_file()

    def _ensure_file(self):
        try:
            if not self.log_path.exists():
                with open(self.log_path, "w", encoding="utf-8") as f:
                    import os
                    genesis_block = {
                        "timestamp": time.time(),
                        "event": "AUDIT_START",
                        "agent": "SYSTEM",
                        "details": {},
                        "previous_hash": None,
                        "hash": hashlib.sha256(os.urandom(16)).hexdigest()
                    }
                    f.write(json.dumps(genesis_block) + "\n")
        except Exception as e:
            logger.warning(f"Audit trail initialization warning: {e}")

    def _get_last_hash(self) -> str | None:
        """Read the hash from the last valid line of the audit log."""
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                last_line = ""
                for line in f:
                    stripped = line.strip()
                    if stripped:
                        last_line = stripped
                if last_line:
                    last_event = json.loads(last_line)
                    return last_event.get("hash")
        except Exception:
            pass
        return None

    def log_event(self, event_type: str, details: dict, agent: str = "SYSTEM"):
        """
        Log a security-critical event to the immutable audit trail.
        Thread-safe — protected by internal lock.
        """
        with self._lock:
            previous_hash = self._get_last_hash()

            event_data = {
                "timestamp": time.time(),
                "event": event_type,
                "agent": agent,
                "details": details,
                "previous_hash": previous_hash
            }

            # Calculate new hash (hash chaining)
            event_string = json.dumps(event_data, sort_keys=True)
            event_hash = hashlib.sha256(event_string.encode("utf-8")).hexdigest()
            event_data["hash"] = event_hash

            try:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event_data) + "\n")
            except Exception as e:
                logger.error(f"Failed to write audit event {event_type}: {e}")

        logger.debug(f"[AUDIT] {event_type} - {agent}")

    def verify_chain(self) -> bool:
        """
        Verify the integrity of the entire audit chain.
        Returns True if the chain is intact, False if tampered.
        """
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]

            if not lines:
                return True

            for i, line in enumerate(lines):
                event = json.loads(line)
                stored_hash = event.pop("hash")
                computed = hashlib.sha256(
                    json.dumps(event, sort_keys=True).encode("utf-8")
                ).hexdigest()
                if stored_hash != computed:
                    logger.error(f"Audit chain integrity violation at event {i}: {event.get('event')}")
                    return False
            return True
        except Exception as e:
            logger.error(f"Audit chain verification failed: {e}")
            return False


# Global instance
audit = AuditTrail()
