import asyncio
import json
from pathlib import Path
import random
import time

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from config import DISCORD_APP_ID, DISCORD_BOT_TOKEN, DISCORD_PUBLIC_KEY
from discord_security import verify_discord_signature
from discord_messages import (
    OldMessagesError,
    clean_recent_messages,
    delete_original_interaction_response,
    edit_original_interaction_response,
    send_interaction_followup_message,
)
from image_commands import (
    FOOD_CAROUSEL,
    FOOD_CAROUSEL_INDEX,
    IMAGE_URLS,
    build_food_carousel_components,
    build_food_carousel_embed,
    is_command_rate_limited,
)

router = APIRouter()
ECHO_CONFIRMATION_DELETE_DELAY_SECONDS = 3
pending_echo_cleanup_tasks: set[asyncio.Task[None]] = set()

WASTE_POSTS_PATH = (
    Path(__file__).resolve().parent / "assets" / "porygon_waste_posts.json"
)
with WASTE_POSTS_PATH.open(encoding="utf-8") as posts_file:
    waste_posts = json.load(posts_file)
if (
    not isinstance(waste_posts, list)
    or not waste_posts
    or any(not isinstance(post, str) or not post.strip() for post in waste_posts)
):
    raise ValueError(
        f"porygon_waste_posts.json 必須是非空字串清單：{WASTE_POSTS_PATH}"
    )


def log_step(interaction_id: str, message: str) -> None:
    print(f"[interaction_id={interaction_id}] {message}", flush=True)


def has_administrator_permission(interaction_data: dict) -> bool:
    member = interaction_data.get("member") or {}
    member_permissions = member.get("permissions")
    try:
        return (
            member_permissions is not None
            and (int(member_permissions) & 8) == 8
        )
    except (TypeError, ValueError):
        return False


async def execute_clean_messages(
    interaction_id: str,
    application_id: str,
    interaction_token: str,
    channel_id: str,
    count: int,
) -> None:
    try:
        deleted_count = await asyncio.to_thread(
            clean_recent_messages,
            DISCORD_BOT_TOKEN,
            channel_id,
            count,
        )
        result_message = f"✅ 已清理 {deleted_count} 則訊息。"
        log_step(
            interaction_id,
            f"/clean 清理完成；deleted_count={deleted_count}",
        )
    except OldMessagesError as error:
        result_message = f"❌ {error}"
        log_step(interaction_id, f"/clean 拒絕清理；reason={error}")
    except (requests.RequestException, RuntimeError, ValueError) as error:
        response = error.response if isinstance(error, requests.HTTPError) else None
        discord_code = None
        discord_message = None
        if response is not None:
            try:
                error_body = response.json()
            except ValueError:
                error_body = None
            if isinstance(error_body, dict):
                discord_code = error_body.get("code")
                discord_message = error_body.get("message")
        status = response.status_code if response is not None else "unknown"
        log_step(
            interaction_id,
            "/clean 清理失敗；"
            f"error_type={type(error).__name__} status={status} "
            f"discord_code={discord_code!r} "
            f"discord_message={discord_message!r}",
        )
        if response is not None and response.status_code == 403:
            result_message = "❌ Bot 缺少查看訊息歷史或管理訊息的權限。"
        elif response is not None and response.status_code == 429:
            result_message = "❌ Discord 暫時限制了 Bot 的 API 請求，請稍後再試。"
        else:
            result_message = "❌ 清理失敗，請檢查 Bot 權限或稍後再試。"

    try:
        await asyncio.to_thread(
            edit_original_interaction_response,
            application_id,
            interaction_token,
            result_message,
        )
    except requests.RequestException as error:
        response = error.response
        log_step(
            interaction_id,
            "/clean 結果回覆更新失敗；"
            f"error_type={type(error).__name__} "
            f"status={response.status_code if response is not None else 'unknown'}",
        )


async def delete_echo_confirmation(
    interaction_id: str,
    application_id: str,
    interaction_token: str,
) -> None:
    await asyncio.sleep(ECHO_CONFIRMATION_DELETE_DELAY_SECONDS)
    try:
        await asyncio.to_thread(
            delete_original_interaction_response,
            application_id,
            interaction_token,
        )
    except requests.RequestException as error:
        response = error.response
        discord_code = None
        discord_message = None
        if response is not None:
            try:
                error_body = response.json()
            except ValueError:
                error_body = None
            if isinstance(error_body, dict):
                discord_code = error_body.get("code")
                discord_message = error_body.get("message")
        log_step(
            interaction_id,
            "/echo 私密確認訊息自動刪除失敗；"
            f"error_type={type(error).__name__} "
            f"status={response.status_code if response is not None else 'unknown'} "
            f"discord_code={discord_code!r} "
            f"discord_message={discord_message!r}",
        )
        return
    log_step(interaction_id, "/echo 私密確認訊息已自動刪除")


def send_echo_message(
    interaction_id: str,
    application_id: str,
    content: str,
    interaction_token: str,
) -> None:
    try:
        send_interaction_followup_message(
            application_id,
            interaction_token,
            content,
        )
    except requests.HTTPError as error:
        response = error.response
        discord_code = None
        discord_message = None
        if response is not None:
            try:
                error_body = response.json()
            except ValueError:
                error_body = None
            if isinstance(error_body, dict):
                discord_code = error_body.get("code")
                discord_message = error_body.get("message")
        log_step(
            interaction_id,
            "/echo 獨立訊息發送失敗；"
            f"status={response.status_code if response is not None else 'unknown'} "
            f"discord_code={discord_code!r} "
            f"discord_message={discord_message!r}",
        )
        failure_message = "❌ 機器人發送訊息失敗，請稍後再試。"
        try:
            edit_original_interaction_response(
                DISCORD_APP_ID,
                interaction_token,
                failure_message,
            )
        except requests.RequestException as edit_error:
            log_step(
                interaction_id,
                "/echo 私密失敗提示更新失敗；"
                f"error_type={type(edit_error).__name__}",
            )
        return
    except requests.Timeout:
        log_step(
            interaction_id,
            "/echo follow-up 發送逾時；Discord 是否已建立訊息無法確認，未自動重試",
        )
        return
    except (requests.RequestException, RuntimeError) as error:
        log_step(
            interaction_id,
            f"/echo 獨立訊息發送失敗；error_type={type(error).__name__}",
        )
        raise
    log_step(interaction_id, "/echo 獨立訊息已發送")


@router.get("/")
def home():
    """保活與測試用的首頁 (UptimeRobot 監控這個 URL)"""
    print("健康檢查：GET / 成功", flush=True)
    return {"status": "ok", "message": "Discord Webhook Bot is running!"}


@router.head("/")
def home_head():
    return Response(status_code=200)


@router.post("/interactions")
async def discord_interactions(
    request: Request,
    background_tasks: BackgroundTasks,
):
    """Discord Webhook 接收主入口"""
    request_started_at = time.perf_counter()
    log_step("pending", f"HTTP 請求進入；method={request.method} path={request.url.path}")
    try:
        signature = request.headers.get("X-Signature-Ed25519")
        timestamp = request.headers.get("X-Signature-Timestamp")
        log_step(
            "pending",
            f"簽章 headers 讀取完成；signature_present={bool(signature)} "
            f"timestamp_present={bool(timestamp)}",
        )
        body_bytes = await request.body()
        log_step("pending", f"請求本文讀取完成；bytes={len(body_bytes)}")

        log_step("pending", "開始驗證 Discord 簽章")
        if not signature or not timestamp or not verify_discord_signature(
            body_bytes,
            signature,
            timestamp,
            DISCORD_PUBLIC_KEY,
        ):
            log_step("pending", "簽章無效；拒絕請求")
            raise HTTPException(status_code=401, detail="Invalid request signature")

        log_step("pending", "開始解析 JSON")
        data = json.loads(body_bytes)
        interaction_id = data.get("id", "unknown")
        interaction_type = data.get("type")
        command_name = data.get("data", {}).get("name")
        log_step(
            interaction_id,
            f"JSON 解析完成；type={interaction_type} command={command_name}",
        )

        if interaction_type == 1:
            log_step(interaction_id, "收到 PING；準備回覆 PONG")
            return JSONResponse({"type": 1})

        if interaction_type == 3:
            custom_id = data.get("data", {}).get("custom_id", "")
            if custom_id.startswith("clean:"):
                parts = custom_id.split(":")
                if len(parts) != 4 or parts[1] not in {"confirm", "cancel"}:
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 這個清理按鈕無效或已過期。",
                            "flags": 64,
                        },
                    })

                _, action, requester_id, raw_count = parts
                try:
                    count = int(raw_count)
                except ValueError:
                    count = 0
                member = data.get("member") or {}
                user = member.get("user") or data.get("user") or {}
                if (
                    not 1 <= count <= 100
                    or not requester_id.isdecimal()
                    or user.get("id") != requester_id
                ):
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 只有發起清理的管理員可以操作這個按鈕。",
                            "flags": 64,
                        },
                    })
                if not has_administrator_permission(data):
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 只有管理員可以操作清理按鈕。",
                            "flags": 64,
                        },
                    })

                if action == "cancel":
                    return JSONResponse({
                        "type": 7,
                        "data": {
                            "content": "已取消清理。",
                            "components": [],
                        },
                    })

                channel_id = data.get("channel_id")
                interaction_token = data.get("token")
                if (
                    not isinstance(channel_id, str)
                    or not channel_id.isdecimal()
                    or not isinstance(interaction_token, str)
                    or not interaction_token
                    or not DISCORD_APP_ID
                    or not DISCORD_BOT_TOKEN
                ):
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 清理功能設定不完整，請通知 Bot 管理員。",
                            "flags": 64,
                        },
                    })

                background_tasks.add_task(
                    execute_clean_messages,
                    interaction_id,
                    DISCORD_APP_ID,
                    interaction_token,
                    channel_id,
                    count,
                )
                return JSONResponse({
                    "type": 5,
                    "data": {"flags": 64},
                })

            if custom_id.startswith("food_carousel:"):
                try:
                    index = int(custom_id.partition(":")[2])
                except ValueError:
                    index = -1
                if not 0 <= index < len(FOOD_CAROUSEL):
                    log_step(
                        interaction_id,
                        f"無效的料理輪播按鈕；custom_id={custom_id!r}",
                    )
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 這個輪播按鈕已失效。",
                            "flags": 64,
                        },
                    })

                return JSONResponse({
                    "type": 7,
                    "data": {
                        "embeds": [build_food_carousel_embed(index)],
                        "components": build_food_carousel_components(index),
                    },
                })

            log_step(
                interaction_id,
                f"不支援的按鈕互動；custom_id={custom_id!r}",
            )
            return JSONResponse({
                "type": 4,
                "data": {"content": "❌ 不支援的按鈕。", "flags": 64},
            })

        if interaction_type == 2:
            command_name = data["data"]["name"]
            log_step(
                interaction_id,
                f"進入 Slash Command 分支；command={command_name}",
            )

            if command_name == "clean":
                if not has_administrator_permission(data):
                    log_step(
                        interaction_id,
                        "拒絕 /clean；使用者沒有管理員權限",
                    )
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 只有管理員可以使用這個指令。",
                            "flags": 64,
                        },
                    })

                options = data["data"].get("options", [])
                message_count = next(
                    (
                        option.get("value")
                        for option in options
                        if option.get("name") == "message_count"
                    ),
                    None,
                )
                if (
                    not isinstance(message_count, int)
                    or isinstance(message_count, bool)
                    or not 1 <= message_count <= 100
                ):
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 清理數量必須是 1 到 100 的整數。",
                            "flags": 64,
                        },
                    })

                member = data.get("member") or {}
                user = member.get("user") or data.get("user") or {}
                requester_id = user.get("id")
                if not isinstance(requester_id, str) or not requester_id.isdecimal():
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 找不到發起清理的管理員資訊。",
                            "flags": 64,
                        },
                    })

                return JSONResponse({
                    "type": 4,
                    "data": {
                        "content": (
                            f"⚠️ 確定要刪除這個頻道最新的 "
                            f"{message_count} 則訊息嗎？超過 14 天的訊息無法批次刪除。"
                        ),
                        "flags": 64,
                        "components": [{
                            "type": 1,
                            "components": [
                                {
                                    "type": 2,
                                    "style": 4,
                                    "label": "確認刪除",
                                    "custom_id": (
                                        f"clean:confirm:{requester_id}:{message_count}"
                                    ),
                                },
                                {
                                    "type": 2,
                                    "style": 2,
                                    "label": "取消",
                                    "custom_id": (
                                        f"clean:cancel:{requester_id}:{message_count}"
                                    ),
                                },
                            ],
                        }],
                    },
                })

            if command_name == "live":
                member = data.get("member") or {}
                member_permissions = member.get("permissions")
                try:
                    is_administrator = (
                        member_permissions is not None
                        and (int(member_permissions) & 8) == 8
                    )
                except (TypeError, ValueError):
                    is_administrator = False

                if not is_administrator:
                    log_step(
                        interaction_id,
                        "拒絕 /live；使用者沒有管理員權限",
                    )
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 只有管理員可以使用這個指令。",
                            "flags": 64,
                        },
                    })

                log_step(interaction_id, "/live 檢查成功")
                return JSONResponse({
                    "type": 4,
                    "data": {"content": "live"},
                })

            if command_name == "echo":
                member = data.get("member") or {}
                member_permissions = member.get("permissions")
                try:
                    is_administrator = (
                        member_permissions is not None
                        and (int(member_permissions) & 8) == 8
                    )
                except (TypeError, ValueError):
                    is_administrator = False

                if not is_administrator:
                    log_step(
                        interaction_id,
                        "拒絕 /echo；使用者沒有權限使用此指令",
                    )
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 只有妮可可以使用這個指令。欸嘿 (ゝω・)╭☆",
                            "flags": 64,
                        },
                    })

                options = data["data"].get("options", [])
                echo_text = next(
                    (
                        option.get("value")
                        for option in options
                        if option.get("name") == "text"
                    ),
                    None,
                )
                if not isinstance(echo_text, str) or not echo_text.strip():
                    log_step(interaction_id, "/echo 缺少有效文字")
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 請輸入要發送的文字。",
                            "flags": 64,
                        },
                    })

                interaction_token = data.get("token")
                if not isinstance(interaction_token, str) or not interaction_token:
                    log_step(interaction_id, "/echo 缺少 follow-up 所需的互動 token")
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 找不到發送訊息所需的互動資訊。",
                            "flags": 64,
                        },
                    })
                if not DISCORD_APP_ID:
                    log_step(interaction_id, "/echo 缺少 DISCORD_APP_ID")
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 機器人的訊息發送功能尚未設定。",
                            "flags": 64,
                        },
                    })

                cleanup_task = asyncio.create_task(
                    delete_echo_confirmation(
                        interaction_id,
                        DISCORD_APP_ID,
                        interaction_token,
                    ),
                    name=f"echo-cleanup-{interaction_id}",
                )
                pending_echo_cleanup_tasks.add(cleanup_task)
                cleanup_task.add_done_callback(pending_echo_cleanup_tasks.discard)
                background_tasks.add_task(
                    send_echo_message,
                    interaction_id,
                    DISCORD_APP_ID,
                    echo_text,
                    interaction_token,
                )
                log_step(interaction_id, f"/echo 排入 follow-up 訊息；length={len(echo_text)}")
                return JSONResponse({
                    "type": 4,
                    "data": {
                        "content": "機器人即將發送指定訊息，請稍候。",
                        "flags": 64,
                    },
                })

            if command_name == "廢文":
                post = random.choice(waste_posts)
                log_step(interaction_id, "多邊獸發廢文")
                return JSONResponse({
                    "type": 4,
                    "data": {"content": post},
                })

            if command_name in IMAGE_URLS:
                if is_command_rate_limited():
                    log_step(interaction_id, "指令過於頻繁；回覆「等一下啦」")
                    return JSONResponse({
                        "type": 4,
                        "data": {"content": "等一下啦"},
                    })

                if command_name in FOOD_CAROUSEL_INDEX:
                    carousel_index = FOOD_CAROUSEL_INDEX[command_name]
                    log_step(
                        interaction_id,
                        f"料理輪播初始頁；command={command_name} "
                        f"index={carousel_index}",
                    )
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "embeds": [
                                build_food_carousel_embed(carousel_index)
                            ],
                            "components": build_food_carousel_components(
                                carousel_index
                            ),
                        },
                    })

                keyword = command_name
                log_step(
                    interaction_id,
                    f"指令選項解析完成；keyword={keyword!r}",
                )
                safe_keyword = "".join(
                    character
                    for character in keyword
                    if character.isalnum() or character.isspace()
                ).strip().replace(" ", "_")
                image_url = IMAGE_URLS.get(safe_keyword)
                if image_url is None:
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": (
                                f"❌ 找不到關鍵字為 `{safe_keyword}` "
                                "的圖片網址。"
                            ),
                        },
                    })
                log_step(
                    interaction_id,
                    f"圖片 CDN 網址查找完成；keyword={safe_keyword!r}",
                )
                log_step(
                    interaction_id,
                    f"直接回覆圖片 CDN 網址；type=4 "
                    f"elapsed={time.perf_counter() - request_started_at:.3f}s",
                )
                return JSONResponse({
                    "type": 4,
                    "data": {"content": image_url},
                })

        log_step(
            interaction_id,
            f"不支援的互動；type={interaction_type} command={command_name}",
        )
        return JSONResponse(
            {"error": "Unsupported interaction type"},
            status_code=400,
        )

    except HTTPException as error:
        return JSONResponse(
            {"detail": error.detail},
            status_code=error.status_code,
            headers=error.headers,
        )
    except Exception as error:
        log_step(
            "pending",
            f"HTTP handler 發生錯誤；error_type={type(error).__name__} "
            f"elapsed={time.perf_counter() - request_started_at:.3f}s",
        )
        return JSONResponse({"error": str(error)}, status_code=400)
