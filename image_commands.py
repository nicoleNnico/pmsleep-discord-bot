import json
from pathlib import Path
import time


PROJECT_DIR = Path(__file__).resolve().parent
IMAGE_URLS_CONFIG = PROJECT_DIR / "assets" / "image_urls.json"

with IMAGE_URLS_CONFIG.open(encoding="utf-8") as config_file:
    image_url_entries = json.load(config_file)

if not isinstance(image_url_entries, dict):
    raise ValueError(f"圖片網址清單必須是 JSON object：{IMAGE_URLS_CONFIG}")

IMAGE_URLS: dict[str, str] = {}
for keyword, image_url in image_url_entries.items():
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError("圖片清單中的關鍵字必須是非空字串")
    if not isinstance(image_url, str) or not image_url.startswith("https://"):
        raise ValueError(f"圖片網址必須是 HTTPS URL：{keyword!r}")
    IMAGE_URLS[keyword] = image_url

FOOD_CAROUSEL = ("沙拉", "咖哩", "甜點")
FOOD_CAROUSEL_INDEX = {
    keyword: index for index, keyword in enumerate(FOOD_CAROUSEL)
}
if any(keyword not in IMAGE_URLS for keyword in FOOD_CAROUSEL):
    raise ValueError("料理輪播需要 image_urls.json 包含：沙拉、咖哩、甜點")

COMMAND_COOLDOWN_SECONDS = 1.0
last_command_received_at = None


def is_command_rate_limited() -> bool:
    """Allow at most one slash command per second across the bot process."""
    global last_command_received_at
    now = time.monotonic()
    if (
        last_command_received_at is not None
        and now - last_command_received_at < COMMAND_COOLDOWN_SECONDS
    ):
        return True
    last_command_received_at = now
    return False


def build_food_carousel_embed(index: int) -> dict:
    keyword = FOOD_CAROUSEL[index]
    return {
        "title": keyword,
        "image": {"url": IMAGE_URLS[keyword]},
        "footer": {"text": f"{index + 1} / {len(FOOD_CAROUSEL)}"},
    }


def build_food_carousel_components(index: int) -> list:
    previous_index = (index - 1) % len(FOOD_CAROUSEL)
    next_index = (index + 1) % len(FOOD_CAROUSEL)
    return [{
        "type": 1,
        "components": [
            {
                "type": 2,
                "style": 2,
                "label": "上一張",
                "custom_id": f"food_carousel:{previous_index}",
            },
            {
                "type": 2,
                "style": 2,
                "label": "下一張",
                "custom_id": f"food_carousel:{next_index}",
            },
        ],
    }]
