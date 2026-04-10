from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    # Anthropic
    anthropic_api_key: str
    claude_model: str = "claude-sonnet-4-6"
    confidence_threshold: float = 0.75
    max_draft_tokens: int = 200
    max_classify_tokens: int = 350

    # Supabase
    supabase_url: str
    supabase_service_key: str

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Resend
    resend_api_key: str = ""

    # Gmail SMTP (legacy — superseded by Resend)
    gmail_user: str = ""
    gmail_app_password: str = ""

    # Clerk
    clerk_secret_key: str = ""

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Live Chat
    chat_confidence_threshold: float = 0.7
    chat_session_timeout_minutes: int = 30
    chat_max_context_messages: int = 20

    # Voice AI
    twilio_voice_number: str = ""
    voice_welcome_greeting: str = "Hi! I'm Vela. How can I help you today?"
    voice_max_call_minutes: int = 10
    retell_api_key: str = "key_2b33b7e079f15e3c8351b40ad0ea"
    twilio_sip_trunk_sid: str = "TK2047892d4b39a8cdd5ffca1a17b8cab1"

    # App
    app_env: str = "development"
    frontend_url: str = "http://localhost:3000"
    api_secret_key: str = "change-me"

    class Config:
        env_file = ".env"
        case_sensitive = False



def get_settings() -> Settings:
    return Settings()
