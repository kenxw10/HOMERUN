from secrets import compare_digest

from fastapi import Header, HTTPException, status

from app.config import get_settings

LOCAL_AUTH_BYPASS_ENVS = {"local", "dev", "development", "test"}


def require_internal_api_key(x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.backend_api_key_configured:
        if settings.app_env.strip().lower() in LOCAL_AUTH_BYPASS_ENVS:
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="BACKEND_API_KEY must be configured for internal operations outside local development.",
        )

    expected = settings.backend_api_key.get_secret_value() if settings.backend_api_key else ""
    if not x_api_key or not compare_digest(x_api_key, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid X-API-Key header is required for this internal operation.",
        )
