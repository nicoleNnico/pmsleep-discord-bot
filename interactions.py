import json
import os
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from discord_security import verify_discord_signature
from image_commands import (
    FOOD_CAROUSEL,
    FOOD_CAROUSEL_INDEX,
    IMAGE_URLS,
    build_food_carousel_components,
    build_food_carousel_embed,
    is_command_rate_limited,
)


router = APIRouter()


def log_step(interaction_id: str, message: str) -> None:
    print(f"[interaction_id={interaction_id}] {message}", flush=True)


@router.get("/")
def home():
    """保活與測試用的首頁 (UptimeRobot 監控這個 URL)"""
    print("健康檢查：GET / 成功", flush=True)
    return {"status": "ok", "message": "Discord Webhook Bot is running!"}


@router.post("/interactions")
async def discord_interactions(request: Request):
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
            os.environ.get("DISCORD_PUBLIC_KEY"),
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

                log_step(interaction_id, f"/echo 發送文字；length={len(echo_text)}")
                return JSONResponse({
                    "type": 4,
                    "data": {
                        "content": echo_text,
                        "allowed_mentions": {
                            "parse": [],
                            "replied_user": False,
                        },
                    },
                })

            if command_name in IMAGE_URLS or command_name == "nico":
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

                if command_name == "nico":
                    log_step(interaction_id, "開始解析指令選項")
                    options = data["data"].get("options", [])
                    keyword = next(
                        (
                            option["value"]
                            for option in options
                            if option["name"] == "keyword"
                        ),
                        "性格",
                    )
                else:
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

    except Exception as error:
        log_step(
            "pending",
            f"HTTP handler 發生錯誤；error_type={type(error).__name__} "
            f"elapsed={time.perf_counter() - request_started_at:.3f}s",
        )
        return JSONResponse({"error": str(error)}, status_code=400)
