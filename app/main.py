from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import get_settings
from app.api import webhooks, messages, queue, conversations, analytics, billing, settings

settings_obj = get_settings()

app = FastAPI(
    title="FrontdeskReply API",
    description="AI-powered lead response and intake assistant for home service businesses",
    version="0.1.0",
    docs_url="/docs" if settings_obj.app_env == "development" else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app.frontdeskreply.com"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhooks.router,     prefix="/api/v1")
app.include_router(messages.router,     prefix="/api/v1")
app.include_router(queue.router,        prefix="/api/v1")
app.include_router(conversations.router,prefix="/api/v1")
app.include_router(analytics.router,    prefix="/api/v1")
app.include_router(billing.router,      prefix="/api/v1")
app.include_router(settings.router,     prefix="/api/v1")

@app.get("/health")
def health():
    return {"status": "ok", "env": settings_obj.app_env}

@app.get("/")
def root():
    return {"product": "FrontdeskReply", "version": "0.1.0", "docs": "/docs"}