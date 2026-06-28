from __future__ import annotations

import argparse
import asyncio
import json

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

    subscribe_message = {
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["market_ticker", "orderbook_delta"],
            "market_tickers": tickers,
        },
    }
    try:
        async with websockets.connect(settings.kalshi_ws_base_url, ping_interval=20, ping_timeout=20) as websocket:
            await websocket.send(json.dumps(subscribe_message))
            with session_factory() as session:
                mark_ws_status(session, running=True, subscribed_market_count=len(tickers), source="websocket")
                session.commit()
            raw = await asyncio.wait_for(websocket.recv(), timeout=settings.ws_heartbeat_timeout_seconds)
            payload = json.loads(raw)
            ticker = _extract_ticker(payload)
            if ticker:
                with session_factory() as session:
                    result = apply_ws_market_update(session, ticker, payload if isinstance(payload, dict) else {})
                    session.commit()
                return {"status": "message_applied", "ticker": ticker, "result": result}
            return {"status": "message_without_ticker", "subscribed_market_count": len(tickers)}
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
