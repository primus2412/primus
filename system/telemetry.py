import psutil
import GPUtil
import time
import threading
from loguru import logger

class TelemetryMonitor:
    """
    Monitors system resources (CPU, RAM, GPU, Disk, Network).
    Designed to run in a background thread and feed data to the UI or Orchestrator.
    """
    def __init__(self, interval: int = 2):
        self.interval = interval
        self.stats = {
            "cpu_percent": 0.0,
            "ram_percent": 0.0,
            "gpu_percent": 0.0,
            "gpu_temp": 0.0,
            "disk_percent": 0.0,
            "network_sent_mb": 0.0,
            "network_recv_mb": 0.0
        }
        self._running = False
        self._gpu_enabled = True
        self._thread = None
        self._net_io_start = psutil.net_io_counters()

    def _update_stats(self):
        while self._running:
            try:
                # CPU & RAM
                self.stats["cpu_percent"] = psutil.cpu_percent(interval=None)
                self.stats["ram_percent"] = psutil.virtual_memory().percent
                self.stats["disk_percent"] = psutil.disk_usage('/').percent
                
                # Network
                net_io = psutil.net_io_counters()
                self.stats["network_sent_mb"] = (net_io.bytes_sent - self._net_io_start.bytes_sent) / (1024 * 1024)
                self.stats["network_recv_mb"] = (net_io.bytes_recv - self._net_io_start.bytes_recv) / (1024 * 1024)
                
                # GPU (if available)
                if self._gpu_enabled:
                    try:
                        gpus = GPUtil.getGPUs()
                        if gpus:
                            self.stats["gpu_percent"] = gpus[0].load * 100
                            self.stats["gpu_temp"] = gpus[0].temperature
                        else:
                            # No NVIDIA GPUs found. Disable future checks.
                            self._gpu_enabled = False
                    except Exception:
                        # nvidia-smi command not found or error. Disable future checks.
                        self._gpu_enabled = False
                
                # Proactive alerting
                self._check_thresholds()
                
            except Exception as e:
                logger.error(f"Error reading telemetry: {e}")
                
            time.sleep(self.interval)

    def _check_thresholds(self):
        if self.stats["cpu_percent"] > 90:
            logger.warning(f"High CPU Usage Detected: {self.stats['cpu_percent']}%")
        if self.stats["gpu_temp"] > 85:
            logger.warning(f"High GPU Temperature Detected: {self.stats['gpu_temp']}°C")

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._update_stats, daemon=True)
        self._thread.start()
        logger.info("System Telemetry Monitor started.")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            
    def get_stats(self) -> dict:
        return self.stats

# Global telemetry instance
telemetry = TelemetryMonitor()
