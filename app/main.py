from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.core.config import get_settings
from app.api import webhooks, messages, queue, conversations, analytics, billing, settings, chat_ws, voice_webhook, voice_ws, sms_chat, voice_retell_webhook, voice_provision, admin, email_inbound
from app.core.security import require_admin

ADMIN = [Depends(require_admin)]

settings_obj = get_settings()

app = FastAPI(
    title="FrontdeskReply API",
    description="AI-powered lead response and intake assistant for home service businesses",
    version="0.1.0",
    docs_url=None,  # never expose OpenAPI docs in production
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://app.frontdeskreply.com",
        "https://pawtyyoga.com",
        "https://www.pawtyyoga.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(webhooks.router,     prefix="/api/v1")
app.include_router(messages.router,     prefix="/api/v1", dependencies=ADMIN)
app.include_router(queue.router,        prefix="/api/v1", dependencies=ADMIN)
app.include_router(conversations.router,prefix="/api/v1", dependencies=ADMIN)
app.include_router(analytics.router,    prefix="/api/v1", dependencies=ADMIN)
app.include_router(billing.router,      prefix="/api/v1")
app.include_router(settings.router,     prefix="/api/v1")
app.include_router(chat_ws.router)  # WebSocket + chat admin endpoints (no prefix — WS at /ws/chat/{id})
app.include_router(voice_webhook.router, prefix="/api/v1")  # Twilio voice webhook
app.include_router(voice_ws.router)  # Voice WebSocket + call data endpoints
app.include_router(sms_chat.router, prefix="/api/v1")  # SMS chat with Milo
app.include_router(voice_retell_webhook.router, prefix="/api/v1")  # Retell AI webhook for call transcripts
app.include_router(voice_provision.router, prefix="/api/v1", dependencies=ADMIN)  # Voice AI self-service provisioning
app.include_router(admin.router, prefix="/api/v1", dependencies=ADMIN)  # Admin dashboard for DJ
app.include_router(email_inbound.router, prefix="/api/v1")  # Email auto-reply (all plans)
# whatsapp, facebook and the voice_retell custom-LLM websocket are unmounted (unused; Pawty voice = Retell LLM over SIP)

@app.get("/health")
def health():
    """Real health check for an uptime monitor: 200 only if the database answers."""
    from fastapi.responses import JSONResponse
    from app.core.database import get_db
    try:
        get_db().table("businesses").select("id").limit(1).execute()
        return {"status": "ok"}
    except Exception:
        return JSONResponse({"status": "degraded", "db": "unreachable"}, status_code=503)

@app.get("/")
def root():
    return {"product": "FrontdeskReply"}