import sys
import os
import asyncio
import threading
import time
from loguru import logger
from config.settings import settings
from core.hotkeys import HotkeyManager
from audit.trail import audit
from security.auth_manager import auth_manager, AuthState

class PrimusOrchestrator:
    """
    Central orchestrator for the PRIMUS OS.
    Manages the lifecycle of UI, Voice, Memory, and Agents.
    Executes the asyncio event loop on a background thread.
    """
    def __init__(self, ui_engine):
        self._setup_logging()
        logger.info(f"Initializing {settings.project_name} v{settings.version}")
        
        self.ui_engine = ui_engine
        self.hotkey_manager = HotkeyManager(
            launch_callback=self.on_launch,
            kill_switch_callback=self.on_kill_switch
        )
        
        self.async_loop = None
        self.async_thread = None
        self.is_active = True  # Starts visible
        self.listen_task = None
        self.vad_threshold = settings.vad_threshold
        self.last_active_window = "None"
        
        # Connect keyboard console command handler
        self.ui_engine.bridge.text_command_callback = self.execute_keyboard_command

    def _setup_logging(self):
        # Configure loguru to write to the system log path
        logger.add(
            settings.system_log_path, 
            rotation="10 MB", 
            retention="10 days", 
            level="DEBUG",
            enqueue=True
        )

    def start(self):
        """Starts the background asyncio event loop thread and hotkeys."""
        logger.info("Starting background services thread...")
        
        # 1. Start hotkeys listener thread
        self.hotkey_manager.start()
        
        # 2. Start dedicated background asyncio loop thread
        self.async_loop = asyncio.new_event_loop()
        self.ui_engine.bridge.set_loop(self.async_loop)
        
        self.async_thread = threading.Thread(
            target=self._run_async_loop,
            args=(self.async_loop,),
            daemon=True
        )
        self.async_thread.start()
        
        # 3. Schedule background systems initialization
        asyncio.run_coroutine_threadsafe(self._init_systems(), self.async_loop)
        logger.info(f"{settings.project_name} runtime initialized. Press {settings.launch_hotkey} to toggle.")

    def _run_async_loop(self, loop):
        asyncio.set_event_loop(loop)
        loop.run_forever()

    async def _init_systems(self):
        """Initialize workers, telemetry, memory, and voice loops in background thread."""
        logger.info("Initializing background subsystems...")
        
        # Connect RestrictedExecutor confirmation callback
        from sandbox.executor import executor
        executor.set_confirmation_callback(self.request_command_confirmation)
        
        # Start system telemetry monitor
        from system.telemetry import telemetry
        telemetry.start()
        
        # Start proactive workers
        from core.workers import worker_manager
        worker_manager.start_all(self.async_loop, self.on_worker_alert)
        
        # Schedule telemetry and workers UI updates loop
        self.async_loop.create_task(self.telemetry_ui_update_loop())

        # Schedule session monitoring and HUD status updater
        self.async_loop.create_task(self.check_session_expiry_loop())

        # Ensure overlay accepts keyboard input on startup
        self.ui_engine.bridge.overlayVisibilityChanged.emit(self.is_active)

        # Auto-launch password setup wizard if no master password exists
        if not auth_manager.is_enrolled:
            self.async_loop.create_task(self.trigger_initial_setup())
        else:
            self.ui_engine.bridge.push_status("SYSTEM: ACTIVE - Enter password to unlock commands")

        # Initialize voice listening (STT/LLM) when active and enrolled
        if self.is_active and auth_manager.is_enrolled:
            self.start_listening()

    def on_launch(self):
        """Toggle HUD overlay visibility and start/stop voice listening."""
        self.is_active = not self.is_active
        logger.info(f"Toggling overlay visibility: {self.is_active}")
        
        # Emit visibility changed signal to QML thread-safely
        self.ui_engine.bridge.overlayVisibilityChanged.emit(self.is_active)
        
        if self.is_active:
            if auth_manager.is_enrolled:
                self.ui_engine.bridge.push_status("SYSTEM: ACTIVE - LISTENING")
                self.start_listening()
            else:
                self.ui_engine.bridge.push_status("SYSTEM: ACTIVE [NOT ENROLLED]")
        else:
            self.ui_engine.bridge.push_status("SYSTEM: STANDBY")
            self.stop_listening()

    def start_listening(self):
        if self.async_loop and (self.listen_task is None or self.listen_task.done()):
            self.async_loop.call_soon_threadsafe(self._create_listen_task)

    def _create_listen_task(self):
        if self.listen_task is None or self.listen_task.done():
            self.listen_task = self.async_loop.create_task(self.continuous_listening_loop())
            logger.info("Voice listening task created on event loop.")

    def stop_listening(self):
        if self.listen_task:
            self.async_loop.call_soon_threadsafe(self.listen_task.cancel)
            self.listen_task = None
            logger.info("Voice listening task cancellation requested.")

    async def continuous_listening_loop(self):
        """Asynchronously listens to the microphone, runs VAD, Auth, STT, LLM, and TTS."""
        logger.info("Continuous voice listening loop active.")
        import sounddevice as sd
        import numpy as np
        
        sample_rate = 16000
        block_size = 1024  # 64ms frames
        channels = 1
        
        silence_limit_sec = 1.5
        
        q = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def callback(indata, frames, time_info, status):
            if status:
                logger.warning(f"Audio status: {status}")
            loop.call_soon_threadsafe(q.put_nowait, indata.copy())

        # Open microphone stream with safety checks
        try:
            stream = sd.InputStream(
                samplerate=sample_rate,
                channels=channels,
                dtype='int16',
                blocksize=block_size,
                callback=callback
            )
        except Exception as e:
            logger.warning(f"Failed to open audio input device: {e}. Running in degraded (text-only) mode.")
            self.ui_engine.bridge.push_status("SYSTEM: ACTIVE [MIC UNAVAILABLE]")
            # Log to audit log
            audit.log_event("MIC_UNAVAILABLE", {"error": str(e)})
            return

        with stream:
            recording = []
            silent_blocks = 0
            in_utterance = False
            
            logger.info(f"Microphone listening. VAD Threshold: {self.vad_threshold}")
            
            try:
                while True:
                    # Await new chunk from audio stream queue - yields control when queue is empty
                    chunk = await q.get()
                    
                    # Calculate energy (RMS)
                    rms = np.sqrt(np.mean(chunk.astype(np.float64)**2))
                    
                    # Check dynamic threshold
                    threshold = self.vad_threshold
                    
                    if rms > threshold:
                        if not in_utterance:
                            logger.info(f"Voice activity detected (RMS: {rms:.1f} > threshold: {threshold})...")
                            in_utterance = True
                            self.ui_engine.bridge.push_status("LISTENING...")
                        recording.append(chunk)
                        silent_blocks = 0
                    else:
                        if in_utterance:
                            recording.append(chunk)
                            silent_blocks += 1
                            
                            # Check silence limit
                            if (silent_blocks * block_size / sample_rate) >= silence_limit_sec:
                                logger.info("Silence detected. Processing utterance...")
                                utterance = np.concatenate(recording)
                                recording = []
                                in_utterance = False
                                silent_blocks = 0
                                
                                # Process voice command in background task so we don't block VAD loop
                                asyncio.create_task(self.process_utterance(utterance.tobytes(), sample_rate))
            except asyncio.CancelledError:
                logger.info("Voice listening loop cancelled.")
            finally:
                logger.info("Exited voice stream context.")

    async def process_utterance(self, audio_bytes: bytes, sample_rate: int):
        """Authenticates, transcribes, queries LLM, and speaks response."""
        try:
            loop = asyncio.get_running_loop()
            
            # 1. Session verification (password only)
            if auth_manager.current_state != AuthState.VERIFIED:
                self.ui_engine.bridge.push_status("AUTH REQUIRED...")
                verified = await self.ui_engine.bridge.request_password_fallback("Enter master password to use voice commands.")
                if not verified:
                    self.ui_engine.bridge.push_status("AUTH DENIED")
                    self.ui_engine.bridge.push_status("SYSTEM: ACTIVE - LISTENING")
                    return
            auth_manager.update_activity()

            # 2. Transcription (STT)
            self.ui_engine.bridge.push_status("TRANSCRIBING...")
            from voice.pipeline import voice_pipeline
            text = await loop.run_in_executor(None, voice_pipeline.transcribe, audio_bytes)
            logger.info(f"Transcribed voice command: '{text}'")
            if not text.strip():
                self.ui_engine.bridge.push_status("SYSTEM: ACTIVE - LISTENING")
                return
                
            self.ui_engine.bridge.push_status(f"COMMAND: {text}")

            # 3. Intent Routing / LLM Response
            self.ui_engine.bridge.push_status("PROCESSING...")
            
            # Context Retrieval (Memory + active window)
            recent = []
            semantic_str = ""
            try:
                from memory.manager import memory
                recent = memory.get_recent_conversation(limit=5)
                # Semantic search ChromaDB for relevant long-term memory context
                memories = memory.recall_context(text, top_k=3)
                if memories:
                    semantic_str = "\n".join([f"- {m['text']}" for m in memories])
            except Exception as e:
                logger.error(f"Failed to load context from memory: {e}")
            recent_str = "\n".join([f"{r['role']}: {r['content']}" for r in recent])
            
            active_window = ""
            try:
                from core.workers import worker_manager
                # Find the context worker
                for w in worker_manager.workers:
                    if w.name == "ContextAwarenessWorker":
                        active_window = getattr(w, "_last_window", "")
            except Exception:
                pass

            system_prompt = (
                "You are PRIMUS, a secure, local-first AI desktop operating assistant for Windows.\n"
                f"Current Foreground Window: {active_window}\n"
                f"Session Auth Status: {auth_manager.current_state.value}\n"
                "Recent conversation history:\n"
                f"{recent_str}\n\n"
                "Relevant long-term memories:\n"
                f"{semantic_str}\n\n"
                "Formulate a helpful, direct, and concise voice response."
            )
            
            from models.llm import ai_engine
            response = await loop.run_in_executor(None, ai_engine.generate_response, text, system_prompt)
            logger.info(f"Ollama response: {response}")
            
            # Save to memory
            try:
                from memory.manager import memory
                memory.remember("user", text)
                memory.remember("assistant", response)
                if len(text) > 10:
                    # Run embedding in executor so it does not block loop thread
                    await loop.run_in_executor(
                        None, 
                        memory.embed_and_store, 
                        f"User asked: {text}. Primus replied: {response}"
                    )
            except Exception as e:
                logger.error(f"Failed to record memory: {e}")

            # 4. Speak response (TTS)
            self.ui_engine.bridge.push_status("SPEAKING...")
            await loop.run_in_executor(None, voice_pipeline.synthesize, response)
            
            self.ui_engine.bridge.push_status("SYSTEM: ACTIVE - LISTENING")
            
        except Exception as e:
            logger.error(f"Error processing utterance: {e}")
            self.ui_engine.bridge.push_status("ERROR PROCESSING VOICE")
            await asyncio.sleep(2)
            self.ui_engine.bridge.push_status("SYSTEM: ACTIVE - LISTENING")

    async def request_command_confirmation(self, risk: str, agent: str, action: str, target: str, command: str) -> bool:
        """Called by RestrictedExecutor to prompt QML and await response."""
        logger.warning(f"Orchestrator requesting user confirmation for {command}")
        
        # Security Gating: check if verified. If not verified, trigger fallback password prompt!
        if auth_manager.current_state != AuthState.VERIFIED:
            logger.info("Access denied to unverified session. Requesting manual password validation...")
            verified = await self.ui_engine.bridge.request_password_fallback("Verification required to execute command.")
            if not verified:
                logger.warning("Action denied: manual verification failed.")
                return False
                
        # Once verified, show the standard prompt dialog
        result = await self.ui_engine.bridge.request_confirmation(risk, agent, action, target, command)
        return result

    def on_worker_alert(self, category: str, message: str):
        """Callback for background worker alerts."""
        logger.warning(f"Worker Alert [{category}]: {message}")
        if category == "CONTEXT" and message.startswith("Active window: "):
            self.last_active_window = message[15:]
            
        self.ui_engine.bridge.push_status(f"ALERT: {message}")
        # Log to audit trail
        audit.log_event(f"WORKER_{category}", {"message": message})

    async def telemetry_ui_update_loop(self):
        """Regularly feeds telemetry stats to the QML HUD."""
        from system.telemetry import telemetry
        while True:
            try:
                stats = telemetry.get_stats()
                # Push stats thread-safely via Qt signal
                self.ui_engine.bridge.push_telemetry(
                    stats.get("cpu_percent", 0.0),
                    stats.get("ram_percent", 0.0),
                    stats.get("gpu_percent", 0.0)
                )
            except Exception as e:
                logger.error(f"Telemetry update loop error: {e}")
            await asyncio.sleep(2.0)

    def update_hud_status(self):
        """Updates the statusText on HUD with central state information."""
        state = auth_manager.current_state.value
        enrolled = "ENROLLED" if auth_manager.is_enrolled else "NOT ENROLLED"
        active_window = getattr(self, "last_active_window", "None")
        
        # Check vault unlocked status
        from security.vault import vault
        vault_status = "UNLOCKED" if vault.is_unlocked else "LOCKED"
        
        status_msg = (
            f"SYSTEM: {state}\n"
            f"VAULT: {vault_status}\n"
            f"WINDOW: {active_window[:28]}\n"
            f"ENROLL: {enrolled}"
        )
        self.ui_engine.bridge.push_status(status_msg)

    async def check_session_expiry_loop(self):
        """Monitors session inactivity and schedules updates."""
        while True:
            try:
                # Check for timeout and clear memory keys if expired
                auth_manager.check_session_expiry()
                
                # Check lockout timer
                if auth_manager.is_locked_out():
                    self.ui_engine.bridge.lockoutStarted.emit(auth_manager.get_lockout_remaining())
                
                self.update_hud_status()
            except Exception as e:
                logger.error(f"Session monitoring loop error: {e}")
            await asyncio.sleep(5.0)

    async def trigger_initial_setup(self):
        """Delays slightly and launches the first-time password setup wizard."""
        await asyncio.sleep(1.5)
        logger.info("Initializing first-time password setup wizard.")
        success = await self.ui_engine.bridge.run_enrollment()
        if success:
            logger.info("Password setup successful. Activating voice listener...")
            auth_manager.transition_to(AuthState.VERIFIED)
            self.ui_engine.bridge.push_status("SYSTEM: ACTIVE - LISTENING")
            self.start_listening()

    def execute_keyboard_command(self, command: str):
        """Helper slot target for QML console text prompt."""
        if self.async_loop:
            asyncio.run_coroutine_threadsafe(self.process_keyboard_command_task(command), self.async_loop)

    async def process_keyboard_command_task(self, command: str):
        """Runs keyboard command validation and execution."""
        logger.info(f"Processing text command input: {command}")
        
        # Gating: check verification
        if auth_manager.current_state != AuthState.VERIFIED:
            self.ui_engine.bridge.push_status("AUTH REQUIRED FOR SHELL COMMAND")
            verified = await self.ui_engine.bridge.request_password_fallback("Verification required to run command.")
            if not verified:
                self.ui_engine.bridge.push_status("SHELL COMMAND BLOCKED - AUTH FAILED")
                return

        self.ui_engine.bridge.push_status(f"EXEC: {command}")
        from sandbox.executor import executor
        result = await executor.execute(command, "KeyboardAgent")
        
        if result.get("blocked"):
            self.ui_engine.bridge.push_status(f"BLOCKED: {result['stderr']}")
        else:
            stdout = result.get("stdout", "").strip()[:50]
            self.ui_engine.bridge.push_status(f"SUCCESS: {stdout}")
            
        audit.log_event("KEYBOARD_COMMAND_EXECUTED", {"command": command, "success": not result.get("blocked")})

    def on_kill_switch(self):
        """Immediate, hard shutdown sequence."""
        logger.critical("EMERGENCY SHUTDOWN INITIATED.")
        audit.log_event("EMERGENCY_KILL_SWITCH", {})
        
        # Stop background threads/loops and close instantly
        try:
            self.stop_listening()
            from core.workers import worker_manager
            worker_manager.stop_all()
            from system.telemetry import telemetry
            telemetry.stop()
        except Exception as e:
            logger.error(f"Error during shutdown cleanup: {e}")
            
        logger.critical("Halt complete. Exiting process.")
        # Hard exit to kill all threads immediately
        os._exit(1)
