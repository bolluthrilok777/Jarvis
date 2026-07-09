import datetime
import logging
import pathlib
import psutil
from PIL import ImageGrab
from jarvis_core.config import SCREENSHOT_DIR

logger = logging.getLogger("jarvis.system")


def get_system_status():
    cpu = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("C:/")

    return {
        "cpu_percent": cpu,
        "ram_used_gb": round(memory.used / (1024 ** 3), 2),
        "ram_total_gb": round(memory.total / (1024 ** 3), 2),
        "ram_percent": memory.percent,
        "disk_used_gb": round(disk.used / (1024 ** 3), 2),
        "disk_total_gb": round(disk.total / (1024 ** 3), 2),
        "disk_percent": disk.percent,
        "running_processes": len(psutil.pids())
    }


def take_screenshot():
    filename = datetime.datetime.now().strftime("screenshot_%Y%m%d_%H%M%S.png")
    file_path = SCREENSHOT_DIR / filename

    image = ImageGrab.grab()
    image.save(file_path)

    logger.info("Screenshot saved: %s", file_path)

    return str(file_path)
