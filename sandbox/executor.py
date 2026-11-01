import asyncio
import psutil
from loguru import logger
from sandbox.validator import validator, RiskLevel
from audit.trail import audit

class RestrictedExecutor:
    """
    Executes terminal commands in a restricted environment.
    Enforces timeouts and validates commands against the risk scorer before execution.
    """
    def __init__(self, default_timeout: int = 30):
        self.default_timeout = default_timeout
        self._confirm_callback = None

    def set_confirmation_callback(self, callback):
        self._confirm_callback = callback

    async def execute(self, command: str, agent_name: str, skip_validation: bool = False) -> dict:
        """
        Executes a command asynchronously.
        Returns a dict with stdout, stderr, returncode, and risk level.
        """
        risk = validator.score_command(command)
        
        # Log attempt
        audit.log_event("COMMAND_EXECUTION_ATTEMPT", {
            "command": command,
            "risk_level": risk
        }, agent=agent_name)

        if not skip_validation:
            if risk in [RiskLevel.HIGH, RiskLevel.CRITICAL]:
                if self._confirm_callback:
                    logger.warning(f"High-risk command requires UI confirmation: {command}")
                    # Invoke orchestrator confirmation request, which prompts QML
                    approved = await self._confirm_callback(risk, agent_name, "execute_command", "terminal", command)
                    if not approved:
                        logger.warning(f"Command execution denied by owner: {command}")
                        audit.log_event("COMMAND_EXECUTION_DENIED", {"command": command}, agent=agent_name)
                        return {
                            "stdout": "",
                            "stderr": "Execution blocked: User denied confirmation.",
                            "returncode": -1,
                            "risk": risk,
                            "blocked": True
                        }
                    logger.info(f"Command execution approved by owner: {command}")
                else:
                    logger.warning(f"Execution blocked for high-risk command (no UI callback): {command}")
                    return {
                        "stdout": "",
                        "stderr": "Execution blocked: High risk command requires explicit UI confirmation.",
                        "returncode": -1,
                        "risk": risk,
                        "blocked": True
                    }

        logger.info(f"Executing command via {agent_name}: {command}")
        try:
            # We use asyncio.create_subprocess_shell to run it without blocking
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # In a real restricted environment on Linux we'd use preexec_fn=os.setsid to kill process trees.
                # On Windows, we use standard subprocess creation.
            )

            try:
                # Enforce timeout
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.default_timeout)
                
                result = {
                    "stdout": stdout.decode(errors='replace'),
                    "stderr": stderr.decode(errors='replace'),
                    "returncode": process.returncode,
                    "risk": risk,
                    "blocked": False
                }
                
                audit.log_event("COMMAND_EXECUTION_SUCCESS", {
                    "command": command,
                    "returncode": process.returncode
                }, agent=agent_name)
                
                return result
                
            except asyncio.TimeoutError:
                logger.error(f"Command timed out after {self.default_timeout} seconds: {command}")
                process.kill()
                await process.wait()
                audit.log_event("COMMAND_EXECUTION_TIMEOUT", {"command": command}, agent=agent_name)
                return {
                    "stdout": "",
                    "stderr": f"Command timed out after {self.default_timeout} seconds.",
                    "returncode": -1,
                    "risk": risk,
                    "blocked": False
                }

        except Exception as e:
            logger.error(f"Failed to execute command: {e}")
            return {
                "stdout": "",
                "stderr": str(e),
                "returncode": -1,
                "risk": risk,
                "blocked": False
            }

# Global executor instance
executor = RestrictedExecutor()
