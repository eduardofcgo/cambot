import base64
import json

from cambot.camera import CameraManager, CameraCaptureError
from cambot.context import MemoryStore

TOOL_DEFINITIONS = [
    {
        "name": "capture_snapshot",
        "description": (
            "Capture a current JPEG snapshot from a specific security camera. "
            "Returns the snapshot image for visual analysis. The camera_name must "
            "match one from the configured cameras."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "camera_name": {
                    "type": "string",
                    "description": "The unique name identifier of the camera",
                },
            },
            "required": ["camera_name"],
        },
    },
    {
        "name": "capture_home_snapshots",
        "description": (
            "Capture snapshots from all cameras at a specific home/property. "
            "Use this when the user asks about a specific home like 'is the "
            "beach house okay?' or 'check the main house'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "home": {
                    "type": "string",
                    "description": "The home identifier, e.g. 'main_house', 'beach_house'",
                },
            },
            "required": ["home"],
        },
    },
    {
        "name": "capture_all_snapshots",
        "description": (
            "Capture snapshots from all enabled cameras across all homes. "
            "Use this for general questions like 'is everything alright everywhere?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "save_memory",
        "description": (
            "Append a piece of information to long-term memory. Use this to "
            "remember things the user tells you (schedules, people, habits) and "
            "to log brief observations during watch checks for future context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The information to remember",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "rewrite_memory",
        "description": (
            "Replace the entire memory with a new version. Use this when the "
            "user asks to update or edit memory, or when memory has grown long "
            "and needs consolidation. Preserves key facts and recent patterns "
            "while dropping redundant or outdated entries."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The full replacement memory content",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "clear_memory",
        "description": (
            "Erase all memory completely. Use this when the user explicitly "
            "asks to clear, reset, or wipe memory. Always confirm with the "
            "user before calling this."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "schedule_next_check",
        "description": (
            "Set when the next autonomous check should happen. Use this during "
            "autonomous watch checks to control the monitoring cadence. Consider "
            "time of day, current conditions, and what you know from memory. "
            "For example: check more frequently at night or when the house should "
            "be empty, less frequently during expected normal activity."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "minutes": {
                    "type": "integer",
                    "description": "Minutes until next check",
                },
                "reason": {
                    "type": "string",
                    "description": "Brief reason for this interval",
                },
            },
            "required": ["minutes", "reason"],
        },
    },
    {
        "name": "get_watcher_status",
        "description": (
            "Get the current status of the autonomous watcher — whether it's "
            "running, when it last checked, what it found, and when it will "
            "check next. Use this when the user asks about monitoring status, "
            "next check time, or last check results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "capture_location_snapshots",
        "description": (
            "Capture snapshots from cameras at a specific location, optionally "
            "filtered by home. Use when asked about a specific area like "
            "'is the backyard safe?' or 'check the beach house patio'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "The location name, e.g. 'backyard', 'garage', 'patio'",
                },
                "home": {
                    "type": "string",
                    "description": "Optional home to filter by. If omitted, checks all homes.",
                },
            },
            "required": ["location"],
        },
    },
]


def _make_image_content(label: str, jpeg_data: bytes) -> list[dict]:
    return [
        {"type": "text", "text": label},
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": base64.b64encode(jpeg_data).decode("ascii"),
            },
        },
    ]


def _build_snapshot_content(results: dict[str, bytes | str], camera_manager: CameraManager) -> list[dict] | str:
    content: list[dict] = []
    for name, data in results.items():
        cam = camera_manager.cameras[name]
        label = f"Snapshot from '{cam.display_name}' ({cam.home} / {cam.location}):"
        if isinstance(data, bytes):
            content.extend(_make_image_content(label, data))
        else:
            content.append({"type": "text", "text": f"{cam.display_name}: {data}"})
    return content if content else "No snapshots captured."


def execute_tool(
    tool_name: str,
    tool_input: dict,
    camera_manager: CameraManager,
    memory_store: MemoryStore,
    watcher=None,
) -> str | list[dict]:

    if tool_name == "get_watcher_status":
        if watcher is None:
            return "Autonomous monitoring is not enabled. Start with --watch to enable it."
        status = watcher.status()
        lines = []
        lines.append(f"Running: {status['running']}")
        if status["last_check_at"]:
            lines.append(f"Last check: {status['last_check_at']}")
        else:
            lines.append("Last check: not yet (first check pending)")
        if status["next_check_at"]:
            lines.append(f"Next check at: {status['next_check_at']}")
        lines.append(f"Current interval: {status['interval_seconds'] // 60} minutes")
        if status["last_schedule_reason"]:
            lines.append(f"Interval reason: {status['last_schedule_reason']}")
        if status["last_report"]:
            lines.append(f"Last report: {status['last_report']}")
        return "\n".join(lines)

    if tool_name == "schedule_next_check":
        minutes = tool_input["minutes"]
        reason = tool_input.get("reason", "")
        return f"Next check scheduled in {minutes} minutes. ({reason})"

    elif tool_name == "save_memory":
        memory_store.append(tool_input["content"])
        return f"Remembered: {tool_input['content']}"

    elif tool_name == "rewrite_memory":
        memory_store.rewrite(tool_input["content"])
        return "Memory rewritten with updated version."

    elif tool_name == "clear_memory":
        memory_store.clear()
        return "Memory cleared."

    elif tool_name == "capture_snapshot":
        name = tool_input["camera_name"]
        try:
            jpeg_data = camera_manager.capture_snapshot(name)
        except CameraCaptureError as e:
            return f"Failed to capture snapshot: {e}"
        cam = camera_manager.cameras[name]
        return _make_image_content(
            f"Snapshot from '{cam.display_name}' ({cam.home} / {cam.location}):",
            jpeg_data,
        )

    elif tool_name == "capture_home_snapshots":
        home = tool_input["home"]
        cams = camera_manager.get_cameras_by_home(home)
        if not cams:
            available = ", ".join(camera_manager.get_homes())
            return f"No cameras found for home '{home}'. Available homes: {available}"
        results = camera_manager.capture_multiple([c.name for c in cams])
        return _build_snapshot_content(results, camera_manager)

    elif tool_name == "capture_all_snapshots":
        enabled = [c.name for c in camera_manager.cameras.values() if c.enabled]
        if not enabled:
            return "No enabled cameras found."
        results = camera_manager.capture_multiple(enabled)
        return _build_snapshot_content(results, camera_manager)

    elif tool_name == "capture_location_snapshots":
        location = tool_input["location"]
        home = tool_input.get("home")
        cams = camera_manager.get_cameras_by_location(location, home=home)
        if not cams:
            all_locations = sorted(set(c.location for c in camera_manager.cameras.values()))
            return f"No cameras found at location '{location}'. Available locations: {', '.join(all_locations)}"
        results = camera_manager.capture_multiple([c.name for c in cams])
        return _build_snapshot_content(results, camera_manager)

    else:
        return f"Unknown tool: {tool_name}"
