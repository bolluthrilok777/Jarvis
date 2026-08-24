import datetime
import logging
import os
import subprocess
import threading
import psutil
from PIL import ImageGrab
from jarvis_core.config import SCREENSHOT_DIR

logger = logging.getLogger("jarvis.system")


# The live monitor has no background sampling thread: metrics are collected only
# while the browser panel is explicitly open. The flag also rejects requests
# from a stale browser timer after the user closes the panel.
_live_monitor_lock = threading.Lock()
_live_monitor_active = False


def start_live_monitor() -> None:
    """Allow the live-monitor endpoint to collect metrics."""
    global _live_monitor_active
    with _live_monitor_lock:
        _live_monitor_active = True


def stop_live_monitor() -> None:
    """Disable the live-monitor endpoint; no background monitor remains."""
    global _live_monitor_active
    with _live_monitor_lock:
        _live_monitor_active = False


def is_live_monitor_active() -> bool:
    with _live_monitor_lock:
        return _live_monitor_active


# ── CPU Temperature ───────────────────────────────────────────────────────────

def _try_thermal_perf_counter() -> float | None:
    """Read CPU temperature via Windows Thermal Zone Performance Counters.

    Uses 'Thermal Zone Information\\Temperature' which is accessible
    WITHOUT admin on all Windows versions.
    Windows returns values in plain Kelvin (e.g. 364 = 90.85°C).
    We filter zones > 250 K (above -23°C) to exclude invalid/zero readings,
    then pick the hottest zone — which is the CPU core temperature.
    """
    try:
        ps = (
            "$s=(Get-Counter '\\Thermal Zone Information(*)\\Temperature' -EA Stop)"
            ".CounterSamples;"
            "$vals=$s|Where-Object{$_.CookedValue -gt 250}|"
            "ForEach-Object{[math]::Round($_.CookedValue-273.15,1)};"
            "if($vals){($vals|Measure-Object -Maximum).Maximum}else{'N/A'}"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=5,
        )
        raw = r.stdout.strip()
        val = float(raw)
        if 0 < val < 120:
            return val
    except Exception:
        pass
    return None

def _try_acpi_wmi() -> float | None:
    """Standard ACPI WMI thermal zone — works on most non-Lenovo machines."""
    try:
        import wmi as _wmi
        w = _wmi.WMI(namespace="root/wmi")
        sensors = w.MSAcpi_ThermalZoneTemperature()
        best = None
        for s in sensors:
            raw = s.CurrentTemperature
            if raw:
                c = raw / 10.0 - 273.15
                if 0 < c < 120:
                    if best is None or c > best:
                        best = round(c, 1)
        return best
    except Exception:
        return None


def _try_lenovo_wmi() -> float | None:
    """Lenovo GameZone WMI — available on Legion / IdeaPad when server runs as admin."""
    try:
        import wmi as _wmi
        w = _wmi.WMI(namespace="root/wmi")
        objs = w.LENOVO_GAMEZONE_DATA()
        if objs:
            result = objs[0].GetCPUTemp()
            # result is a tuple; first element is the temp in Celsius (integer)
            temp = result[0] if result else None
            if temp and 0 < temp < 120:
                return float(temp)
    except Exception:
        pass
    return None


def _try_ohm_wmi() -> float | None:
    """OpenHardwareMonitor / LibreHardwareMonitor WMI interface (if running)."""
    for ns in ("root/OpenHardwareMonitor", "root/LibreHardwareMonitor"):
        try:
            import wmi as _wmi
            w = _wmi.WMI(namespace=ns)
            sensors = w.Sensor()
            cpu_temps = [
                float(s.Value) for s in sensors
                if getattr(s, "SensorType", "") == "Temperature"
                and "cpu" in getattr(s, "Name", "").lower()
                and 0 < float(s.Value) < 120
            ]
            if cpu_temps:
                return round(max(cpu_temps), 1)
        except Exception:
            continue
    return None


def _try_cim_ps() -> float | None:
    """PowerShell CIM query with a hard 4-second timeout — last resort."""
    try:
        ps = (
            "try {"
            "  $r = Get-CimInstance -Namespace root/wmi "
            "    -ClassName MSAcpi_ThermalZoneTemperature -EA Stop;"
            "  if ($r) { [math]::Round($r[0].CurrentTemperature/10.0-273.15,1) }"
            "  else { 'N/A' }"
            "} catch { 'N/A' }"
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=4,
        )
        raw = result.stdout.strip()
        val = float(raw)
        if 0 < val < 120:
            return val
    except Exception:
        pass
    return None


def _get_cpu_temperature() -> float | None:
    """Try multiple temperature sources in order of reliability.

    1. Standard ACPI WMI  (most desktops / non-Lenovo laptops)
    2. Lenovo GameZone WMI (Lenovo laptops — needs server running as admin)
    3. OpenHardwareMonitor / LibreHardwareMonitor WMI (if the app is running)
    4. PowerShell CIM fallback (4-second timeout hard cap)
    """
    for fn in (_try_thermal_perf_counter, _try_acpi_wmi, _try_lenovo_wmi, _try_ohm_wmi, _try_cim_ps):
        try:
            val = fn()
            if val is not None:
                return val
        except Exception:
            continue
    return None




# ── GPU Metrics ───────────────────────────────────────────────────────────────

def _get_gpu_name() -> str:
    """Return the GPU name from WMI Win32_VideoController."""
    try:
        import wmi as _wmi
        gpus = _wmi.WMI().Win32_VideoController()
        if gpus:
            return gpus[0].Name or "GPU"
    except Exception:
        pass
    return "GPU"


def _get_gpu_util_dxgi() -> float | None:
    """Read GPU 3D-engine utilisation via Windows DXGI performance counters.

    Works on AMD, Intel, and NVIDIA integrated/discrete GPUs without admin.
    This is the exact same data source Windows Task Manager uses.
    """
    try:
        ps = (
            "try {"
            "  $c = (Get-Counter '\\GPU Engine(*engtype_3D)\\Utilization Percentage' "
            "    -EA Stop).CounterSamples;"
            "  [math]::Round([math]::Min(($c | Measure-Object -Property CookedValue -Sum).Sum, 100), 1)"
            "} catch { echo 'N/A' }"
        )
        r = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=5,
        )
        raw = r.stdout.strip()
        val = float(raw)
        if 0 <= val <= 100:
            return round(val, 1)
    except Exception:
        pass
    return None


def _get_gpu_metrics_nvidia() -> dict | None:
    """Try NVIDIA NVML (pynvml) for detailed GPU data — temp, VRAM, utilization."""
    try:
        from pynvml import (
            nvmlInit, nvmlDeviceGetHandleByIndex,
            nvmlDeviceGetUtilizationRates, nvmlDeviceGetTemperature,
            nvmlDeviceGetMemoryInfo, nvmlDeviceGetName, NVML_TEMPERATURE_GPU,
        )
        nvmlInit()
        h = nvmlDeviceGetHandleByIndex(0)
        util = nvmlDeviceGetUtilizationRates(h)
        temp = nvmlDeviceGetTemperature(h, NVML_TEMPERATURE_GPU)
        mem  = nvmlDeviceGetMemoryInfo(h)
        name = nvmlDeviceGetName(h)
        return {
            "gpu_name":     name,
            "gpu_percent":  float(util.gpu),
            "gpu_temp_c":   float(temp),
            "vram_used_gb": round(mem.used  / 1024 ** 3, 2),
            "vram_total_gb": round(mem.total / 1024 ** 3, 2),
        }
    except Exception:
        return None


def _get_gpu_metrics() -> dict | None:
    """Return GPU metrics. Tries NVIDIA (detailed) then DXGI (universal)."""
    # 1. Try NVIDIA NVML — gives temp + VRAM too
    nvidia = _get_gpu_metrics_nvidia()
    if nvidia:
        return nvidia

    # 2. DXGI counter — works on AMD / Intel iGPU without admin
    util = _get_gpu_util_dxgi()
    if util is not None:
        return {
            "gpu_name":    _get_gpu_name(),
            "gpu_percent": util,
            # Temperature not available without LHM/admin on AMD/Intel
            "gpu_temp_c":  None,
        }
    return None


# ── Task Manager ──────────────────────────────────────────────────────────────

def _launch_task_manager() -> None:
    """Fire-and-forget Task Manager launch using os.startfile."""
    try:
        os.startfile("taskmgr.exe")
        logger.info("Task Manager launched via os.startfile")
    except Exception as exc:
        # Fallback: full path
        try:
            subprocess.Popen(
                [r"C:\Windows\System32\taskmgr.exe"],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
            )
            logger.info("Task Manager launched via full-path Popen")
        except Exception as exc2:
            logger.warning("Could not open Task Manager: %s | %s", exc, exc2)


def open_task_manager() -> None:
    """Launch Task Manager in a background thread so it never blocks metrics."""
    t = threading.Thread(target=_launch_task_manager, daemon=True)
    t.start()


# ── Main status function ───────────────────────────────────────────────────────

def get_system_status(launch_task_manager: bool = False) -> dict:
    """Collect CPU, RAM, disk, temperature, and GPU metrics.

    Args:
        launch_task_manager: If True, also opens Windows Task Manager.

    Returns:
        A dict with all available metrics.
    """
    if launch_task_manager:
        open_task_manager()   # non-blocking — runs in daemon thread

    cpu = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("C:/")

    status: dict = {
        "cpu_percent":       cpu,
        "ram_used_gb":       round(memory.used  / (1024 ** 3), 2),
        "ram_total_gb":      round(memory.total / (1024 ** 3), 2),
        "ram_percent":       memory.percent,
        "disk_used_gb":      round(disk.used  / (1024 ** 3), 2),
        "disk_total_gb":     round(disk.total / (1024 ** 3), 2),
        "disk_percent":      disk.percent,
        "running_processes":  len(psutil.pids()),
    }

    cpu_temp = _get_cpu_temperature()
    status["cpu_temp_c"] = cpu_temp   # always present; None means sensor N/A

    gpu = _get_gpu_metrics()
    if gpu:
        status.update(gpu)

    if launch_task_manager:
        status["task_manager_opened"] = True

    return status


def take_screenshot():
    filename = datetime.datetime.now().strftime("screenshot_%Y%m%d_%H%M%S.png")
    file_path = SCREENSHOT_DIR / filename

    image = ImageGrab.grab()
    image.save(file_path)

    logger.info("Screenshot saved: %s", file_path)

    return str(file_path)
