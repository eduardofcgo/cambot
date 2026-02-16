from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
CAMERAS_CONFIG_PATH = CONFIG_DIR / "cameras.yaml"


def load_cameras_config(config_path: Path | None = None) -> dict:
    config_path = config_path or CAMERAS_CONFIG_PATH
    if not config_path.exists():
        raise FileNotFoundError(
            f"Camera config not found at {config_path}\n"
            f"Copy config/cameras.yaml.example to config/cameras.yaml and fill in your camera details."
        )

    with open(config_path) as f:
        config = yaml.safe_load(f)

    if not config or "cameras" not in config:
        raise ValueError("cameras.yaml must contain a 'cameras' list")

    for i, cam in enumerate(config["cameras"]):
        if "name" not in cam:
            raise ValueError(f"Camera #{i} missing required field: name")

        has_rtsp = "rtsp_url" in cam
        has_sdp = "sdp_file" in cam

        if not has_rtsp and not has_sdp:
            raise ValueError(
                f"Camera #{i} ({cam.get('name', '?')}): must specify either 'rtsp_url' or 'sdp_file'"
            )
        if has_rtsp and has_sdp:
            raise ValueError(
                f"Camera #{i} ({cam.get('name', '?')}): specify either 'rtsp_url' or 'sdp_file', not both"
            )

    return config
