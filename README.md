# PRIMUS
*Secure, Local-First AI Operating System*

PRIMUS is a production-grade, privacy-first AI desktop operating assistant for Windows. It features a transparent PyQt6/QML overlay, local AI processing via Ollama, AES-256 encrypted storage, and immutable audit trails.

## Phase 1 Execution

### Prerequisites
1. **Python 3.12+** must be installed.
2. **Ollama** must be installed on your system (Download from `ollama.com`).

### Environment Configuration
1. Clone this repository to `d:\primus` (or your chosen location).
2. Create a `.env` file in the root directory:
```env
# .env file example
PRIMUS_VERSION="1.0.0"
THEME="tactical_orange"
LOCAL_AI_ENABLED=True
PRIMARY_LOCAL_MODEL="llama3"
MASTER_PASSWORD_HASH="your_secure_password" # Used to unlock the AES vault
```

### Dependency Installation
Open an administrator terminal (PowerShell) and run:
```powershell
cd d:\primus
python -m venv venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

### Local Model Setup
Before running PRIMUS, ensure your local Ollama model is downloaded:
```powershell
ollama run llama3
# Once it finishes downloading, you can exit the ollama prompt.
```

### Execution
Start the PRIMUS Core Orchestrator:
```powershell
python main.py
```

### Controls
* **Activate Overlay:** `CTRL + WIN + J`
* **Emergency Kill Switch:** `CTRL + SHIFT + ALT + P`

### Security Notes
- The vault is located at `security/vault.enc`. It is AES-256 encrypted.
- The immutable audit trail is located at `audit/audit.log`.
- All operations prioritize local execution to ensure absolute privacy.
