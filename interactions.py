import asyncio
import json
from pathlib import Path
import random
import time

import requests
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse

from config import DISCORD_APP_ID, DISCORD_BOT_TOKEN, DISCORD_PUBLIC_KEY
from discord_security import verify_discord_signature
from discord_messages import (
    delete_original_interaction_response,
    edit_original_interaction_response,
    send_channel_message,
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
        log_step(
            interaction_id,
            f"/echo 私密確認訊息自動刪除失敗；error_type={type(error).__name__}",
        )
        return
    log_step(interaction_id, "/echo 私密確認訊息已自動刪除")


def send_echo_message(
    interaction_id: str,
    token: str,
    channel_id: str,
    content: str,
    interaction_token: str,
) -> None:
    try:
        send_channel_message(token, channel_id, content)
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
        failure_message = (
            "❌ 機器人無法在這個頻道發送訊息，請確認頻道權限。"
            if response is not None and response.status_code == 403
            else "❌ 機器人發送訊息失敗，請稍後再試。"
        )
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

                channel_id = data.get("channel_id")
                token = DISCORD_BOT_TOKEN
                if not isinstance(channel_id, str) or not channel_id.isdecimal():
                    log_step(interaction_id, "/echo 缺少有效頻道 ID")
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 找不到要發送的頻道。",
                            "flags": 64,
                        },
                    })
                if not token:
                    log_step(interaction_id, "/echo 缺少 DISCORD_BOT_TOKEN")
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 機器人的獨立發送功能尚未設定。",
                            "flags": 64,
                        },
                    })

                interaction_token = data.get("token")
                if not isinstance(interaction_token, str) or not interaction_token:
                    log_step(interaction_id, "/echo 缺少互動 token")
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 找不到刪除確認訊息所需的互動資訊。",
                            "flags": 64,
                        },
                    })
                if not DISCORD_APP_ID:
                    log_step(interaction_id, "/echo 缺少 DISCORD_APP_ID")
                    return JSONResponse({
                        "type": 4,
                        "data": {
                            "content": "❌ 確認訊息自動刪除功能尚未設定。",
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
                    token,
                    channel_id,
                    echo_text,
                    interaction_token,
                )
                log_step(interaction_id, f"/echo 排入獨立訊息；length={len(echo_text)}")
                return JSONResponse({
                    "type": 4,
                    "data": {
                        "content": "收到，機器人會另外發一則訊息；此訊息 3 秒後自動刪除。",
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
