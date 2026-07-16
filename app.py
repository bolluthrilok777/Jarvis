import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI

from jarvis_core.config import APP_NAME
from jarvis_core.logging_config import setup_logging
from jarvis_core.routes import router
from jarvis_core.voice_routes import router as voice_router

setup_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: begin the task worker. Shutdown: stop the task worker."""
    from jarvis_core.orchestrator import orchestrator
    orchestrator.start_worker()
    yield
    orchestrator.stop_worker()


app = FastAPI(title=APP_NAME, lifespan=lifespan)
app.include_router(router)
app.include_router(voice_router)

if __name__ == "__main__":
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)