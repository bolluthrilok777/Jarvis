import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

from jarvis_core.config import APP_NAME
from jarvis_core.logging_config import setup_logging
from jarvis_core.routes import router
from jarvis_core.voice_routes import router as voice_router
from jarvis_core.gmail_routes import router as gmail_router
from jarvis_core.gmail_monitor import load_config as load_gmail_config

setup_logging()
load_gmail_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Start and stop Jarvis background workers with the application."""
    from jarvis_core.orchestrator import orchestrator
    from jarvis_core.telegram_service import telegram_service

    orchestrator.start_worker()
    telegram_service.start()
    yield
    from jarvis_core.monitoring import monitoring

    telegram_service.stop()
    monitoring.stop()
    orchestrator.stop_worker()


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.include_router(router)
app.include_router(voice_router)
app.include_router(gmail_router)

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)
