import logging
import math
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests


DISCORD_API_URL = "https://discord.com/api/v10"
DISCORD_EPOCH_MILLISECONDS = 1420070400000  # Discord Snowflake ID 的時間基準：1420070400000 代表 2015-01-01 00:00:00 UTC 的毫秒時間戳。
MESSAGE_BULK_DELETE_MAX_AGE = timedelta(days=14)
MAX_RATE_LIMIT_RETRIES = 1  # 設重試次數上限，避免無限重試造成 rate limit。
logger = logging.getLogger(__name__)


def _log_rate_limit_response(response: requests.Response, operation: str) -> None:
    try:
        response_body = response.json()
    except ValueError:
        response_body = None

    body_retry_after = (
        response_body.get("retry_after")
        if isinstance(response_body, dict)
        else None
    )
    body_global = (
        response_body.get("global")
        if isinstance(response_body, dict)
        else None
    )
    headers = response.headers
    logger.warning(
        "Discord API rate limit response；operation=%s status=%s "
        "retry_after_body=%r retry_after_header=%r "
        "global_body=%r global_header=%r scope=%r "
        "limit=%r remaining=%r reset_after=%r bucket=%r",
        operation,
        response.status_code,
        body_retry_after,
        headers.get("Retry-After"),
        body_global,
        headers.get("X-RateLimit-Global"),
        headers.get("X-RateLimit-Scope"),
        headers.get("X-RateLimit-Limit"),
        headers.get("X-RateLimit-Remaining"),
        headers.get("X-RateLimit-Reset-After"),
        headers.get("X-RateLimit-Bucket"),
    )


def send_channel_message(token: str, channel_id: str, content: str) -> None:
    url = f"{DISCORD_API_URL}/channels/{channel_id}/messages"
    headers = {"Authorization": f"Bot {token}"}
    payload = {
        "content": content,
        "allowed_mentions": {"parse": []},
    }

    retry_count = 0
    while True:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        if response.status_code != 429:
            response.raise_for_status()
            return
        _log_rate_limit_response(response, "send_channel_message")

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
            header_retry_after = _parse_retry_after_header(retry_after_header)

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

        if retry_count >= MAX_RATE_LIMIT_RETRIES:
            logger.error(
                "Discord API rate limit；已達最大重試次數=%s",
                MAX_RATE_LIMIT_RETRIES,
            )
            response.raise_for_status()

        retry_count += 1
        logger.warning(
            "Discord API rate limit；等待 %.2f 秒後重試；retry=%s/%s",
            retry_after,
            retry_count,
            MAX_RATE_LIMIT_RETRIES,
        )
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
    if response.status_code == 429:
        _log_rate_limit_response(response, "delete_original_interaction_response")
    if response.status_code == 404:
        logger.info("原始互動回覆已不存在；略過自動刪除")
        return
    response.raise_for_status()


def edit_original_interaction_response(
    application_id: str,
    interaction_token: str,
    content: str,
) -> None:
    url = (
        f"{DISCORD_API_URL}/webhooks/{application_id}/"
        f"{interaction_token}/messages/@original"
    )
    response = requests.patch(url, json={"content": content}, timeout=15)
    if response.status_code == 429:
        _log_rate_limit_response(response, "edit_original_interaction_response")
    response.raise_for_status()


class OldMessagesError(ValueError):
    pass


def clean_recent_messages(token: str, channel_id: str, count: int) -> int:
    if not 1 <= count <= 100:
        raise ValueError("清理數量必須介於 1 到 100。")

    headers = {"Authorization": f"Bot {token}"}
    messages_response = requests.get(
        f"{DISCORD_API_URL}/channels/{channel_id}/messages",
        headers=headers,
        params={"limit": count},
        timeout=15,
    )
    if messages_response.status_code == 429:
        _log_rate_limit_response(messages_response, "clean_get_messages")
    messages_response.raise_for_status()
    messages = messages_response.json()
    if not isinstance(messages, list):
        raise RuntimeError("Discord 回傳非預期的訊息清單。")
    if not messages:
        return 0

    message_ids = []
    cutoff = datetime.now(timezone.utc) - MESSAGE_BULK_DELETE_MAX_AGE
    for message in messages:
        message_id = message.get("id") if isinstance(message, dict) else None
        if not isinstance(message_id, str) or not message_id.isdecimal():
            raise RuntimeError("Discord 回傳含有無效的訊息 ID。")

        created_at = datetime.fromtimestamp(
            (int(message_id) >> 22) / 1000
            + DISCORD_EPOCH_MILLISECONDS / 1000,
            timezone.utc,
        )
        if created_at < cutoff:
            raise OldMessagesError(
                "最新訊息範圍內包含超過 14 天的訊息，為避免部分刪除，本次未刪除。"
            )
        message_ids.append(message_id)

    if len(message_ids) == 1:
        delete_response = requests.delete(
            f"{DISCORD_API_URL}/channels/{channel_id}/messages/{message_ids[0]}",
            headers=headers,
            timeout=15,
        )
    else:
        delete_response = requests.post(
            f"{DISCORD_API_URL}/channels/{channel_id}/messages/bulk-delete",
            headers=headers,
            json={"messages": message_ids},
            timeout=15,
        )
    if delete_response.status_code == 429:
        _log_rate_limit_response(delete_response, "clean_delete_messages")
    delete_response.raise_for_status()
    return len(message_ids)


def _is_valid_retry_after(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value > 0
    )


def _parse_retry_after_header(value: str | None) -> float | None:
    if not value:
        return None

    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None

        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()

    return seconds if _is_valid_retry_after(seconds) else None
