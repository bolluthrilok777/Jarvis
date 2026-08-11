"""Quick diagnostic: test every temperature method available on this machine."""
import subprocess, json

results = {}

# Method 1: WMI MSAcpi_ThermalZoneTemperature (root/wmi)
try:
    import wmi
    w = wmi.WMI(namespace="root/wmi")
    sensors = w.MSAcpi_ThermalZoneTemperature()
    results["MSAcpi"] = [(s.InstanceName, round((s.CurrentTemperature / 10.0) - 273.15, 1)) for s in sensors]
except Exception as e:
    results["MSAcpi"] = f"ERROR: {e}"

# Method 2: WMI Win32_TemperatureProbe (root/cimv2)
try:
    import wmi
    w2 = wmi.WMI()
    probes = w2.Win32_TemperatureProbe()
    results["Win32_TemperatureProbe"] = [(p.Name, p.CurrentReading) for p in probes]
except Exception as e:
    results["Win32_TemperatureProbe"] = f"ERROR: {e}"

# Method 3: PowerShell Get-WmiObject
try:
    ps = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace root/wmi | Select InstanceName,CurrentTemperature | ConvertTo-Json"],
        capture_output=True, text=True, timeout=8
    )
    results["PS_MSAcpi"] = ps.stdout.strip() or ps.stderr.strip()
except Exception as e:
    results["PS_MSAcpi"] = f"ERROR: {e}"

# Method 4: nvidia-smi GPU temp
try:
    ns = subprocess.run(
        ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=5
    )
    results["nvidia_smi"] = ns.stdout.strip() if ns.returncode == 0 else f"rc={ns.returncode}"
except FileNotFoundError:
    results["nvidia_smi"] = "not found"
except Exception as e:
    results["nvidia_smi"] = f"ERROR: {e}"

# Method 5: taskmgr launch test
try:
    import os
    os.startfile("taskmgr.exe")
    results["taskmgr_startfile"] = "launched OK"
except Exception as e:
    results["taskmgr_startfile"] = f"ERROR: {e}"

print(json.dumps(results, indent=2, default=str))
