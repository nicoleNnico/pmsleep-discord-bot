import os

from fastapi import FastAPI

from interactions import router


app = FastAPI()
app.include_router(router)


# ================= 環境變數狀態 =================
DISCORD_PUBLIC_KEY = os.environ.get("DISCORD_PUBLIC_KEY")
DISCORD_APP_ID = os.environ.get("DISCORD_APP_ID")
print(
    "啟動：環境變數狀態 "
    f"DISCORD_PUBLIC_KEY={bool(DISCORD_PUBLIC_KEY)} "
    f"DISCORD_APP_ID={bool(DISCORD_APP_ID)}",
    flush=True,
)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
