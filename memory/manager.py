"""
memory/manager.py

PRIMUS Memory System — Local-First, Encrypted, Context-Aware

Architecture:
  ┌─────────────────────────────────────────────────────────┐
  │  Short-term memory  →  SQLite (last N interactions)     │
  │  Long-term memory   →  ChromaDB + local SentenceTransf. │
  │  Sensitive memory   →  Tagged + routed through Vault     │
  └─────────────────────────────────────────────────────────┘

All heavy modules (SentenceTransformer, ChromaDB) are lazily loaded
to avoid slowing startup on low-power hardware (AMD iGPU).
"""
from __future__ import annotations

import time
import sqlite3
import json
from pathlib import Path
from typing import Optional, List
from loguru import logger
from config.settings import settings
from audit.trail import audit


# ─────────────────────────────────────────────────────────────────
# Short-Term Memory  (SQLite — fast, synchronous, zero external deps)
# ─────────────────────────────────────────────────────────────────

class ShortTermMemory:
    """Stores the last N conversation turns and events in a local SQLite database."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS interactions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp   REAL    NOT NULL,
        role        TEXT    NOT NULL,   -- 'user' | 'assistant' | 'system'
        content     TEXT    NOT NULL,
        agent       TEXT    DEFAULT 'CorePrimus',
        is_sensitive INTEGER DEFAULT 0,
        expires_at  REAL    DEFAULT NULL
    );
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _init_db(self):
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.execute(self.SCHEMA)
        self._conn.commit()
        logger.info(f"Short-term memory initialized at {self.db_path}")

    def add(self, role: str, content: str, agent: str = "CorePrimus",
            is_sensitive: bool = False, ttl_seconds: Optional[int] = None):
        if settings.private_mode:
            logger.debug("Memory storage suppressed — Private Mode active.")
            return

        expires_at = time.time() + ttl_seconds if ttl_seconds else None
        self._conn.execute(
            "INSERT INTO interactions (timestamp, role, content, agent, is_sensitive, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), role, content, agent, int(is_sensitive), expires_at)
        )
        self._conn.commit()

    def get_recent(self, limit: int = 20) -> List[dict]:
        """Return the most recent interactions, filtering expired ones."""
        now = time.time()
        rows = self._conn.execute(
            "SELECT role, content, agent, timestamp FROM interactions "
            "WHERE (expires_at IS NULL OR expires_at > ?) "
            "ORDER BY timestamp DESC LIMIT ?",
            (now, limit)
        ).fetchall()
        return [{"role": r[0], "content": r[1], "agent": r[2], "timestamp": r[3]} for r in reversed(rows)]

    def purge_expired(self):
        self._conn.execute("DELETE FROM interactions WHERE expires_at IS NOT NULL AND expires_at < ?", (time.time(),))
        self._conn.commit()

    def clear_all(self):
        """Hard-wipe all memory — used when entering Private Mode."""
        self._conn.execute("DELETE FROM interactions")
        self._conn.commit()
        logger.warning("Short-term memory cleared.")


# ─────────────────────────────────────────────────────────────────
# Long-Term Vector Memory (ChromaDB + SentenceTransformers — lazy)
# ─────────────────────────────────────────────────────────────────

class LongTermMemory:
    """
    Semantic vector memory using ChromaDB with local SentenceTransformers embeddings.
    Lazy-loaded to avoid blocking startup.
    CPU-first; no CUDA assumptions.
    """

    def __init__(self, vector_db_path: Path):
        self.vector_db_path = vector_db_path
        self._chroma_client = None
        self._collection = None
        self._embed_model = None
        self._loaded = False

    def _lazy_load(self):
        if self._loaded:
            return
        try:
            import chromadb
            from sentence_transformers import SentenceTransformer

            logger.info("Loading local embedding model (all-MiniLM-L6-v2) — CPU mode...")
            # CPU-only by default; abstracted for future ONNX / DirectML
            self._embed_model = SentenceTransformer(
                "sentence-transformers/all-MiniLM-L6-v2",
                device="cpu"
            )

            self._chroma_client = chromadb.PersistentClient(path=str(self.vector_db_path))
            self._collection = self._chroma_client.get_or_create_collection(
                name="primus_memory",
                metadata={"hnsw:space": "cosine"}
            )
            self._loaded = True
            logger.info("Long-term vector memory ready.")
        except ImportError as e:
            logger.error(f"Failed to load vector memory dependencies: {e}")
            raise

    def store(self, text: str, metadata: Optional[dict] = None, doc_id: Optional[str] = None):
        if settings.private_mode:
            return

        self._lazy_load()
        embedding = self._embed_model.encode([text], convert_to_numpy=True).tolist()
        doc_id = doc_id or f"mem_{int(time.time() * 1000)}"
        self._collection.add(
            ids=[doc_id],
            embeddings=embedding,
            documents=[text],
            metadatas=[metadata or {}]
        )
        audit.log_event("MEMORY_STORED", {"doc_id": doc_id, "sensitive": metadata.get("is_sensitive", False) if metadata else False})

    def search(self, query: str, top_k: int = 5) -> List[dict]:
        self._lazy_load()
        embedding = self._embed_model.encode([query], convert_to_numpy=True).tolist()
        results = self._collection.query(
            query_embeddings=embedding,
            n_results=top_k,
            include=["documents", "metadatas", "distances"]
        )
        output = []
        for doc, meta, dist in zip(results["documents"][0], results["metadatas"][0], results["distances"][0]):
            output.append({"text": doc, "metadata": meta, "score": 1 - dist})
        audit.log_event("MEMORY_RETRIEVED", {"query": query[:80], "results": len(output)})
        return output


# ─────────────────────────────────────────────────────────────────
# Memory Manager — unified facade
# ─────────────────────────────────────────────────────────────────

class MemoryManager:
    """
    Unified interface to short-term (SQLite) and long-term (ChromaDB) memory.
    Routes sensitive content through secure tagging and expiration.
    """

    def __init__(self):
        self.short_term = ShortTermMemory(settings.memory_db_path)
        self.long_term = LongTermMemory(settings.vector_db_path)

    def remember(self, role: str, content: str, agent: str = "CorePrimus",
                 is_sensitive: bool = False, ttl_seconds: Optional[int] = None):
        """Store in short-term memory. Sensitive items get short TTL by default."""
        effective_ttl = ttl_seconds if ttl_seconds else (3600 if is_sensitive else None)
        self.short_term.add(role, content, agent, is_sensitive, effective_ttl)

    def embed_and_store(self, text: str, metadata: Optional[dict] = None):
        """Store in long-term semantic memory."""
        self.long_term.store(text, metadata)

    def recall_context(self, query: str, top_k: int = 5) -> List[dict]:
        """Semantic search across long-term memory."""
        return self.long_term.search(query, top_k)

    def get_recent_conversation(self, limit: int = 20) -> List[dict]:
        """Return recent short-term interactions as a chat context list."""
        return self.short_term.get_recent(limit)

    def enter_private_mode(self):
        """Wipe all non-persistent memory buffers immediately."""
        self.short_term.clear_all()
        logger.warning("Memory Manager: Private Mode engaged. All session memory cleared.")


# Global singleton
memory = MemoryManager()
