import logging
from datetime import datetime, timedelta, timezone

import requests


DISCORD_API_URL = "https://discord.com/api/v10"
DISCORD_EPOCH_MILLISECONDS = 1420070400000  # Discord Snowflake ID 的時間基準：1420070400000 代表 2015-01-01 00:00:00 UTC 的毫秒時間戳。
MESSAGE_BULK_DELETE_MAX_AGE = timedelta(days=14)
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


def send_interaction_followup_message(
    application_id: str,
    interaction_token: str,
    content: str,
) -> None:
    url = (
        f"{DISCORD_API_URL}/webhooks/{application_id}/"
        f"{interaction_token}"
    )
    response = requests.post(
        url,
        json={
            "content": content,
            "allowed_mentions": {"parse": []},
        },
        timeout=15,
    )
    if response.status_code == 429:
        _log_rate_limit_response(response, "send_interaction_followup_message")
    response.raise_for_status()


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
