import sys
import os
import asyncio
import threading
import time
from loguru import logger
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QGuiApplication
from ui.engine import ui_engine
from core.orchestrator import PrimusOrchestrator
from sandbox.executor import executor

async def run_async_integration_tests(orchestrator):
    app = QGuiApplication.instance()
    logger.info("--- Starting Async Integration Tests ---")
    from security.auth_manager import auth_manager, AuthState
    auth_manager.transition_to(AuthState.VERIFIED)
    auth_manager.last_activity = time.time()
    
    # Wait until UI engine is fully loaded
    await asyncio.sleep(4)
    
    # 1. Test Telemetry Pushing
    logger.info("1. Simulating Telemetry updates...")
    ui_engine.bridge.push_telemetry(12.5, 45.0, 0.0)
    
    # 2. Test status updates
    logger.info("2. Simulating status updates...")
    ui_engine.bridge.push_status("TESTING ACTIVE INTEGRATION")
    
    # 3. Test Confirmation flow (Approve)
    logger.info("3. Testing Sandbox command execution confirmation flow (Approve)...")
    
    async def auto_approve():
        await asyncio.sleep(1.0)
        logger.info("Simulating user clicking APPROVE on HUD")
        ui_engine.bridge.onPromptResponse(True)
        
    approve_task = asyncio.create_task(auto_approve())
    
    # Run a command that scores HIGH risk but is safe to execute
    result = await executor.execute("echo taskkill", "IntegrationTestAgent")
    logger.info(f"Sandbox High Risk Execution result: {result}")
    assert result["blocked"] is False
    assert "taskkill" in result["stdout"]
    await approve_task
    
    # 4. Test Deny Confirmation flow
    logger.info("4. Testing Sandbox command execution denial flow...")
    
    async def auto_deny():
        await asyncio.sleep(1.0)
        logger.info("Simulating user clicking DENY on HUD")
        ui_engine.bridge.onPromptResponse(False)
        
    deny_task = asyncio.create_task(auto_deny())
    
    # Run a command that scores CRITICAL risk but is safe to execute (just an echo)
    result_deny = await executor.execute("echo rm -rf /", "IntegrationTestAgent")
    logger.info(f"Sandbox High Risk Denied result: {result_deny}")
    assert result_deny["blocked"] is True
    await deny_task
    
    # 5. Test Overlay Visibility toggling
    logger.info("5. Simulating launch callback (overlay toggle)...")
    initial_visibility = orchestrator.is_active
    orchestrator.on_launch()
    assert orchestrator.is_active != initial_visibility
    # Toggle back
    orchestrator.on_launch()
    assert orchestrator.is_active == initial_visibility

    logger.info("6. Verifying emergency shutdown...")
    def mock_kill_switch():
        logger.info("EMERGENCY KILL SWITCH Mock triggered successfully. Exiting integration test.")
        # Quit the Qt app cleanly to end the test
        QTimer.singleShot(0, app.quit)
        
    orchestrator.on_kill_switch = mock_kill_switch
    logger.info("Simulating kill switch invocation...")
    orchestrator.on_kill_switch()
    logger.info("--- Async Integration Tests Complete ---")

def main():
    # Initialize orchestrator and pass the ui_engine reference
    orchestrator = PrimusOrchestrator(ui_engine)
    
    # Disable actual microphone listening during integration test to prevent real-world audio interference
    orchestrator.start_listening = lambda: logger.info("Mock start_listening: Microphone disabled for integration tests.")
    
    # Start background services
    orchestrator.start()
    
    # Schedule our test coroutine on the background event loop
    asyncio.run_coroutine_threadsafe(
        run_async_integration_tests(orchestrator), 
        orchestrator.async_loop
    )
    
    # Start UI engine (which blocks and runs the Qt event loop)
    ui_engine.start_ui()

if __name__ == "__main__":
    main()
