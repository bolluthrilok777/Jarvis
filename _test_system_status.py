from jarvis_core.system_tools import get_system_status
import json
r = get_system_status(launch_task_manager=True)
print(json.dumps(r, indent=2))
