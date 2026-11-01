import keyboard
import threading
from loguru import logger
from config.settings import settings

class HotkeyManager:
    def __init__(self, launch_callback, kill_switch_callback):
        self.launch_callback = launch_callback
        self.kill_switch_callback = kill_switch_callback
        self.listener_thread = None
        self._running = False

    def _listen(self):
        logger.info(f"Registering Launch Hotkey: {settings.launch_hotkey}")
        keyboard.add_hotkey(settings.launch_hotkey, self._on_launch)
        
        logger.info(f"Registering Kill Switch Hotkey: {settings.kill_switch_hotkey}")
        keyboard.add_hotkey(settings.kill_switch_hotkey, self._on_kill_switch)
        
        self._running = True
        logger.info("Hotkey listener active.")
        keyboard.wait() # Block this thread until the process dies

    def _on_launch(self):
        logger.debug("Launch hotkey detected!")
        self.launch_callback()

    def _on_kill_switch(self):
        logger.critical("EMERGENCY KILL SWITCH ACTIVATED!")
        self.kill_switch_callback()

    def start(self):
        if self._running:
            return
        self.listener_thread = threading.Thread(target=self._listen, daemon=True)
        self.listener_thread.start()

    def stop(self):
        # Keyboard library handles unhooking on exit, but we can clear manually
        keyboard.unhook_all()
        self._running = False
