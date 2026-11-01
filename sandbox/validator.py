import re
from loguru import logger

class RiskLevel:
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

class CommandValidator:
    """
    Analyzes terminal commands to determine risk level and block 
    destructive operations.
    """
    
    # Patterns that instantly trigger CRITICAL risk
    CRITICAL_PATTERNS = [
        r"(?i)\brm\s+-rf\b",                    # Linux mass delete
        r"(?i)Remove-Item\s+-Recurse\s+-Force", # Windows mass delete
        r"(?i)\bdel\s+/s\s+/q\b",               # Windows cmd mass delete
        r"(?i)format\s+[a-z]:",                 # Disk format
        r"(?i)net\s+user.*\/add",               # User creation
        r"(?i)runas\s+/user",                   # Privilege escalation
        r"(?i)icacls.*grant\s+.*:F",            # Granting full permissions
    ]

    # Patterns for HIGH risk
    HIGH_PATTERNS = [
        r"(?i)\brm\s+",                         # Basic delete
        r"(?i)Remove-Item\b",                   # Basic delete Windows
        r"(?i)\bdel\b",                         # Basic delete cmd
        r"(?i)Stop-Process\b",                  # Killing processes
        r"(?i)taskkill\b",                      # Killing processes cmd
        r"(?i)\bcurl\s+.*\|",                   # Curl piping to shell
        r"(?i)\bwget\s+.*\|",                   # Wget piping to shell
        r"(?i)Invoke-WebRequest.*\|",           # PS web request piping
        r"(?i)\.ssh",                           # Touching SSH keys
        r"(?i)credentials",                     # Credential access
    ]

    def __init__(self):
        self.critical_regexes = [re.compile(p) for p in self.CRITICAL_PATTERNS]
        self.high_regexes = [re.compile(p) for p in self.HIGH_PATTERNS]

    def score_command(self, command: str) -> str:
        """
        Calculates the risk level of a given command.
        """
        for regex in self.critical_regexes:
            if regex.search(command):
                logger.warning(f"CRITICAL risk detected in command: {command}")
                return RiskLevel.CRITICAL
                
        for regex in self.high_regexes:
            if regex.search(command):
                logger.warning(f"HIGH risk detected in command: {command}")
                return RiskLevel.HIGH
                
        # Any file modification or network operation not explicitly flagged is medium
        if re.search(r"(?i)(>|>>|curl|wget|Invoke-RestMethod|New-Item|Set-Content)", command):
            return RiskLevel.MEDIUM
            
        return RiskLevel.LOW

    def explain_risk(self, command: str, risk: str) -> str:
        if risk == RiskLevel.CRITICAL:
            return "Command attempts mass deletion, privilege escalation, or format operations. High risk of system destruction."
        elif risk == RiskLevel.HIGH:
            return "Command attempts to delete files, kill processes, or access sensitive configuration."
        elif risk == RiskLevel.MEDIUM:
            return "Command mutates local state or performs outbound network requests."
        return "Standard read-only or low-impact operation."

# Global instance
validator = CommandValidator()
