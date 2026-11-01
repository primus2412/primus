"""
voice/pipeline.py

Speech-to-Text (STT) and Text-to-Speech (TTS) pipeline for PRIMUS.
Priority: ElevenLabs cloud TTS → pyttsx3 local TTS → Windows SAPI (PowerShell)
STT: Faster-Whisper on CPU (lazy-loaded, graceful degradation if unavailable)
"""
import os
import sys
import shlex
import numpy as np
from loguru import logger
from config.settings import settings


class VoicePipeline:
    """
    Handles Speech-to-Text (STT) and Text-to-Speech (TTS) operations.
    All heavy modules are lazy-loaded to keep startup fast.
    """

    _FEMALE_VOICE_HINTS = (
        "female", "zira", "hazel", "aria", "jenny", "samantha", "susan",
        "catherine", "linda", "heera", "priya",
    )

    def __init__(self):
        self.stt_model_size = settings.stt_model_size
        self.local_tts = settings.local_tts_enabled
        self.stt_model = None
        self._stt_available = None   # None = not yet probed
        logger.info("Voice Pipeline initialized.")

    def _apply_female_voice_pyttsx3(self, engine) -> None:
        """Select a female voice on Windows SAPI via pyttsx3."""
        if settings.tts_windows_voice_name:
            engine.setProperty("voice", settings.tts_windows_voice_name)
            return
        voices = engine.getProperty("voices") or []
        for voice in voices:
            label = f"{voice.name} {getattr(voice, 'id', '')}".lower()
            if any(hint in label for hint in self._FEMALE_VOICE_HINTS):
                engine.setProperty("voice", voice.id)
                logger.debug(f"pyttsx3 female voice: {voice.name}")
                return
        if len(voices) > 1:
            engine.setProperty("voice", voices[1].id)

    def _female_sapi_ps_prefix(self) -> str:
        """PowerShell prefix to pick a female Windows voice before speaking."""
        if settings.tts_windows_voice_name:
            name = settings.tts_windows_voice_name.replace("'", "''")
            return (
                f"$voices = $synth.GetInstalledVoices() | ForEach-Object {{ $_.VoiceInfo }}; "
                f"$pick = $voices | Where-Object {{ $_.Name -eq '{name}' }} | Select-Object -First 1; "
                f"if ($pick) {{ $synth.SelectVoice($pick.Name) }} "
                f"else {{ $synth.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female) }}; "
            )
        return "$synth.SelectVoiceByHints([System.Speech.Synthesis.VoiceGender]::Female); "

    def _load_stt(self) -> bool:
        """
        Lazy-load the Faster-Whisper model. Returns True if successfully loaded.
        Caches result to avoid repeated load attempts.
        """
        if self._stt_available is True:
            return True
        if self._stt_available is False:
            return False

        logger.info(f"Loading Faster-Whisper model ({self.stt_model_size}) on CPU...")
        try:
            from faster_whisper import WhisperModel
            self.stt_model = WhisperModel(
                self.stt_model_size,
                device="cpu",
                compute_type="int8",
                download_root=str(
                    __import__('pathlib').Path(__file__).resolve().parent.parent / "models" / "whisper"
                )
            )
            logger.info("Faster-Whisper model loaded successfully.")
            self._stt_available = True
            return True
        except ImportError:
            logger.warning(
                "faster-whisper not installed. STT unavailable. "
                "Install: pip install faster-whisper"
            )
            self._stt_available = False
            return False
        except Exception as e:
            logger.error(f"Failed to load Faster-Whisper: {e}")
            self._stt_available = False
            return False

    def transcribe(self, audio_bytes: bytes) -> str:
        """Transcribe raw PCM 16-bit 16kHz audio bytes to text."""
        if not self._load_stt():
            logger.warning("STT unavailable — skipping transcription.")
            return ""

        try:
            # Convert raw 16-bit PCM bytes to float32 numpy array normalized to [-1.0, 1.0]
            audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            logger.debug("Transcribing audio via Faster-Whisper...")
            segments, info = self.stt_model.transcribe(
                audio_np,
                beam_size=1,
                language="en"  # Force English to avoid autodetection overhead
            )
            text = " ".join([segment.text for segment in segments])
            return text.strip()
        except Exception as e:
            logger.error(f"Speech-to-Text transcription failed: {e}")
            return ""

    def synthesize(self, text: str):
        """
        Convert text to speech and play it.
        Prioritizes ElevenLabs → pyttsx3 → Windows SAPI (via EncodedCommand to prevent injection).
        """
        if not text:
            return

        logger.info(f"Synthesizing speech: '{text[:60]}...'")

        # 1. ElevenLabs cloud TTS (if API key is set)
        if settings.elevenlabs_api_key:
            try:
                logger.debug("Using ElevenLabs TTS...")
                from elevenlabs.client import ElevenLabs
                from elevenlabs import play
                client = ElevenLabs(api_key=settings.elevenlabs_api_key)
                audio = client.generate(
                    text=text,
                    voice=settings.elevenlabs_voice,
                    model="eleven_monolingual_v1"
                )
                play(audio)
                return
            except Exception as e:
                logger.warning(f"ElevenLabs TTS failed: {e}. Falling back to local TTS.")

        # 2. Local pyttsx3 offline TTS
        if self.local_tts:
            try:
                logger.debug("Using pyttsx3 local TTS...")
                import pyttsx3
                engine = pyttsx3.init()
                engine.setProperty('rate', 175)
                if settings.tts_voice_gender.lower() == "female":
                    self._apply_female_voice_pyttsx3(engine)
                engine.say(text)
                engine.runAndWait()
                return
            except Exception as e:
                logger.warning(f"pyttsx3 local TTS failed: {e}. Falling back to Windows system TTS.")

        # 3. Windows SAPI via PowerShell EncodedCommand (injection-safe)
        # Use -EncodedCommand with base64 to avoid all PowerShell injection vectors
        try:
            logger.debug("Using Windows PowerShell SAPI fallback (EncodedCommand)...")
            import subprocess
            import base64

            # Encode the text as base64 — prevents ALL injection vectors
            text_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
            voice_pick = self._female_sapi_ps_prefix() if settings.tts_voice_gender.lower() == "female" else ""
            ps_script = (
                "Add-Type -AssemblyName System.Speech; "
                "$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                + voice_pick
                + "$synth.Speak([System.Text.Encoding]::UTF8.GetString("
                "[Convert]::FromBase64String('" + text_b64 + "')));"
            )
            encoded = base64.b64encode(ps_script.encode("utf-16-le")).decode("ascii")
            subprocess.run(
                ["powershell", "-NonInteractive", "-NoProfile", "-EncodedCommand", encoded],
                capture_output=True,
                timeout=30
            )
        except Exception as e:
            logger.error(f"System TTS fallback failed: {e}")


# Global instance
voice_pipeline = VoicePipeline()
