import threading
from datetime import datetime, timezone

import anthropic

from cambot.camera import CameraManager
from cambot.context import MemoryStore
from cambot.tools import TOOL_DEFINITIONS, execute_tool

SYSTEM_PROMPT_TEMPLATE = """\
You are a security monitoring assistant. The user has cameras across multiple \
homes/properties. You can capture live snapshots and analyze what is happening.

HOMES AND CAMERAS:
{home_camera_context}
{memories_section}
BEHAVIOR:
- When asked about a specific home ("is the beach house okay?", "check the main \
house"), use capture_home_snapshots with the matching home identifier.
- When asked about general safety across everything ("is everything alright?"), \
use capture_all_snapshots.
- When asked about a specific location at a home ("is the beach house patio safe?"), \
use capture_location_snapshots with the location and home.
- Focus on security-relevant observations: presence of people, vehicles, open \
doors/windows, environmental hazards, anomalies compared to expected activity.
- Respect privacy: do not describe personal activities, private moments, or \
details that aren't security-relevant. If someone recognized is home doing \
normal things, just say they're there — don't narrate what they're doing. \
Only describe behavior in detail when it's relevant to security.
- If something matches an alert condition, clearly flag it.
- If everything looks normal, say so briefly.
- Be honest about image quality limitations (dark, blurry, etc.).
- You cannot do facial recognition, but you CAN match people against known \
descriptions stored in memory (height, build, hair, clothing style, etc.). \
If someone matches a known person's description, say so. If no one in memory \
matches, flag them as an unrecognized person.
- When the user describes household members, regular visitors, or service people, \
save detailed physical descriptions to memory so you can recognize them later.
- Capture fresh snapshots for every question. Never rely on previous images.
- When the user tells you something worth remembering for future sessions \
(schedules, people, habits, temporary situations), use save_memory to store it.
- If the memory is getting long, use rewrite_memory to compress it into a shorter \
version that keeps key facts and recent patterns.
- The user can ask to see, edit, clear, or improve the memory. Use rewrite_memory \
to update it or clear_memory to erase it. Always confirm before clearing.
- During autonomous watch checks, use schedule_next_check to set when to check \
again. Consider time of day, conditions, and memories to decide the interval. \
When something needs follow-up on specific cameras, include focus_cameras so the \
next check only recaptures those — don't re-check all cameras if only one needs \
attention. Omit focus_cameras when you want to return to a full sweep.
- When the user asks about monitoring status, next check time, or what the last \
check found, use get_watcher_status to get the current state of the autonomous \
watcher.
- When you detect something the user should see (anomalies, alerts, unfamiliar \
people), use send_photo to send the relevant camera snapshot to the user.
- When the user asks to see a camera or says "show me", use send_photo after \
capturing and analyzing.
- During autonomous watch checks, use send_photo for any alert-worthy observations \
so the user can see what triggered the alert.\
"""


class SecurityAgent:
    def __init__(
        self,
        camera_manager: CameraManager,
        config: dict,
        model: str = "claude-sonnet-4-5-20250929",
        language: str | None = None,
        locale: str | None = None,
    ):
        self.client = anthropic.Anthropic()
        self.camera_manager = camera_manager
        self.memory_store = MemoryStore()
        self.config = config
        self.model = model
        self.language = language
        self.locale = locale
        self.messages: list[dict] = []
        self._lock = threading.Lock()
        self.watcher = None  # set externally when watcher is enabled
        self._pending_photos: list[tuple[bytes, str]] = []

    def _get_system_prompt(self) -> str:
        homes_cfg = self.config.get("homes", {})
        cameras_cfg = self.config.get("cameras", [])

        # Group cameras by home
        by_home: dict[str, list[dict]] = {}
        for cam in cameras_cfg:
            by_home.setdefault(cam.get("home", "default"), []).append(cam)

        lines = []
        for home_id, cams in by_home.items():
            home_info = homes_cfg.get(home_id, {})
            home_label = home_id
            if home_info.get("description"):
                home_label += f" - {home_info['description']}"
            lines.append(f"\n## {home_label}")
            for cam in cams:
                cam_info = f"  - {cam['display_name']} ({cam['name']}): {cam.get('description', cam['location'])}"
                if cam.get("typical_activity"):
                    cam_info += f"\n    Normal: {cam['typical_activity']}"
                if cam.get("alert_conditions"):
                    cam_info += f"\n    Alert: {cam['alert_conditions']}"
                lines.append(cam_info)

        # Inject memories if any exist
        memory_text = self.memory_store.read()
        memories_section = ""
        if memory_text:
            memories_section = f"\nTHINGS THE USER HAS TOLD YOU:\n{memory_text}\n"

        prompt = SYSTEM_PROMPT_TEMPLATE.format(
            home_camera_context="\n".join(lines),
            memories_section=memories_section,
        )

        if self.language:
            lang_instruction = f"\nLANGUAGE: Always respond in {self.language}."
            if self.locale:
                lang_instruction += f" Use locale {self.locale} for dates and times."
            prompt += lang_instruction

        return prompt

    def _run_turn(self) -> tuple[str, int | None, str | None, list[str] | None]:
        """Run the agent loop until it stops.
        Returns (text, scheduled_minutes, schedule_reason, focus_cameras)."""
        scheduled_minutes = None
        schedule_reason = None
        focus_cameras = None

        while True:
            response = self.client.messages.create(
                model=self.model,
                system=self._get_system_prompt(),
                tools=TOOL_DEFINITIONS,
                messages=self.messages,
                max_tokens=4096,
            )

            self.messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                return self._extract_text(response.content), scheduled_minutes, schedule_reason, focus_cameras

            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    if block.name == "schedule_next_check":
                        scheduled_minutes = block.input.get("minutes")
                        schedule_reason = block.input.get("reason")
                        focus_cameras = block.input.get("focus_cameras")
                    result = execute_tool(
                        block.name,
                        block.input,
                        self.camera_manager,
                        self.memory_store,
                        watcher=self.watcher,
                        photo_queue=self._pending_photos,
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })

            self.messages.append({"role": "user", "content": tool_results})

    def pop_pending_photos(self) -> list[tuple[bytes, str]]:
        """Retrieve and clear any photos queued during the last turn."""
        photos = self._pending_photos[:]
        self._pending_photos.clear()
        return photos

    def chat(self, user_message: str) -> str:
        with self._lock:
            self._pending_photos.clear()
            self.messages.append({"role": "user", "content": user_message})
            text, _, _, _ = self._run_turn()
            return text

    def watch(self, focus_cameras: list[str] | None = None) -> tuple[str, int | None, str | None, list[str] | None]:
        """Autonomous watch check. Shares conversation history.
        Returns (report_text, next_check_minutes, schedule_reason, focus_cameras)."""
        now = datetime.now(timezone.utc).strftime("%A, %Y-%m-%d %H:%M:%S UTC")
        self._pending_photos.clear()

        if focus_cameras:
            camera_instruction = (
                f"This is a follow-up check. Focus on these cameras: {', '.join(focus_cameras)}. "
                "Use capture_snapshot for each one — don't capture all cameras. "
                "If the situation has resolved, you can return to checking all cameras next time "
                "by omitting focus_cameras from schedule_next_check."
            )
        else:
            camera_instruction = (
                "Check all cameras now using capture_all_snapshots."
            )

        prompt = (
            f"[Autonomous watch check at {now}]\n"
            f"{camera_instruction} "
            "Analyze what you see against the time of day, "
            "configured alert conditions, and what you know from memory.\n"
            "- If something needs the user's attention, describe it clearly.\n"
            "- If everything looks normal and unremarkable, respond with just: WATCH_OK\n"
            "- Respect privacy: only report security-relevant details. Don't describe \n"
            "what recognized people are doing unless it's a security concern.\n"
            "- Use schedule_next_check to set when to check again:\n"
            "  * If you detect something unusual or matching an alert condition, "
            "schedule the next check in 1-2 minutes to follow up and include "
            "focus_cameras with just the cameras that need attention.\n"
            "  * At night or when the house should be empty, check every 3-5 min.\n"
            "  * During calm, expected activity, check every 10-15 min.\n"
            "- Use save_memory to log a brief observation (e.g. 'Watch 14:30: all clear, "
            "1 car in driveway'). This gives you context for future checks.\n"
            "- If the memory section above is getting long, use rewrite_memory to "
            "compress it: keep key facts and recent patterns, drop redundant entries."
        )
        with self._lock:
            self.messages.append({"role": "user", "content": prompt})
            return self._run_turn()

    def _extract_text(self, content: list) -> str:
        parts = []
        for block in content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts)
