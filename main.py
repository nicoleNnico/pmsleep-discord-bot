import os
import io
import json
import requests
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import JSONResponse
from google import genai
from google.genai import types
from PIL import Image as PILImage
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
import nacl.signing
import nacl.exceptions

app = FastAPI()

# ================= 1. 環境變數讀取 =================
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")
DISCORD_APP_ID = os.environ.get("DISCORD_APP_ID")
FOLDER_ID = os.environ.get("FOLDER_ID")
CREDENTIALS_JSON_CONTENT = os.environ.get("CREDENTIALS_JSON_CONTENT")

# 動態還原 credentials.json (供 Google API 使用)
CREDENTIALS_FILE = "credentials.json"
if CREDENTIALS_JSON_CONTENT and not os.path.exists(CREDENTIALS_FILE):
    with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
        f.write(CREDENTIALS_JSON_CONTENT)

# 初始化 Google Drive API Client
SCOPES = ['https://www.googleapis.com/auth/drive']
creds = service_account.Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
drive_service = build('drive', 'v3', credentials=creds)

# 初始化 Gemini Client
gemini_client = genai.Client(api_key=GEMINI_API_KEY)

LOCAL_TEMP_DIR = "./temp_images"
os.makedirs(LOCAL_TEMP_DIR, exist_ok=True)

# ================= 2. 輔助函式 =================
def verify_discord_signature(request_body: bytes, signature: str, timestamp: str) -> bool:
    """驗證 Discord Webhook 請求簽章"""
    if not DISCORD_PUBLIC_KEY:
        return False
    verify_key = nacl.signing.VerifyKey(bytes.fromhex(DISCORD_PUBLIC_KEY))
    try:
        verify_key.verify(timestamp.encode() + request_body, bytes.fromhex(signature))
        return True
    except nacl.exceptions.BadSignatureError:
        return False

def find_image_in_drive(file_name: str):
    """在指定的 Google Drive 資料夾搜尋檔案"""
    query = f"'{FOLDER_ID}' in parents and name = '{file_name}' and trashed = false"
    results = drive_service.files().list(q=query, fields="files(id, name)").execute()
    files = results.get('files', [])
    return files[0]['id'] if files else None

def download_image_from_drive(file_id: str, local_path: str):
    """從 Google Drive 下載檔案到本地"""
    request = drive_service.files().get_media(fileId=file_id)
    fh = io.FileIO(local_path, 'wb')
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

def upload_image_to_drive(local_path: str, file_name: str):
    """上傳本地圖片到 Google Drive"""
    file_metadata = {'name': file_name, 'parents': [FOLDER_ID]}
    media = MediaFileUpload(local_path, mimetype='image/png')
    file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    return file.get('id')

# ================= 3. 背景非同步處理任務 =================
def process_image_interaction(interaction_token: str, user_id: str, keyword: str):
    """背景執行：找圖 / 產圖 ➜ 上傳 ➜ Followup 回傳給 Discord"""
    followup_url = f"https://discord.com/api/v10/webhooks/{DISCORD_APP_ID}/{interaction_token}"
    
    safe_filename = "".join([c for c in keyword if c.isalnum() or c.isspace()]).strip().replace(" ", "_")
    file_name = f"{safe_filename}.png"
    local_path = os.path.join(LOCAL_TEMP_DIR, file_name)

    try:
        # 1. 檢查 Google 雲端硬碟
        file_id = find_image_in_drive(file_name)

        if file_id:
            download_image_from_drive(file_id, local_path)
            content = f"Hey <@{user_id}>！在雲端硬碟找到「{keyword}」的圖片囉："
        else:
            # 2. 雲端沒有，呼叫 Gemini 產圖
            result = gemini_client.models.generate_images(
                model='imagen-3.0-generate-002',
                prompt=keyword,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    output_mime_type="image/png",
                    aspect_ratio="1:1",
                )
            )
            for generated_image in result.generated_images:
                image = PILImage.open(io.BytesIO(generated_image.image.image_bytes))
                image.save(local_path)
                break

            # 上傳至 Google Drive
            upload_image_to_drive(local_path, file_name)
            content = f"✨ 已透過 Gemini 生成「{keyword}」並自動同步至雲端硬碟！這是給 <@{user_id}> 的圖片："

        # 3. 透過 Discord Followup API 補發訊息與附件
        with open(local_path, "rb") as f:
            files = {"file": (file_name, f, "image/png")}
            payload = {"content": content}
            requests.post(followup_url, data={"payload_json": json.dumps(payload)}, files=files)

    except Exception as e:
        error_payload = {"content": f"❌ 處理「{keyword}」時發生錯誤：`{str(e)}`"}
        requests.post(followup_url, json=error_payload)

# ================= 4. FastAPI 路由設定 =================
@app.get("/")
def home():
    """保活與測試用的首頁 (UptimeRobot 監控這個 URL)"""
    return {"status": "ok", "message": "Discord Webhook Bot is running!"}

@app.post("/interactions")
async def discord_interactions(request: Request, background_tasks: BackgroundTasks):
    """Discord Webhook 接收主入口"""
    signature = request.headers.get("X-Signature-Ed25519")
    timestamp = request.headers.get("X-Signature-Timestamp")
    body_bytes = await request.body()

    # 安全簽章驗證
    if not signature or not timestamp or not verify_discord_signature(body_bytes, signature, timestamp):
        raise HTTPException(status_code=401, detail="Invalid request signature")

    data = await request.json()

    # 1. 處理 Discord PING (驗證 Endpoint 時使用)
    if data.get("type") == 1:
        return JSONResponse({"type": 1})

    # 2. 處理 Slash Command (例如 /img)
    if data.get("type") == 2:
        command_name = data["data"]["name"]
        
        if command_name == "img":
            options = data["data"].get("options", [])
            keyword = next((opt["value"] for opt in options if opt["name"] == "keyword"), "預設")
            
            interaction_token = data["token"]
            user_id = data.get("member", {}).get("user", {}).get("id") or data.get("user", {}).get("id")

            # 加入背景任務，避免超過 3 秒限制
            background_tasks.add_task(process_image_interaction, interaction_token, user_id, keyword)

            # 立即回覆 Type 5：宣告機器人思考中 (Deferred Channel Message)
            return JSONResponse({
                "type": 5,
                "data": {"content": f"🔍 正在處理「{keyword}」，請稍候..."}
            })

    return JSONResponse({"error": "Unsupported interaction type"}, status_code=400)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)