import sys
from ui.engine import ui_engine
from core.orchestrator import PrimusOrchestrator
from loguru import logger
from dotenv import load_dotenv

def main():
    load_dotenv()
    try:
        # Initialize orchestrator and pass the ui_engine reference
        orchestrator = PrimusOrchestrator(ui_engine)
        # Start background asyncio services
        orchestrator.start()
        # Start UI engine on the main thread (blocks and runs the Qt event loop)
        ui_engine.start_ui()
    except Exception as e:
        logger.opt(exception=True).critical("Fatal error occurred during startup.")
        sys.exit(1)

if __name__ == "__main__":
    main()
