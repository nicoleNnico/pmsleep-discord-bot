from fastapi import FastAPI

from config import DISCORD_APP_ID, DISCORD_PUBLIC_KEY, PORT
from interactions import router


app = FastAPI()
app.include_router(router)



if __name__ == "__main__":
    import uvicorn

    port = int(PORT)
    uvicorn.run("main:app", host="0.0.0.0", port=port)
