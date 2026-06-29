from __future__ import annotations

import argparse
import asyncio
import base64
import inspect
import json
import time
from collections.abc import Callable
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


def _update_subscription_message(message_id: int, sids: list[int], tickers: list[str]) -> dict[str, object]:
    return {
        "id": message_id,
        "cmd": "update_subscription",
        "params": {
            "sids": sids,
            "market_tickers": tickers,
            "action": "add_markets",
        },
    }


def _message_type(payload: dict[str, object]) -> str:
    value = payload.get("type")
    return value.strip().lower() if isinstance(value, str) else ""


def _message_id(payload: dict[str, object]) -> int | None:
    value = payload.get("id")
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _subscription_sid(payload: dict[str, object]) -> int | None:
    message = payload.get("msg")
    data = message if isinstance(message, dict) else payload
    value = data.get("sid")
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _market_update_payload(payload: dict[str, object]) -> tuple[str | None, dict[str, object]]:
    message = payload.get("msg")
    update_payload = message if isinstance(message, dict) else payload
    return _extract_ticker(update_payload) or _extract_ticker(payload), update_payload


def _websocket_path(ws_url: str) -> str:
    parsed = urlparse(ws_url)
    return parsed.path or "/trade-api/ws/v2"


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


def _websocket_connect_kwargs(connect: Callable[..., object], auth_headers: dict[str, str]) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "ping_interval": 20,
        "ping_timeout": 20,
    }
    try:
        parameters = inspect.signature(connect).parameters
    except (TypeError, ValueError):
        parameters = {}
    header_arg = "extra_headers" if "extra_headers" in parameters and "additional_headers" not in parameters else "additional_headers"
    kwargs[header_arg] = auth_headers
    return kwargs


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
            **_websocket_connect_kwargs(websockets.connect, auth_headers),
        ) as websocket:
            await websocket.send(json.dumps(subscribe_message))
            with session_factory() as session:
                mark_ws_status(session, running=True, subscribed_market_count=len(tickers), source="websocket")
                session.commit()
            skipped_messages = 0
            applied_updates = 0
            subscription_refreshes = 0
            active_subscription_count = len(tickers)
            subscribed_tickers = {ticker.upper() for ticker in tickers}
            subscription_sids: set[int] = set()
            pending_subscription_updates: dict[int, set[str]] = {}
            next_message_id = 2
            subscription_refresh_interval = max(1, settings.ws_heartbeat_timeout_seconds)
            loop = asyncio.get_running_loop()
            next_subscription_refresh = loop.time() + subscription_refresh_interval
            last_ticker: str | None = None
            last_result: dict[str, object] | None = None
            while True:
                try:
                    raw = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=settings.ws_heartbeat_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    break
                payload = json.loads(raw)
                payload_dict = payload if isinstance(payload, dict) else {}
                message_type = _message_type(payload_dict)
                if message_type == "subscribed":
                    sid = _subscription_sid(payload_dict)
                    if sid is not None:
                        subscription_sids.add(sid)
                elif message_type == "ok":
                    ack_id = _message_id(payload_dict)
                    if ack_id in pending_subscription_updates:
                        subscribed_tickers.update(pending_subscription_updates.pop(ack_id))
                        subscription_refreshes += 1
                        active_subscription_count = len(subscribed_tickers)
                        with session_factory() as session:
                            mark_ws_status(
                                session,
                                running=True,
                                subscribed_market_count=active_subscription_count,
                                source="websocket",
                            )
                            session.commit()
                elif message_type == "error":
                    ack_id = _message_id(payload_dict)
                    if ack_id in pending_subscription_updates:
                        pending_subscription_updates.pop(ack_id)

                ticker, update_payload = _market_update_payload(payload_dict)
                if ticker:
                    with session_factory() as session:
                        result = apply_ws_market_update(session, ticker, update_payload)
                        session.commit()
                    applied_updates += 1
                    last_ticker = ticker
                    last_result = result
                else:
                    skipped_messages += 1
                if loop.time() >= next_subscription_refresh:
                    with session_factory() as session:
                        refreshed_tickers = active_ws_tickers(session)
                        mark_ws_status(
                            session,
                            running=True,
                            subscribed_market_count=len(subscribed_tickers),
                            source="websocket",
                        )
                        session.commit()
                    active_subscription_count = len(subscribed_tickers)
                    new_tickers = [ticker for ticker in refreshed_tickers if ticker.upper() not in subscribed_tickers]
                    if new_tickers and subscription_sids:
                        message_id = next_message_id
                        next_message_id += 1
                        pending_subscription_updates[message_id] = {ticker.upper() for ticker in new_tickers}
                        await websocket.send(
                            json.dumps(_update_subscription_message(message_id, sorted(subscription_sids), new_tickers))
                        )
                    if not refreshed_tickers:
                        break
                    next_subscription_refresh = loop.time() + subscription_refresh_interval
            if applied_updates:
                return {
                    "status": "message_applied",
                    "ticker": last_ticker,
                    "result": last_result or {},
                    "applied_updates": applied_updates,
                    "skipped_messages": skipped_messages,
                    "subscription_refreshes": subscription_refreshes,
                    "subscribed_market_count": active_subscription_count,
                }
            return {
                "status": "message_without_ticker",
                "subscribed_market_count": active_subscription_count,
                "skipped_messages": skipped_messages,
                "subscription_refreshes": subscription_refreshes,
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
