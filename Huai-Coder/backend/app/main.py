from contextlib import asynccontextmanager
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
import json
from .agent import run_agent
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from .config import get_settings
from .database import Base, engine, get_db

settings = get_settings()
@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()

app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origin_list, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.get("/health")
async def health(db: AsyncSession = Depends(get_db)):
    await db.execute(text("SELECT 1"))
    return {"status": "ok"}

class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)

@app.post("/api/runs")
async def create_run(request: RunRequest):
    async def events():
        async for event in run_agent(request.prompt):
            yield f"data: {json.dumps({'type': event.type, 'content': event.content, 'tool': event.tool}, ensure_ascii=False)}\n\n"
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
