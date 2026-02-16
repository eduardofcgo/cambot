import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from cambot.config import load_cameras_config


class CameraCaptureError(Exception):
    pass


@dataclass
class Camera:
    name: str
    display_name: str
    home: str
    location: str
    rtsp_url: str | None = None
    sdp_file: str | None = None
    enabled: bool = True


class CameraManager:
    def __init__(self, config_path: Path | None = None):
        self.cameras: dict[str, Camera] = {}
        config = load_cameras_config(config_path)
        self._settings = config.get("settings", {})

        for cam_cfg in config["cameras"]:
            cam = Camera(
                name=cam_cfg["name"],
                display_name=cam_cfg.get("display_name", cam_cfg["name"]),
                home=cam_cfg.get("home", "default"),
                location=cam_cfg.get("location", "unknown"),
                rtsp_url=cam_cfg.get("rtsp_url"),
                sdp_file=cam_cfg.get("sdp_file"),
                enabled=cam_cfg.get("enabled", True),
            )
            self.cameras[cam.name] = cam

    def list_cameras(self) -> list[dict]:
        return [
            {
                "name": cam.name,
                "display_name": cam.display_name,
                "home": cam.home,
                "location": cam.location,
                "enabled": cam.enabled,
            }
            for cam in self.cameras.values()
        ]

    def get_homes(self) -> list[str]:
        return sorted(set(cam.home for cam in self.cameras.values()))

    def get_cameras_by_home(self, home: str) -> list[Camera]:
        return [
            cam
            for cam in self.cameras.values()
            if cam.home.lower() == home.lower() and cam.enabled
        ]

    def get_cameras_by_location(self, location: str, home: str | None = None) -> list[Camera]:
        results = []
        for cam in self.cameras.values():
            if not cam.enabled:
                continue
            if cam.location.lower() != location.lower():
                continue
            if home and cam.home.lower() != home.lower():
                continue
            results.append(cam)
        return results

    def capture_snapshot(self, camera_name: str, timeout: int | None = None) -> bytes:
        if camera_name not in self.cameras:
            raise CameraCaptureError(f"Unknown camera: {camera_name}")

        cam = self.cameras[camera_name]
        if not cam.enabled:
            raise CameraCaptureError(f"Camera '{camera_name}' is disabled")

        if timeout is None:
            timeout = self._settings.get("snapshot_timeout", 10)

        quality = str(self._settings.get("snapshot_quality", 2))

        cmd = ["ffmpeg", "-y"]

        if cam.sdp_file:
            cmd += ["-protocol_whitelist", "file,udp,srtp,rtp", "-i", cam.sdp_file]
        else:
            cmd += ["-rtsp_transport", "tcp", "-i", cam.rtsp_url]

        cmd += ["-frames:v", "1", "-q:v", quality, "-f", "image2", "pipe:1"]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise CameraCaptureError(
                f"Capture timed out after {timeout}s - camera '{cam.display_name}' may be offline"
            )

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            raise CameraCaptureError(
                f"ffmpeg failed for '{cam.display_name}' (exit {result.returncode}): {stderr[:300]}"
            )

        jpeg_data = result.stdout
        if len(jpeg_data) == 0:
            raise CameraCaptureError(f"ffmpeg produced empty output for '{cam.display_name}'")

        return jpeg_data

    def capture_multiple(self, camera_names: list[str], timeout: int | None = None) -> dict[str, bytes | str]:
        results: dict[str, bytes | str] = {}
        with ThreadPoolExecutor(max_workers=len(camera_names)) as executor:
            futures = {
                executor.submit(self.capture_snapshot, name, timeout): name
                for name in camera_names
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                except CameraCaptureError as e:
                    results[name] = f"Error: {e}"
        return results
