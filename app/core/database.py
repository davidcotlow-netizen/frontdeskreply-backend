from supabase import create_client, Client
from app.core.config import get_settings
from functools import lru_cache

_client: Client | None = None


def get_db() -> Client:
    global _client
    if _client is None:
        settings = get_settings()
        _client = create_client(settings.supabase_url, settings.supabase_service_key)
    return _client
