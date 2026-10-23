import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import time

import requests
from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_DIR / "assets" / "scheduled_messages.json"
STATE_PATH = PROJECT_DIR / ".scheduled_messages_state.json"
MESSAGE_INTERVAL_SECONDS = 5
DISCORD_API_URL = "https://discord.com/api/v10"
logger = logging.getLogger(__name__)

load_dotenv(PROJECT_DIR / ".env")


@dataclass(frozen=True)
class ScheduleConfig:
    interval_days: int
    channel_id: str
    messages: tuple[str, ...]


@dataclass(frozen=True)
class ScheduleState:
    next_send_at: datetime
    message_index: int
    config_fingerprint: str


def _load_config() -> ScheduleConfig:
    with CONFIG_PATH.open(encoding="utf-8") as config_file:
        raw_config = json.load(config_file)

    if not isinstance(raw_config, dict):
        raise ValueError(f"定時訊息設定必須是 JSON object：{CONFIG_PATH}")

    interval_days = raw_config.get("interval_days")
    if (
        not isinstance(interval_days, int)
        or isinstance(interval_days, bool)
        or interval_days <= 0
    ):
        raise ValueError("interval_days 必須是正整數")

    channel_id = raw_config.get("channel_id")
    if not isinstance(channel_id, str) or not channel_id.isdecimal():
        raise ValueError("channel_id 必須是 Discord 頻道 ID 數字字串")

    messages = raw_config.get("messages")
    if (
        not isinstance(messages, list)
        or not messages
        or any(
            not isinstance(message, str)
            or not message.strip()
            or len(message) > 2000
            for message in messages
        )
    ):
        raise ValueError("messages 必須是非空字串清單，且每則訊息不可超過 2000 字")

    return ScheduleConfig(interval_days, channel_id, tuple(messages))


def _config_fingerprint(config: ScheduleConfig) -> str:
    serialized = json.dumps(
        {
            "interval_days": config.interval_days,
            "channel_id": config.channel_id,
            "messages": config.messages,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _save_state(state: ScheduleState) -> None:
    serialized = json.dumps(
        {
            "next_send_at": state.next_send_at.isoformat(),
            "message_index": state.message_index,
            "config_fingerprint": state.config_fingerprint,
        },
        ensure_ascii=False,
        indent=2,
    )
    temporary_path = STATE_PATH.with_suffix(".tmp")
    temporary_path.write_text(serialized, encoding="utf-8")
    os.replace(temporary_path, STATE_PATH)


def _load_state(config: ScheduleConfig) -> ScheduleState:
    fingerprint = _config_fingerprint(config)
    if not STATE_PATH.exists():
        state = ScheduleState(datetime.now(timezone.utc), 0, fingerprint)
        _save_state(state)
        return state

    with STATE_PATH.open(encoding="utf-8") as state_file:
        raw_state = json.load(state_file)
    if not isinstance(raw_state, dict):
        raise ValueError(f"定時訊息狀態必須是 JSON object：{STATE_PATH}")

    if raw_state.get("config_fingerprint") != fingerprint:
        state = ScheduleState(datetime.now(timezone.utc), 0, fingerprint)
        _save_state(state)
        logger.info("定時訊息設定已變更；將立即以新設定開始")
        return state

    try:
        next_send_at = datetime.fromisoformat(raw_state["next_send_at"])
        message_index = raw_state["message_index"]
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"定時訊息狀態格式錯誤：{STATE_PATH}") from error

    if next_send_at.tzinfo is None:
        raise ValueError(f"定時訊息狀態時間必須包含時區：{STATE_PATH}")
    if (
        not isinstance(message_index, int)
        or isinstance(message_index, bool)
        or not 0 <= message_index < len(config.messages)
    ):
        raise ValueError(f"定時訊息狀態索引無效：{STATE_PATH}")

    return ScheduleState(next_send_at, message_index, fingerprint)


def _send_message(token: str, channel_id: str, content: str) -> None:
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


async def run_scheduled_messages() -> None:
    config = _load_config()
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("定時訊息功能需要設定 DISCORD_BOT_TOKEN")

    state = _load_state(config)
    logger.info(
        "定時訊息排程啟動；channel_id=%s interval_days=%s message_count=%s",
        config.channel_id,
        config.interval_days,
        len(config.messages),
    )

    while True:
        wait_seconds = (state.next_send_at - datetime.now(timezone.utc)).total_seconds()
        if wait_seconds > 0:
            await asyncio.sleep(wait_seconds)

        message = config.messages[state.message_index]
        try:
            await asyncio.to_thread(
                _send_message,
                token,
                config.channel_id,
                message,
            )
        except (requests.ConnectionError, requests.Timeout) as error:
            logger.warning(
                "Discord 訊息發送連線失敗；60 秒後重試；error=%s",
                error,
                exc_info=(type(error), error, error.__traceback__),
            )
            await asyncio.sleep(60)
            continue
        except requests.HTTPError as error:
            response = error.response
            if response is None or response.status_code < 500:
                raise
            logger.warning(
                "Discord API 暫時錯誤；status=%s；60 秒後重試",
                response.status_code,
            )
            await asyncio.sleep(60)
            continue

        logger.info(
            "定時訊息已送出；message=%s/%s",
            state.message_index + 1,
            len(config.messages),
        )

        completed_at = datetime.now(timezone.utc)
        if state.message_index + 1 < len(config.messages):
            state = ScheduleState(
                completed_at + timedelta(seconds=MESSAGE_INTERVAL_SECONDS),
                state.message_index + 1,
                state.config_fingerprint,
            )
        else:
            state = ScheduleState(
                completed_at + timedelta(days=config.interval_days),
                0,
                state.config_fingerprint,
            )
        _save_state(state)
