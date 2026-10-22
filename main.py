import os
import json
import time
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
import nacl.signing
import nacl.exceptions

app = FastAPI()

def log_step(interaction_id: str, message: str):
    print(f"[interaction_id={interaction_id}] {message}", flush=True)

# ================= 1. 環境變數讀取 =================
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")
DISCORD_APP_ID = os.environ.get("DISCORD_APP_ID")
print(
    "啟動：環境變數狀態 "
    f"DISCORD_PUBLIC_KEY={bool(DISCORD_PUBLIC_KEY)} "
    f"DISCORD_APP_ID={bool(DISCORD_APP_ID)}",
    flush=True,
)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_URLS_CONFIG = os.path.join(PROJECT_DIR, "assets", "image_urls.json")
with open(IMAGE_URLS_CONFIG, encoding="utf-8") as config_file:
    image_url_entries = json.load(config_file)

if not isinstance(image_url_entries, dict):
    raise ValueError(f"圖片網址清單必須是 JSON object：{IMAGE_URLS_CONFIG}")

IMAGE_URLS = {}
for keyword, image_url in image_url_entries.items():
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError("圖片清單中的關鍵字必須是非空字串")
    if not isinstance(image_url, str) or not image_url.startswith("https://"):
        raise ValueError(f"圖片網址必須是 HTTPS URL：{keyword!r}")
    IMAGE_URLS[keyword] = image_url

COMMAND_COOLDOWN_SECONDS = 1.0
last_command_received_at = None

# ================= 2. 輔助函式 =================
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

def verify_discord_signature(request_body: bytes, signature: str, timestamp: str) -> bool:
    """驗證 Discord Webhook 請求簽章"""
    print(f"簽章驗證：開始；body_bytes={len(request_body)}", flush=True)
    if not DISCORD_PUBLIC_KEY:
        print("簽章驗證失敗：DISCORD_PUBLIC_KEY 未設定", flush=True)
        return False
        
    if not signature or not timestamp:
        print(
            f"簽章驗證失敗：必要 header 缺漏；"
            f"signature_present={bool(signature)} timestamp_present={bool(timestamp)}",
            flush=True,
        )
        return False

    try:
        # 將 Hex 字串轉成 PyNaCl 的 VerifyKey
        print("簽章驗證：解析公鑰與簽章格式", flush=True)
        verify_key = nacl.signing.VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
        
        # 組合 Discord 規定的驗證訊息：timestamp.encode() + request_body
        message = timestamp.encode() + request_body
        
        # 進行驗證
        print("簽章驗證：比對請求簽章", flush=True)
        verify_key.verify(message, bytes.fromhex(signature))
        print("簽章驗證成功", flush=True)
        return True
        
    except ValueError as ve:
        print(f"簽章驗證失敗：Hex 格式錯誤（{type(ve).__name__}）", flush=True)
        return False
    except nacl.exceptions.BadSignatureError:
        print("簽章驗證失敗：簽章與公鑰不符", flush=True)
        return False
    except Exception as e:
        print(f"簽章驗證發生未預期錯誤：{type(e).__name__}", flush=True)
        return False

# ================= 4. FastAPI 路由設定 =================
@app.get("/")
def home():
    """保活與測試用的首頁 (UptimeRobot 監控這個 URL)"""
    print("健康檢查：GET / 成功", flush=True)
    return {"status": "ok", "message": "Discord Webhook Bot is running!"}

@app.post("/interactions")
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
        if not signature or not timestamp or not verify_discord_signature(body_bytes, signature, timestamp):
            log_step("pending", "簽章無效；拒絕請求")
            raise HTTPException(status_code=401, detail="Invalid request signature")

        log_step("pending", "開始解析 JSON")
        data = await request.json()
        interaction_id = data.get("id", "unknown")
        interaction_type = data.get("type")
        command_name = data.get("data", {}).get("name")
        log_step(interaction_id, f"JSON 解析完成；type={interaction_type} command={command_name}")

        # 1. 處理 Discord PING
        if data.get("type") == 1:
            log_step(interaction_id, "收到 PING；準備回覆 PONG")
            return JSONResponse({"type": 1})

        # 2. 處理 Slash Command
        if data.get("type") == 2:
            command_name = data["data"]["name"]
            log_step(interaction_id, f"進入 Slash Command 分支；command={command_name}")
            
            if command_name in ("nico", "性格"):
                if is_command_rate_limited():
                    log_step(interaction_id, "指令過於頻繁；回覆「等一下啦」")
                    return JSONResponse({
                        "type": 4,
                        "data": {"content": "等一下啦"},
                    })

                if command_name == "性格":
                    keyword = "性格"
                else:
                    log_step(interaction_id, "開始解析指令選項")
                    options = data["data"].get("options", [])
                    keyword = next(
                        (opt["value"] for opt in options if opt["name"] == "keyword"),
                        "性格",
                    )
                log_step(interaction_id, f"指令選項解析完成；keyword={keyword!r}")
                safe_keyword = "".join(
                    character for character in keyword
                    if character.isalnum() or character.isspace()
                ).strip().replace(" ", "_")
                image_url = IMAGE_URLS.get(safe_keyword)
                content = image_url or f"❌ 找不到關鍵字為 `{safe_keyword}` 的圖片網址。"
                log_step(
                    interaction_id,
                    f"圖片網址查找完成；keyword={safe_keyword!r} found={image_url is not None}",
                )
                log_step(
                    interaction_id,
                    f"直接回覆 Slash Command；type=4 "
                    f"elapsed={time.perf_counter() - request_started_at:.3f}s",
                )
                return JSONResponse({
                    "type": 4,
                    "data": {"content": content},
                })

        log_step(interaction_id, f"不支援的互動；type={interaction_type} command={command_name}")
        return JSONResponse({"error": "Unsupported interaction type"}, status_code=400)

    except Exception as e:
        log_step(
            "pending",
            f"HTTP handler 發生錯誤；error_type={type(e).__name__} "
            f"elapsed={time.perf_counter() - request_started_at:.3f}s",
        )
        return JSONResponse({"error": str(e)}, status_code=400)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)