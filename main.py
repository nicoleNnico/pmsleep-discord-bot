import os
import json
import mimetypes
import time
import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
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
IMAGE_FILES_CONFIG = os.path.join(PROJECT_DIR, "assets", "image_files.json")
with open(IMAGE_FILES_CONFIG, encoding="utf-8") as config_file:
    image_file_entries = json.load(config_file)

if not isinstance(image_file_entries, dict):
    raise ValueError(f"圖片清單必須是 JSON object：{IMAGE_FILES_CONFIG}")

IMAGE_FILES = {}
for keyword, relative_path in image_file_entries.items():
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError("圖片清單中的關鍵字必須是非空字串")
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError(f"圖片路徑必須是非空字串：{keyword!r}")
    if os.path.isabs(relative_path):
        raise ValueError(f"圖片路徑必須相對於專案根目錄：{relative_path!r}")
    IMAGE_FILES[keyword] = os.path.normpath(
        os.path.join(PROJECT_DIR, relative_path)
    )

# ================= 2. 輔助函式 =================
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

# ================= 3. 背景非同步處理任務 =================
def send_followup(
    followup_url: str,
    interaction_id: str,
    payload: dict,
    interaction_token: str,
    files=None,
):
    max_attempts = 2
    max_retry_delay = 14 * 60

    for attempt in range(1, max_attempts + 1):
        started_at = time.perf_counter()
        log_step(
            interaction_id,
            f"Discord follow-up：開始送出；has_file={files is not None} "
            f"attempt={attempt}/{max_attempts} timeout=15s",
        )
        try:
            if files is None:
                response = requests.post(followup_url, json=payload, timeout=15)
            else:
                for file_data in files.values():
                    file_data[1].seek(0)
                response = requests.post(
                    followup_url,
                    data={"payload_json": json.dumps(payload)},
                    files=files,
                    timeout=15,
                )
        except requests.RequestException as e:
            log_step(
                interaction_id,
                f"Discord follow-up 網路請求失敗；"
                f"elapsed={time.perf_counter() - started_at:.2f}s "
                f"error_type={type(e).__name__}",
            )
            raise

        log_step(
            interaction_id,
            f"Discord follow-up 收到回應；status={response.status_code} "
            f"elapsed={time.perf_counter() - started_at:.2f}s",
        )

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after is None:
                try:
                    error_body = response.json()
                    retry_after = (
                        error_body.get("retry_after")
                        if isinstance(error_body, dict)
                        else None
                    )
                except (ValueError, AttributeError):
                    retry_after = None
            try:
                retry_delay = float(retry_after) if retry_after is not None else 60.0
            except (TypeError, ValueError):
                retry_delay = 60.0
            retry_delay = max(retry_delay, 60.0)

            if attempt == max_attempts or retry_delay > max_retry_delay:
                log_step(
                    interaction_id,
                    "Discord follow-up 遭遇 HTTP 429；"
                    f"不再重試；retry_after={retry_delay:.1f}s",
                )
                raise requests.HTTPError("Discord follow-up rate limited", response=response)

            log_step(
                interaction_id,
                f"Discord follow-up 遭遇 HTTP 429；等待 {retry_delay:.1f}s 後"
                f"再嘗試一次（不超過 {max_retry_delay}s）",
            )
            time.sleep(retry_delay)
            continue

        if not response.ok:
            content_type = response.headers.get("Content-Type", "")
            try:
                error_body = response.json()
            except ValueError:
                error_body = {}
            if not isinstance(error_body, dict):
                error_body = {}
            if error_body:
                error_details = {
                    "code": error_body.get("code"),
                    "message": error_body.get("message"),
                    "errors": error_body.get("errors"),
                }
                error_details = json.dumps(error_details, ensure_ascii=False, default=str)
            else:
                error_details = response.text
            error_details = error_details.replace(interaction_token, "[REDACTED]")[:2000]
            log_step(
                interaction_id,
                "Discord 錯誤詳情；"
                f"content_type={content_type} "
                f"body={error_details}",
            )
        response.raise_for_status()
        log_step(interaction_id, "Discord follow-up 成功")
        return

def process_image_interaction(
    interaction_id: str,
    application_id: str,
    interaction_token: str,
    user_id: str,
    keyword: str,
):
    """依本機圖片字典找到檔案並上傳到 Discord。"""
    task_started_at = time.perf_counter()
    log_step(interaction_id, "本機圖片背景工作已啟動")
    followup_url = f"https://discord.com/api/v10/webhooks/{DISCORD_APP_ID}/{interaction_token}"
    log_step(interaction_id, f"Follow-up webhook 已建立；application_id_present={bool(application_id)}")

    log_step(interaction_id, f"輸入關鍵字：{keyword!r}")
    safe_filename = "".join(
        c for c in keyword if c.isalnum() or c.isspace()
    ).strip().replace(" ", "_")
    image_path = IMAGE_FILES.get(safe_filename)
    log_step(
        interaction_id,
        f"本機圖片查找完成；file_stem={safe_filename!r} found={image_path is not None}",
    )

    stage = "本機圖片查找"
    try:
        if image_path is None:
            payload = {
                "content": (
                    f"❌ 找不到檔名為 `{safe_filename}` 的本機圖片。"
                )
            }
            stage = "Discord 找不到檔案 follow-up"
            send_followup(
                followup_url,
                interaction_id,
                payload,
                interaction_token,
            )
        else:
            if not os.path.isfile(image_path):
                raise FileNotFoundError(f"本機圖片檔案不存在：{image_path}")

            file_name = os.path.basename(image_path)
            mime_type = mimetypes.guess_type(file_name)[0] or "application/octet-stream"
            content = f"<@{user_id}> keyword: `{keyword}`"
            stage = "Discord 本機圖片 follow-up"
            with open(image_path, "rb") as image_file:
                log_step(
                    interaction_id,
                    f"Discord 上傳：開啟本機圖片；path={image_path} "
                    f"bytes={os.path.getsize(image_path)}",
                )
                files = {"files[0]": (file_name, image_file, mime_type)}
                send_followup(
                    followup_url,
                    interaction_id,
                    {"content": content},
                    interaction_token,
                    files,
                )
    except Exception as e:
        log_step(
            interaction_id,
            f"背景工作失敗；stage={stage} "
            f"elapsed={time.perf_counter() - task_started_at:.2f}s "
            f"error_type={type(e).__name__}",
        )
        if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 429:
            log_step(
                interaction_id,
                "Discord 仍在限流；略過錯誤 follow-up，避免立即再送請求",
            )
            return

        try:
            send_followup(
                followup_url,
                interaction_id,
                {"content": "❌ 處理圖片時發生內部錯誤，請稍後再試。"},
                interaction_token,
            )
        except Exception as followup_error:
            log_step(
                interaction_id,
                "錯誤通知也無法送出；"
                f"error_type={type(followup_error).__name__}",
            )
    else:
        log_step(
            interaction_id,
            f"本機圖片背景工作結束；elapsed={time.perf_counter() - task_started_at:.2f}s",
        )


# ================= 4. FastAPI 路由設定 =================
@app.get("/")
def home():
    """保活與測試用的首頁 (UptimeRobot 監控這個 URL)"""
    print("健康檢查：GET / 成功", flush=True)
    return {"status": "ok", "message": "Discord Webhook Bot is running!"}

@app.post("/interactions")
async def discord_interactions(request: Request, background_tasks: BackgroundTasks):
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
            
            if command_name == "nico": # (或是你的指令名稱)
                log_step(interaction_id, "開始解析指令選項")
                options = data["data"].get("options", [])
                keyword = next((opt["value"] for opt in options if opt["name"] == "keyword"), "性格")
                log_step(interaction_id, f"指令選項解析完成；keyword={keyword!r}")
                
                interaction_token = data["token"]
                application_id = DISCORD_APP_ID
                user_id = data.get("member", {}).get("user", {}).get("id") or data.get("user", {}).get("id")
                log_step(
                    interaction_id,
                    f"回覆資料準備完成；user_id_present={bool(user_id)} "
                    f"token_present={bool(interaction_token)} "
                    f"application_id_present={bool(application_id)}",
                )

                if not application_id:
                    log_step(
                        interaction_id,
                        "無法排程背景工作：DISCORD_APP_ID 環境變數未設定",
                    )
                    return JSONResponse(
                        {"error": "Missing Discord application ID"},
                        status_code=500,
                    )

                log_step(interaction_id, "開始排程背景工作")
                background_tasks.add_task(
                    process_image_interaction,
                    interaction_id,
                    application_id,
                    interaction_token,
                    user_id,
                    keyword,
                )
                log_step(interaction_id, "背景工作已排程；準備回傳 deferred response type=5")

                response = JSONResponse({
                    "type": 5,
                    "data": {"content": f"🔍 正在處理「{keyword}」，請稍候..."}
                })
                log_step(
                    interaction_id,
                    f"HTTP handler 即將完成；deferred_type=5 "
                    f"elapsed={time.perf_counter() - request_started_at:.3f}s",
                )
                return response

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