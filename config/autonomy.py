from loguru import logger
from sandbox.validator import RiskLevel
from audit.trail import audit

class AutonomyMode:
    MANUAL = "MANUAL"                   # Only direct commands execute
    ASSISTED = "ASSISTED"               # Agents suggest, user confirms everything
    SEMI_AUTONOMOUS = "SEMI_AUTONOMOUS" # Low risk auto-executes, Medium+ prompts
    FULL_AUTONOMOUS = "FULL_AUTONOMOUS" # Low/Medium auto-executes, High/Critical prompts

class AutonomyManager:
    """
    Manages the current autonomy level and determines if an action requires UI confirmation.
    """
    def __init__(self, default_mode: str = AutonomyMode.ASSISTED):
        self.current_mode = default_mode

    def set_mode(self, mode: str):
        if mode in [AutonomyMode.MANUAL, AutonomyMode.ASSISTED, AutonomyMode.SEMI_AUTONOMOUS, AutonomyMode.FULL_AUTONOMOUS]:
            logger.info(f"Autonomy mode changing from {self.current_mode} to {mode}")
            audit.log_event("AUTONOMY_MODE_CHANGE", {"old_mode": self.current_mode, "new_mode": mode})
            self.current_mode = mode
        else:
            logger.error(f"Invalid autonomy mode: {mode}")

    def requires_confirmation(self, risk_level: str) -> bool:
        """
        Determines if an action with the given risk level requires explicit user confirmation
        based on the current autonomy mode.
        """
        # CRITICAL actions ALWAYS require confirmation, regardless of mode.
        if risk_level == RiskLevel.CRITICAL:
            return True

        if self.current_mode == AutonomyMode.MANUAL:
            # In manual mode, everything that isn't a direct user text/voice command is blocked or prompts.
            return True
            
        elif self.current_mode == AutonomyMode.ASSISTED:
            # In assisted mode, the AI suggests actions, but user must confirm anything that mutates state (Medium+).
            # Even Low risk might require a "continue" prompt in strict assisted mode, but let's say Low is auto, rest is prompted.
            if risk_level in [RiskLevel.MEDIUM, RiskLevel.HIGH]:
                return True
            return False
            
        elif self.current_mode == AutonomyMode.SEMI_AUTONOMOUS:
            # Low and Medium might auto-execute if they don't modify critical state, but for safety:
            # Low is auto. Medium and High prompt.
            if risk_level in [RiskLevel.MEDIUM, RiskLevel.HIGH]:
                return True
            return False
            
        elif self.current_mode == AutonomyMode.FULL_AUTONOMOUS:
            # Low and Medium auto-execute. High and Critical prompt.
            if risk_level == RiskLevel.HIGH:
                return True
            return False
            
        return True # Default to safe

# Global instance — initialized from settings
from config.settings import settings as _settings

_configured_mode = getattr(_settings, "autonomy_level", AutonomyMode.ASSISTED)
_valid_modes = [AutonomyMode.MANUAL, AutonomyMode.ASSISTED,
                AutonomyMode.SEMI_AUTONOMOUS, AutonomyMode.FULL_AUTONOMOUS]

# Normalize: settings may use hyphens (SEMI-AUTONOMOUS) vs underscores (SEMI_AUTONOMOUS)
_configured_mode = _configured_mode.replace("-", "_")
if _configured_mode not in _valid_modes:
    logger.warning(f"Invalid autonomy_level '{_configured_mode}' in settings. Defaulting to ASSISTED.")
    _configured_mode = AutonomyMode.ASSISTED

autonomy = AutonomyManager(default_mode=_configured_mode)
