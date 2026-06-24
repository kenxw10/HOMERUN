from fastapi import Header, HTTPException, status

from app.config import get_settings


def require_internal_api_key(x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.backend_api_key_configured:
        return

    expected = settings.backend_api_key.get_secret_value() if settings.backend_api_key else ""
    if not x_api_key or x_api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Valid X-API-Key header is required for this internal operation.",
        )
