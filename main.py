import os
import io
import json
import time
import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
# from google import genai
# from google.genai import types
# from PIL import Image as PILImage
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import nacl.signing
import nacl.exceptions

app = FastAPI()

def log_step(interaction_id: str, message: str):
    print(f"[interaction_id={interaction_id}] {message}", flush=True)

# ================= 1. 環境變數讀取 =================
# GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")
DISCORD_APP_ID = os.environ.get("DISCORD_APP_ID")
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")
CREDENTIALS_JSON_CONTENT = os.environ.get("CREDENTIALS_JSON_CONTENT")
print(
    "啟動：環境變數狀態 "
    f"DISCORD_PUBLIC_KEY={bool(DISCORD_PUBLIC_KEY)} "
    f"DISCORD_APP_ID={bool(DISCORD_APP_ID)} "
    f"GDRIVE_FOLDER_ID={bool(GDRIVE_FOLDER_ID)}",
    flush=True,
)

# 動態還原 credentials.json (供 Google API 使用)
CREDENTIALS_FILE = "credentials.json"
print("啟動：檢查 Google 服務帳戶憑證設定", flush=True)
print(f"啟動：CREDENTIALS_JSON_CONTENT 已設定={bool(CREDENTIALS_JSON_CONTENT)}", flush=True)
if CREDENTIALS_JSON_CONTENT and not os.path.exists(CREDENTIALS_FILE):
    print("啟動：尚無本機憑證檔，正在從環境變數建立", flush=True)
    with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        f.write(CREDENTIALS_JSON_CONTENT)
    print("啟動：憑證檔建立完成", flush=True)
elif os.path.exists(CREDENTIALS_FILE):
    print("啟動：使用既有本機憑證檔", flush=True)
else:
    print("啟動錯誤：未設定憑證 JSON，且本機憑證檔不存在", flush=True)

# 初始化 Google Drive API Client
SCOPES = ['https://www.googleapis.com/auth/drive']
print(f"啟動：初始化 Google Drive API client；資料夾 ID 已設定={bool(GDRIVE_FOLDER_ID)}", flush=True)
try:
    creds = service_account.Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    print("啟動：服務帳戶憑證解析完成", flush=True)
    drive_service = build('drive', 'v3', credentials=creds)
except Exception as e:
    print(f"啟動錯誤：Google Drive API client 初始化失敗；error_type={type(e).__name__}", flush=True)
    raise
print("啟動：Google Drive API client 初始化完成", flush=True)

# 初始化 Gemini Client
# gemini_client = genai.Client(api_key=GEMINI_API_KEY)

LOCAL_TEMP_DIR = "./temp_images"
os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)
print(f"啟動：本機暫存目錄就緒；exists={os.path.isdir(LOCAL_TEMP_DIR)}", flush=True)

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

def find_image_in_drive(file_stem: str, interaction_id: str):
    """依檔名主體比對資料夾內圖片，不將副檔名納入比對。"""
    query = f"'{GDRIVE_FOLDER_ID}' in parents and trashed = false"
    page_token = None
    files = []
    started_at = time.perf_counter()
    log_step(interaction_id, f"Drive 搜尋：列取資料夾檔案，依檔名主體比對；file_stem={file_stem!r}")

    try:
        while True:
            results = drive_service.files().list(
                q=query,
                fields="nextPageToken, files(id, name, mimeType)",
                pageSize=1000,
                pageToken=page_token,
            ).execute()
            files.extend(results.get("files", []))
            page_token = results.get("nextPageToken")
            if not page_token:
                break

    except Exception as e:
        log_step(
            interaction_id,
            f"Drive 搜尋失敗；已列取項目數={len(files)} "
            f"elapsed={time.perf_counter() - started_at:.2f}s "
            f"error_type={type(e).__name__}",
        )
        raise

    log_step(
        interaction_id,
        f"Drive 資料夾檔案列取完成；total={len(files)} "
        f"elapsed={time.perf_counter() - started_at:.2f}s",
    )

    matching_files = [
        drive_file
        for drive_file in files
        if drive_file.get("mimeType", "").startswith("image/")
        and os.path.splitext(drive_file.get("name", ""))[0].casefold() == file_stem.casefold()
    ]
    matching_files.sort(
        key=lambda drive_file: (
            {".png": 0, ".jpg": 1}.get(
                os.path.splitext(drive_file.get("name", ""))[1].lower(),
                2,
            ),
            drive_file.get("name", "").casefold(),
        )
    )

    if matching_files:
        selected_file = matching_files[0]
        log_step(
            interaction_id,
            f"Drive 搜尋完成；filename_matches={len(matching_files)} "
            f"selected={selected_file.get('name')!r} "
            f"mimeType={selected_file.get('mimeType')!r}",
        )
        return (
            selected_file["id"],
            selected_file["name"],
            selected_file.get("mimeType", "application/octet-stream"),
        )

    log_step(interaction_id, "Drive 搜尋結果：沒有符合檔名主體的圖片；輸出資料夾清單")
    for index, drive_file in enumerate(files, start=1):
        log_step(
            interaction_id,
            f"Drive 清單項目 #{index}："
            f"name={drive_file.get('name', '<unnamed>')!r} "
            f"mimeType={drive_file.get('mimeType', '<unknown>')!r}",
        )
    log_step(interaction_id, f"Drive 清單列取完成；total={len(files)}")
    return None

def download_image_from_drive(file_id: str, local_path: str, interaction_id: str):
    """從 Google Drive 下載檔案到本地"""
    log_step(interaction_id, "Drive 下載：建立媒體請求")
    started_at = time.perf_counter()
    media_request = drive_service.files().get_media(fileId=file_id)
    log_step(interaction_id, f"Drive 下載：開始寫入暫存檔；path={local_path}")
    with io.FileIO(local_path, 'wb') as fh:
        downloader = MediaIoBaseDownload(fh, media_request)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                log_step(
                    interaction_id,
                    f"Drive 下載進度：{status.progress() * 100:.0f}%",
                )

    log_step(
        interaction_id,
        f"Drive 下載完成；bytes={os.path.getsize(local_path)} "
        f"elapsed={time.perf_counter() - started_at:.2f}s",
    )

def ensure_image_extension(file_name: str, mime_type: str, interaction_id: str) -> str:
    """若 Drive 檔名沒有副檔名，依 MIME type 補上常見圖片副檔名。"""
    if os.path.splitext(file_name)[1]:
        return file_name

    extension_by_mime_type = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/tiff": ".tiff",
        "image/svg+xml": ".svg",
        "image/avif": ".avif",
    }
    extension = extension_by_mime_type.get(mime_type.lower())
    if not extension:
        raise ValueError(f"無法從圖片 MIME type 推斷副檔名：{mime_type}")

    updated_name = f"{file_name}{extension}"
    log_step(
        interaction_id,
        f"Drive 檔名沒有副檔名；依 MIME type 補上 {extension}，"
        f"Discord 上傳檔名={updated_name!r}",
    )
    return updated_name

# def upload_image_to_drive(local_path: str, file_name: str):
#     """上傳本地圖片到 Google Drive"""
#     file_metadata = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
#     media = MediaFileUpload(local_path, mimetype='image/png')
#     file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
#     return file.get('id')

# ================= 3. 背景非同步處理任務 =================
def send_followup(
    followup_url: str,
    interaction_id: str,
    payload: dict,
    interaction_token: str,
    files=None,
):
    started_at = time.perf_counter()
    log_step(
        interaction_id,
        f"Discord follow-up：開始送出；has_file={files is not None} timeout=15s",
    )
    try:
        if files is None:
            response = requests.post(followup_url, json=payload, timeout=15)
        else:
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
    if not response.ok:
        try:
            error_body = response.json()
        except ValueError:
            error_body = {}
        if not isinstance(error_body, dict):
            error_body = {}
        error_details = {
            "code": error_body.get("code"),
            "message": error_body.get("message"),
            "errors": error_body.get("errors"),
        }
        error_details = json.dumps(error_details, ensure_ascii=False, default=str)[:2000]
        error_details = error_details.replace(interaction_token, "[REDACTED]")
        log_step(
            interaction_id,
            "Discord 錯誤詳情；"
            f"content_type={response.headers.get('Content-Type')} "
            f"body={error_details}",
        )
    response.raise_for_status()
    log_step(interaction_id, "Discord follow-up 成功")

def process_image_interaction(
    interaction_id: str,
    application_id: str,
    interaction_token: str,
    user_id: str,
    keyword: str,
):
    """
    到 Google 雲端硬碟依檔名主體尋找圖片，不比對副檔名。
    如果找到：直接下載並回傳給使用者。
    如果找不到：直接回報雲端硬碟沒有該圖片，不會再進行額外生圖。
    """
    task_started_at = time.perf_counter()
    log_step(interaction_id, "背景工作已啟動")
    followup_url = f"https://discord.com/api/v10/webhooks/{application_id}/{interaction_token}"
    log_step(interaction_id, f"Follow-up webhook 已建立；application_id_present={bool(application_id)}")
    
    # 處理中文或空白檔名
    log_step(interaction_id, f"輸入關鍵字：{keyword!r}")
    safe_filename = "".join([c for c in keyword if c.isalnum() or c.isspace()]).strip().replace(" ", "_")
    log_step(interaction_id, f"檔名主體解析完成；file_stem={safe_filename}")

    stage = "Drive 搜尋"
    try:
        image = find_image_in_drive(safe_filename, interaction_id)

        if image:
            file_id, file_name, mime_type = image
            file_name = ensure_image_extension(file_name, mime_type, interaction_id)
            local_path = os.path.join(LOCAL_TEMP_DIR, file_name)
            log_step(interaction_id, f"Drive 搜尋結果：找到符合檔案；file_name={file_name}")

            stage = "Drive 下載"
            download_image_from_drive(file_id, local_path, interaction_id)

            content = f"Hey <@{user_id}>！在雲端硬碟找到「{keyword}」的圖片囉："
            
            stage = "Discord 圖片 follow-up"
            with open(local_path, "rb") as f:
                log_step(
                    interaction_id,
                    f"Discord 上傳：開啟暫存圖片；exists={os.path.isfile(local_path)} "
                    f"bytes={os.path.getsize(local_path)}",
                )
                files = {"files[0]": (file_name, f, mime_type)}
                payload = {"content": content}
                send_followup(
                    followup_url,
                    interaction_id,
                    payload,
                    interaction_token,
                    files,
                )
        else:
            log_step(interaction_id, "Drive 搜尋結果：沒有符合檔名主體的圖片")
            payload = {
                "content": (
                    f"❌ 抱歉，雲端硬碟中找不到檔名為 "
                    f"`{safe_filename}` 的圖片（已忽略副檔名）。"
                )
            }
            stage = "Discord 找不到檔案 follow-up"
            send_followup(
                followup_url,
                interaction_id,
                payload,
                interaction_token,
            )

    except Exception as e:
        log_step(
            interaction_id,
            f"背景工作失敗；stage={stage} "
            f"elapsed={time.perf_counter() - task_started_at:.2f}s "
            f"error_type={type(e).__name__}",
        )
        try:
            error_payload = {"content": "❌ 處理圖片時發生內部錯誤，請稍後再試。"}
            send_followup(
                followup_url,
                interaction_id,
                error_payload,
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
            f"背景工作結束；elapsed={time.perf_counter() - task_started_at:.2f}s",
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