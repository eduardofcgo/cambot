from pathlib import Path

import yaml

from cambot.camera import Camera, CameraManager, CameraCaptureError
from cambot.config import CONFIG_DIR

ASSETS_DIR = Path(__file__).parent.parent.parent / "assets" / "mock"
MOCK_CONFIG_PATH = CONFIG_DIR / "cameras.mock.yaml"


def load_mock_config() -> dict:
    with open(MOCK_CONFIG_PATH) as f:
        return yaml.safe_load(f)


class MockCameraManager(CameraManager):
    """Camera manager that loads from cameras.mock.yaml and returns static images from assets/."""

    def __init__(self):
        # Skip the parent __init__ — load mock config instead
        self.cameras: dict[str, Camera] = {}
        self._assets: dict[str, Path] = {}

        config = load_mock_config()
        self._settings = config.get("settings", {})

        for cam_cfg in config["cameras"]:
            cam = Camera(
                name=cam_cfg["name"],
                display_name=cam_cfg.get("display_name", cam_cfg["name"]),
                home=cam_cfg.get("home", "default"),
                location=cam_cfg.get("location", "unknown"),
                rtsp_url="mock://localhost",
                enabled=cam_cfg.get("enabled", True),
            )
            self.cameras[cam.name] = cam
            self._assets[cam.name] = ASSETS_DIR / cam_cfg["asset"]

    def capture_snapshot(self, camera_name: str, timeout: int | None = None) -> bytes:
        if camera_name not in self.cameras:
            raise CameraCaptureError(f"Unknown camera: {camera_name}")

        cam = self.cameras[camera_name]
        if not cam.enabled:
            raise CameraCaptureError(f"Camera '{camera_name}' is disabled")

        asset_path = self._assets[camera_name]
        if not asset_path.exists():
            raise CameraCaptureError(f"Mock asset not found: {asset_path}")

        return asset_path.read_bytes()
