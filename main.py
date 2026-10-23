import asyncio
from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI

from scheduled_messages import run_scheduled_messages
from interactions import router


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduled_task = asyncio.create_task(
        run_scheduled_messages(),
        name="scheduled-messages",
    )

    def log_task_failure(task: asyncio.Task) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logging.getLogger(__name__).error(
                "定時訊息排程停止；error_type=%s error=%s",
                type(error).__name__,
                error,
                exc_info=(type(error), error, error.__traceback__),
            )

    scheduled_task.add_done_callback(log_task_failure)
    try:
        yield
    finally:
        scheduled_task.cancel()
        try:
            await scheduled_task
        except asyncio.CancelledError:
            pass


app = FastAPI(lifespan=lifespan)
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
