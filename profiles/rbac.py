from typing import List
from loguru import logger
from audit.trail import audit

class UnauthorizedExecutionException(Exception):
    pass

class PermissionManifest:
    def __init__(self, agent_name: str, allowed_permissions: List[str]):
        self.agent_name = agent_name
        self.allowed_permissions = allowed_permissions

    def has_permission(self, permission: str) -> bool:
        return permission in self.allowed_permissions

class RBACManager:
    """
    Manages Role-Based Access Control for AI Agents.
    """
    def __init__(self):
        self.manifests = {}
        self._initialize_default_roles()

    def _initialize_default_roles(self):
        # Coding Agent
        self.register_agent(PermissionManifest(
            "CodingAgent",
            ["read_workspace", "write_workspace", "run_isolated_code"]
        ))
        
        # Browser Agent
        self.register_agent(PermissionManifest(
            "BrowserAgent",
            ["web_browse", "web_search", "read_dom"]
        ))
        
        # Security Agent
        self.register_agent(PermissionManifest(
            "SecurityAgent",
            ["monitor_processes", "read_logs", "escalate_privileges"]
        ))
        
        # Core Primus
        self.register_agent(PermissionManifest(
            "CorePrimus",
            ["read_workspace", "read_logs", "manage_agents", "read_telemetry"]
        ))

        # Keyboard Agent (console text command entry)
        self.register_agent(PermissionManifest(
            "KeyboardAgent",
            ["read_workspace", "run_isolated_code"]
        ))

        # Integration Test Agent (testing only — restricted in production)
        self.register_agent(PermissionManifest(
            "IntegrationTestAgent",
            ["read_workspace", "run_isolated_code"]
        ))

    def register_agent(self, manifest: PermissionManifest):
        self.manifests[manifest.agent_name] = manifest
        logger.debug(f"Registered RBAC manifest for {manifest.agent_name}")

    def validate_permission(self, agent_name: str, permission: str):
        """
        Validates if an agent has the requested permission.
        Raises UnauthorizedExecutionException if denied.
        """
        if agent_name not in self.manifests:
            audit.log_event("RBAC_DENIED", {"agent": agent_name, "permission": permission, "reason": "Unknown agent"})
            logger.error(f"RBAC Denial: Unknown agent '{agent_name}' requested '{permission}'")
            raise UnauthorizedExecutionException(f"Agent {agent_name} is not registered in RBAC.")

        manifest = self.manifests[agent_name]
        if not manifest.has_permission(permission):
            audit.log_event("RBAC_DENIED", {"agent": agent_name, "permission": permission, "reason": "Missing permission in manifest"})
            logger.error(f"RBAC Denial: '{agent_name}' denied access to '{permission}'")
            raise UnauthorizedExecutionException(f"Agent {agent_name} lacks permission: {permission}")

        audit.log_event("RBAC_GRANTED", {"agent": agent_name, "permission": permission})
        logger.debug(f"RBAC Granted: '{agent_name}' -> '{permission}'")

# Global instance
rbac = RBACManager()
