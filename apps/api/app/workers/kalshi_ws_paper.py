from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import get_settings
from app.database import get_session_factory
from app.services.ws_market_data import active_ws_tickers, apply_ws_market_update, mark_ws_status


def _extract_ticker(payload: dict[str, object]) -> str | None:
    for key in ("market_ticker", "ticker", "sid"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value.upper()
    message = payload.get("msg")
    if isinstance(message, dict):
        return _extract_ticker(message)
    return None


def _subscribe_message(tickers: list[str]) -> dict[str, object]:
    return {
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["ticker", "orderbook_delta"],
            "market_tickers": tickers,
        },
    }


def _market_update_payload(payload: dict[str, object]) -> tuple[str | None, dict[str, object]]:
    message = payload.get("msg")
    update_payload = message if isinstance(message, dict) else payload
    return _extract_ticker(update_payload) or _extract_ticker(payload), update_payload


def _websocket_path(ws_url: str) -> str:
    parsed = urlparse(ws_url)
    path = parsed.path or "/trade-api/ws/v2"
    return f"{path}?{parsed.query}" if parsed.query else path


def _websocket_auth_headers(api_key: str, api_secret: str, ws_url: str, *, timestamp_ms: str | None = None) -> dict[str, str]:
    timestamp = timestamp_ms or str(int(time.time() * 1000))
    path = _websocket_path(ws_url)
    message = f"{timestamp}GET{path}".encode("utf-8")
    private_key = serialization.load_pem_private_key(api_secret.replace("\\n", "\n").encode("utf-8"), password=None)
    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


async def _run_worker_once() -> dict[str, object]:
    settings = get_settings()
    session_factory = get_session_factory()
    with session_factory() as session:
        tickers = active_ws_tickers(session)
        mark_ws_status(
            session,
            running=settings.websocket_market_data_enabled,
            subscribed_market_count=len(tickers),
            source="websocket" if settings.websocket_market_data_enabled else "rest_fallback",
        )
        session.commit()

    if not settings.websocket_market_data_enabled:
        return {"status": "disabled", "source": "rest_fallback", "subscribed_market_count": len(tickers)}
    if not tickers:
        return {"status": "idle_no_markets", "source": "websocket", "subscribed_market_count": 0}

    if not settings.kalshi_credentials_configured:
        with session_factory() as session:
            mark_ws_status(
                session,
                running=False,
                subscribed_market_count=len(tickers),
                error="Kalshi WebSocket credentials are not configured.",
                source="rest_fallback",
            )
            session.commit()
        return {"status": "websocket_credentials_missing", "source": "rest_fallback"}

    try:
        import websockets  # type: ignore
    except Exception as exc:
        with session_factory() as session:
            mark_ws_status(
                session,
                running=False,
                subscribed_market_count=len(tickers),
                error=f"websockets import unavailable: {exc}",
                source="rest_fallback",
            )
            session.commit()
        return {"status": "websocket_unavailable", "source": "rest_fallback", "error": str(exc)}

    subscribe_message = _subscribe_message(tickers)
    try:
        auth_headers = _websocket_auth_headers(
            settings.kalshi_api_key.get_secret_value() if settings.kalshi_api_key else "",
            settings.kalshi_api_secret.get_secret_value() if settings.kalshi_api_secret else "",
            settings.kalshi_ws_base_url,
        )
        async with websockets.connect(
            settings.kalshi_ws_base_url,
            ping_interval=20,
            ping_timeout=20,
            additional_headers=auth_headers,
        ) as websocket:
            await websocket.send(json.dumps(subscribe_message))
            with session_factory() as session:
                mark_ws_status(session, running=True, subscribed_market_count=len(tickers), source="websocket")
                session.commit()
            skipped_messages = 0
            loop = asyncio.get_running_loop()
            deadline = loop.time() + settings.ws_heartbeat_timeout_seconds
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                payload = json.loads(raw)
                ticker, update_payload = _market_update_payload(payload if isinstance(payload, dict) else {})
                if ticker:
                    with session_factory() as session:
                        result = apply_ws_market_update(session, ticker, update_payload)
                        session.commit()
                    return {
                        "status": "message_applied",
                        "ticker": ticker,
                        "result": result,
                        "skipped_messages": skipped_messages,
                    }
                skipped_messages += 1
            return {
                "status": "message_without_ticker",
                "subscribed_market_count": len(tickers),
                "skipped_messages": skipped_messages,
            }
    except Exception as exc:
        with session_factory() as session:
            mark_ws_status(
                session,
                running=False,
                subscribed_market_count=len(tickers),
                error=str(exc),
                reconnect_increment=True,
                source="rest_fallback",
            )
            session.commit()
        return {"status": "failed", "source": "rest_fallback", "error": str(exc)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Paper-safe Kalshi WebSocket market-data worker.")
    parser.add_argument("--once", action="store_true", help="Run one subscribe/read cycle and exit.")
    args = parser.parse_args()

    if args.once:
        print(asyncio.run(_run_worker_once()))
        return

    settings = get_settings()
    while True:
        result = asyncio.run(_run_worker_once())
        print(result)
        if not settings.websocket_market_data_enabled:
            return
        asyncio.run(asyncio.sleep(settings.ws_reconnect_backoff_seconds))


if __name__ == "__main__":
    main()
