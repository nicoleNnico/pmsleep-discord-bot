import logging
import math
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
            response_body = response.json()
        except ValueError:
            response_body = None

        retry_after = (
            response_body.get("retry_after")
            if isinstance(response_body, dict)
            else None
        )
        if not _is_valid_retry_after(retry_after):
            retry_after_header = response.headers.get("Retry-After")
            try:
                header_retry_after = float(retry_after_header)
            except (TypeError, ValueError):
                header_retry_after = None

            if _is_valid_retry_after(header_retry_after):
                logger.warning(
                    "Discord 429 JSON 的 retry_after 無效；改用 Retry-After header"
                )
                retry_after = header_retry_after
            else:
                retry_after_type = type(retry_after).__name__
                raise RuntimeError(
                    "Discord 回傳無效的 rate limit 等待時間；"
                    f"status={response.status_code} "
                    f"body_retry_after_type={retry_after_type} "
                    f"retry_after_header_present={bool(retry_after_header)}"
                )

        logger.warning("Discord API rate limit；等待 %.2f 秒後重試", retry_after)
        time.sleep(retry_after)


def delete_original_interaction_response(
    application_id: str,
    interaction_token: str,
) -> None:
    url = (
        f"{DISCORD_API_URL}/webhooks/{application_id}/"
        f"{interaction_token}/messages/@original"
    )
    response = requests.delete(url, timeout=15)
    if response.status_code == 404:
        logger.info("原始互動回覆已不存在；略過自動刪除")
        return
    response.raise_for_status()


def _is_valid_retry_after(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )
