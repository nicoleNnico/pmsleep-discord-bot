import os
import json
from pathlib import Path

import requests
from dotenv import load_dotenv

# Discord 斜線指令（Slash Command）需要透過 Discord API 發送一個 HTTP POST 請求，該指令才會被註冊。
# 這個檔案只需要在你的電腦本機執行一次即可。

load_dotenv(Path(__file__).resolve().with_name(".env"))

DISCORD_APP_ID = os.getenv("DISCORD_APP_ID")
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

if not DISCORD_APP_ID or not DISCORD_BOT_TOKEN:
    raise RuntimeError("請在專案根目錄的 .env 設定 DISCORD_APP_ID 和 DISCORD_BOT_TOKEN")

url = f"https://discord.com/api/v10/applications/{DISCORD_APP_ID}/commands"

headers = {
    "Authorization": f"Bot {DISCORD_BOT_TOKEN}",
    "Content-Type": "application/json"
}

image_urls_path = Path(__file__).resolve().parent / "assets" / "image_urls.json"
with image_urls_path.open(encoding="utf-8") as config_file:
    image_urls = json.load(config_file)

if not isinstance(image_urls, dict):
    raise ValueError(f"圖片網址清單必須是 JSON object：{image_urls_path}")

# 清除全域指令
# 後面的程式碼會重新註冊新的指令, 所以這裡先清除舊的指令.
url_global = f"https://discord.com/api/v10/applications/{DISCORD_APP_ID}/commands"
response_global = requests.put(url_global, headers=headers, json=[])
print("清除全域指令狀態：", response_global.status_code)


commands = [
    *(
        {
            "name": keyword,
            "description": f"回傳{keyword}圖片網址",
        }
        for keyword in image_urls
    ),
    {
        "name": "echo",
        "description": "讓機器人在群組中發送指定文字",
        "default_member_permissions": "8",
        "options": [
            {
                "name": "text",
                "description": "要發送的文字",
                "type": 3,  # STRING 類型
                "required": True,
                "max_length": 2000
            }
        ]
    }
]

for command in commands:
    response = requests.post(url, json=command, headers=headers)
    print(f"Command: {command['name']}")
    print("Status Code:", response.status_code)
    print("Response:", response.json())