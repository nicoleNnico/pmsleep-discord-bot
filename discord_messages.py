import logging
import time

import requests


DISCORD_API_URL = "https://discord.com/api/v10"
logger = logging.getLogger(__name__)


def send_channel_message(token: str, channel_id: str, content: str) -> None:
    url = f"{DISCORD_API_URL}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}"}
    payload = {
        "content": content,
        "allowed_mentions": {"parse": []},
    }

    while True:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code != 429:
            response.raise_for_status()
            return

        try:
            retry_after = response.json().get("retry_after")
        except ValueError as error:
            raise RuntimeError("Discord 回傳無法解析的 rate limit 回應") from error
        if (
            not isinstance(retry_after, (int, float))
            or isinstance(retry_after, bool)
            or retry_after <= 0
        ):
            raise RuntimeError("Discord 回傳無效的 rate limit 等待時間")
        logger.warning("Discord API rate limit；等待 %.2f 秒後重試", retry_after)
        time.sleep(retry_after)
