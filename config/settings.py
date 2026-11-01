import os
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Base directory of the project
BASE_DIR = Path(__file__).resolve().parent.parent

class AppSettings(BaseSettings):
    """
    Centralized configuration for PRIMUS.
    Reads from .env file or environment variables.
    """
    # Project Identity
    project_name: str = "PRIMUS"
    version: str = "1.0.0"
    
    # UI Theme
    theme: str = "tactical_orange"
    transparent_ui: bool = True
    always_on_top: bool = True
    
    # Hotkeys
    launch_hotkey: str = "ctrl+win+j"
    kill_switch_hotkey: str = "ctrl+shift+alt+p"

    # AI & Models (Local-First)
    local_ai_enabled: bool = True
    ollama_base_url: str = "http://localhost:11434"
    primary_local_model: str = "llama3"
    cloud_fallback_enabled: bool = False
    openai_api_key: str = ""
    
    # Voice Pipeline
    stt_model_size: str = "base.en" # "tiny.en", "base.en", "small.en"
    local_tts_enabled: bool = True
    elevenlabs_api_key: str = ""
    elevenlabs_voice: str = "Rachel"  # female voice (ElevenLabs)
    tts_voice_gender: str = "female"  # pyttsx3 / Windows SAPI hint
    tts_windows_voice_name: str = ""    # e.g. "Microsoft Zira Desktop" — empty = auto female
    vad_threshold: int = 500
    
    # Security & Autonomy
    autonomy_level: str = "ASSISTED" # MANUAL, ASSISTED, SEMI-AUTONOMOUS, FULL
    private_mode: bool = False
    master_password_hash: str = "" # Set upon first initialization
    auth_lockout_sec: int = 300
    session_duration_sec: int = 900
    
    # Paths
    memory_db_path: Path = BASE_DIR / "memory" / "primus_sqlite.db"
    vector_db_path: Path = BASE_DIR / "memory" / "primus_chroma"
    vault_path: Path = BASE_DIR / "security" / "vault.enc"
    audit_log_path: Path = BASE_DIR / "audit" / "audit.log"
    system_log_path: Path = BASE_DIR / "logs" / "system.log"

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding='utf-8',
        extra='ignore'
    )

# Global instance of settings
settings = AppSettings()

# Ensure directories exist
for path in [
    settings.memory_db_path.parent, 
    settings.vector_db_path, 
    settings.vault_path.parent, 
    settings.audit_log_path.parent,
    settings.system_log_path.parent
]:
    path.mkdir(parents=True, exist_ok=True)
