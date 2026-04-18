"""
WeCom (Enterprise WeChat) platform adapter.

Uses the WeCom AI Bot WebSocket gateway for inbound and outbound messages.
The adapter focuses on the core gateway path:

- authenticate via ``aibot_subscribe``
- receive inbound ``aibot_msg_callback`` events
- send outbound markdown messages via ``aibot_send_msg``
- upload outbound media via ``aibot_upload_media_*`` and send native attachments
- best-effort download of inbound image/file attachments for agent context

Configuration in config.yaml:
    platforms:
      wecom:
        enabled: true
        extra:
          bot_id: "your-bot-id"          # or WECOM_BOT_ID env var
          secret: "your-secret"          # or WECOM_SECRET env var
          websocket_url: "wss://openws.work.weixin.qq.com"
          dm_policy: "open"              # open | allowlist | disabled | pairing
          allow_from: ["user_id_1"]
          group_policy: "open"           # open | allowlist | disabled
          group_allow_from: ["group_id_1"]
          groups:
            group_id_1:
              allow_from: ["user_id_1"]
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None  # type: ignore[assignment]

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.mention_router import MentionRouter
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_document_from_bytes,
    cache_image_from_bytes,
)

logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"

APP_CMD_SUBSCRIBE = "aibot_subscribe"
APP_CMD_CALLBACK = "aibot_msg_callback"
APP_CMD_LEGACY_CALLBACK = "aibot_callback"
APP_CMD_EVENT_CALLBACK = "aibot_event_callback"
APP_CMD_SEND = "aibot_send_msg"
APP_CMD_RESPONSE = "aibot_respond_msg"
APP_CMD_PING = "ping"
APP_CMD_UPLOAD_MEDIA_INIT = "aibot_upload_media_init"
APP_CMD_UPLOAD_MEDIA_CHUNK = "aibot_upload_media_chunk"
APP_CMD_UPLOAD_MEDIA_FINISH = "aibot_upload_media_finish"

CALLBACK_COMMANDS = {APP_CMD_CALLBACK, APP_CMD_LEGACY_CALLBACK}
NON_RESPONSE_COMMANDS = CALLBACK_COMMANDS | {APP_CMD_EVENT_CALLBACK}

MAX_MESSAGE_LENGTH = 4000
CONNECT_TIMEOUT_SECONDS = 20.0
REQUEST_TIMEOUT_SECONDS = 15.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]

DEDUP_MAX_SIZE = 1000

IMAGE_MAX_BYTES = 10 * 1024 * 1024
VIDEO_MAX_BYTES = 10 * 1024 * 1024
VOICE_MAX_BYTES = 2 * 1024 * 1024
FILE_MAX_BYTES = 20 * 1024 * 1024
ABSOLUTE_MAX_BYTES = FILE_MAX_BYTES
UPLOAD_CHUNK_SIZE = 512 * 1024
MAX_UPLOAD_CHUNKS = 100
VOICE_SUPPORTED_MIMES = {"audio/amr"}


def check_wecom_requirements() -> bool:
    """Check if WeCom runtime dependencies are available."""
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


def _coerce_list(value: Any) -> List[str]:
    """Coerce config values into a trimmed string list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normalize_entry(raw: str) -> str:
    """Normalize allowlist entries such as ``wecom:user:foo``."""
    value = str(raw).strip()
    value = re.sub(r"^wecom:", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^(user|group):", "", value, flags=re.IGNORECASE)
    return value.strip()


def _entry_matches(entries: List[str], target: str) -> bool:
    """Case-insensitive allowlist match with ``*`` support."""
    normalized_target = str(target).strip().lower()
    for entry in entries:
        normalized = _normalize_entry(entry).lower()
        if normalized == "*" or normalized == normalized_target:
            return True
    return False


def _is_bot_mentioned(body: Dict[str, Any], bot_id: str) -> bool:
    """
    Check if the bot is mentioned in a group chat message.
    
    WeCom sends mentioned user IDs in the `mentioned_userid_list` field.
    """
    if not bot_id:
        return False
    
    # Get mentioned user list from WeCom message
    mentioned_list = body.get("mentioned_userid_list") or []
    if not isinstance(mentioned_list, list):
        mentioned_list = [mentioned_list] if mentioned_list else []
    
    # Check if bot's userid is in the mentioned list
    return bot_id in mentioned_list


class WeComAdapter(BasePlatformAdapter):
    """WeCom AI Bot adapter backed by a persistent WebSocket connection."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    # Threshold for detecting WeCom client-side message splits.
    # When a chunk is near the 4000-char limit, a continuation is almost certain.
    _SPLIT_THRESHOLD = 3900

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WECOM)

        extra = config.extra or {}
        self._bot_id = str(extra.get("bot_id") or os.getenv("WECOM_BOT_ID", "")).strip()
        self._secret = str(extra.get("secret") or os.getenv("WECOM_SECRET", "")).strip()
        self._ws_url = str(
            extra.get("websocket_url")
            or extra.get("websocketUrl")
            or os.getenv("WECOM_WEBSOCKET_URL", DEFAULT_WS_URL)
        ).strip() or DEFAULT_WS_URL

        self._dm_policy = str(extra.get("dm_policy") or os.getenv("WECOM_DM_POLICY", "open")).strip().lower()
        self._allow_from = _coerce_list(extra.get("allow_from") or extra.get("allowFrom"))

        self._group_policy = str(extra.get("group_policy") or os.getenv("WECOM_GROUP_POLICY", "open")).strip().lower()
        self._group_allow_from = _coerce_list(extra.get("group_allow_from") or extra.get("groupAllowFrom"))
        self._groups = extra.get("groups") if isinstance(extra.get("groups"), dict) else {}

        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._http_client: Optional["httpx.AsyncClient"] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._pending_responses: Dict[str, asyncio.Future] = {}
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE)
        self._reply_req_ids: Dict[str, str] = {}

        # Text batching: merge rapid successive messages (Telegram-style).
        # WeCom clients split long messages around 4000 chars.
        self._text_batch_delay_seconds = float(os.getenv("HERMES_WECOM_TEXT_BATCH_DELAY_SECONDS", "0.6"))
        self._text_batch_split_delay_seconds = float(os.getenv("HERMES_WECOM_TEXT_BATCH_SPLIT_DELAY_SECONDS", "2.0"))
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}

        # Multi-agent support: parse @mentions and route to different agents
        # in group chats. Disabled by default 鈥?requires explicit config.
        self._mention_router = MentionRouter.from_wecom_extra(extra)
        self._multi_agent_chains: Dict[str, asyncio.Task] = {}  # chat_id 鈫?chain task

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        """Connect to the WeCom AI Bot gateway."""
        if not AIOHTTP_AVAILABLE:
            message = "WeCom startup failed: aiohttp not installed"
            self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s. Run: pip install aiohttp", self.name, message)
            return False
        if not HTTPX_AVAILABLE:
            message = "WeCom startup failed: httpx not installed"
            self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
            logger.warning("[%s] %s. Run: pip install httpx", self.name, message)
            return False
        if not self._bot_id or not self._secret:
            message = "WeCom startup failed: WECOM_BOT_ID and WECOM_SECRET are required"
            self._set_fatal_error("wecom_missing_credentials", message, retryable=True)
            logger.warning("[%s] %s", self.name, message)
            return False

        try:
            self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
            await self._open_connection()
            self._mark_connected()
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            logger.info("[%s] Connected to %s", self.name, self._ws_url)
            return True
        except Exception as exc:
            message = f"WeCom startup failed: {exc}"
            self._set_fatal_error("wecom_connect_error", message, retryable=True)
            logger.error("[%s] Failed to connect: %s", self.name, exc, exc_info=True)
            await self._cleanup_ws()
            if self._http_client:
                await self._http_client.aclose()
                self._http_client = None
            return False

    async def disconnect(self) -> None:
        """Disconnect from WeCom."""
        self._running = False
        self._mark_disconnected()

        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

    async def _cleanup_ws(self) -> None:
        """Close the live websocket/session, if any."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    def _is_group_allowed(self, chat_id: str, sender_id: str) -> bool:
        """
        Check if a group chat message should be processed based on policy.
        
        Policies:
        - "open": Allow all group messages (but still need @mention)
        - "allowlist": Only allow from specific groups/members
        - "disabled": Block all group messages
        """
        if self._group_policy == "disabled":
            return False
        
        if self._group_policy == "allowlist":
            # Check if group is in allowlist
            if not _entry_matches(self._group_allow_from, chat_id):
                return False
            
            # Check group-specific member allowlist
            group_config = self._groups.get(chat_id, {})
            group_allow = _coerce_list(group_config.get("allow_from"))
            if group_allow and not _entry_matches(group_allow, sender_id):
                return False
        
        return True

    async def _open_connection(self) -> None:
        """Open and authenticate a websocket connection."""
        await self._cleanup_ws()
        self._session = aiohttp.ClientSession(trust_env=True)
        self._ws = await self._session.ws_connect(
            self._ws_url,
            heartbeat=HEARTBEAT_INTERVAL_SECONDS * 2,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )

        req_id = self._new_req_id("subscribe")
        await self._send_json(
            {
                "cmd": APP_CMD_SUBSCRIBE,
                "headers": {"req_id": req_id},
                "body": {"bot_id": self._bot_id, "secret": self._secret},
            }
        )

        auth_payload = await self._wait_for_handshake(req_id)
        errcode = auth_payload.get("errcode", 0)
        if errcode not in (0, None):
            errmsg = auth_payload.get("errmsg", "authentication failed")
            raise RuntimeError(f"{errmsg} (errcode={errcode})")

    async def _wait_for_handshake(self, req_id: str) -> Dict[str, Any]:
        """Wait for the subscribe acknowledgement."""
        if not self._ws:
            raise RuntimeError("WebSocket not initialized")

        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_SECONDS
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for WeCom subscribe acknowledgement")

            msg = await asyncio.wait_for(self._ws.receive(), timeout=remaining)
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if not payload:
                    continue
                if payload.get("cmd") == APP_CMD_PING:
                    continue
                if self._payload_req_id(payload) == req_id:
                    return payload
                logger.debug("[%s] Ignoring pre-auth payload: %s", self.name, payload.get("cmd"))
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                raise RuntimeError("WeCom websocket closed during authentication")

    async def _listen_loop(self) -> None:
        """Read websocket events forever, reconnecting on errors."""
        backoff_idx = 0
        while self._running:
            try:
                await self._read_events()
                backoff_idx = 0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if not self._running:
                    return
                logger.warning("[%s] WebSocket error: %s", self.name, exc)
                self._fail_pending_responses(RuntimeError("WeCom connection interrupted"))

                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                backoff_idx += 1
                await asyncio.sleep(delay)

                try:
                    await self._open_connection()
                    backoff_idx = 0
                    logger.info("[%s] Reconnected", self.name)
                except Exception as reconnect_exc:
                    logger.warning("[%s] Reconnect failed: %s", self.name, reconnect_exc)

    async def _read_events(self) -> None:
        """Read websocket frames until the connection closes."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")

        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if payload:
                    await self._dispatch_payload(payload)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                raise RuntimeError("WeCom websocket closed")

    async def _heartbeat_loop(self) -> None:
        """Send lightweight application-level pings."""
        try:
            while self._running:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if not self._ws or self._ws.closed:
                    continue
                try:
                    await self._send_json(
                        {
                            "cmd": APP_CMD_PING,
                            "headers": {"req_id": self._new_req_id("ping")},
                            "body": {},
                        }
                    )
                except Exception as exc:
                    logger.debug("[%s] Heartbeat send failed: %s", self.name, exc)
        except asyncio.CancelledError:
            pass

    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        """Route inbound websocket payloads."""
        req_id = self._payload_req_id(payload)
        cmd = str(payload.get("cmd") or "")

        if req_id and req_id in self._pending_responses and cmd not in NON_RESPONSE_COMMANDS:
            future = self._pending_responses.get(req_id)
            if future and not future.done():
                future.set_result(payload)
            return

        if cmd in CALLBACK_COMMANDS:
            await self._on_message(payload)
            return
        if cmd in {APP_CMD_PING, APP_CMD_EVENT_CALLBACK}:
            return

        logger.debug("[%s] Ignoring websocket payload: %s", self.name, cmd or payload)

    def _fail_pending_responses(self, exc: Exception) -> None:
        """Fail all outstanding request futures."""
        for req_id, future in list(self._pending_responses.items()):
            if not future.done():
                future.set_exception(exc)
            self._pending_responses.pop(req_id, None)

    async def _send_json(self, payload: Dict[str, Any]) -> None:
        """Send a raw JSON frame over the active websocket."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")
        await self._ws.send_json(payload)

    async def _send_request(self, cmd: str, body: Dict[str, Any], timeout: float = REQUEST_TIMEOUT_SECONDS) -> Dict[str, Any]:
        """Send a JSON request and await the correlated response."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")

        req_id = self._new_req_id(cmd)
        future = asyncio.get_running_loop().create_future()
        self._pending_responses[req_id] = future
        try:
            await self._send_json({"cmd": cmd, "headers": {"req_id": req_id}, "body": body})
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        finally:
            self._pending_responses.pop(req_id, None)

    async def _send_reply_request(
        self,
        reply_req_id: str,
        body: Dict[str, Any],
        cmd: str = APP_CMD_RESPONSE,
        timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        """Send a reply frame correlated to an inbound callback req_id."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")

        normalized_req_id = str(reply_req_id or "").strip()
        if not normalized_req_id:
            raise ValueError("reply_req_id is required")

        future = asyncio.get_running_loop().create_future()
        self._pending_responses[normalized_req_id] = future
        try:
            await self._send_json(
                {"cmd": cmd, "headers": {"req_id": normalized_req_id}, "body": body}
            )
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        finally:
            self._pending_responses.pop(normalized_req_id, None)

    @staticmethod
    def _new_req_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    @staticmethod
    def _payload_req_id(payload: Dict[str, Any]) -> str:
        headers = payload.get("headers")
        if isinstance(headers, dict):
            return str(headers.get("req_id") or "")
        return ""

    @staticmethod
    def _parse_json(raw: Any) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(raw)
        except Exception:
            logger.debug("Failed to parse WeCom payload: %r", raw)
            return None
        return payload if isinstance(payload, dict) else None

    # ------------------------------------------------------------------
    # Inbound message parsing
    # ------------------------------------------------------------------

    async def _on_message(self, payload: Dict[str, Any]) -> None:
        """Process an inbound WeCom message callback event."""
        body = payload.get("body")
        if not isinstance(body, dict):
            return

        msg_id = str(body.get("msgid") or self._payload_req_id(payload) or uuid.uuid4().hex)
        if self._dedup.is_duplicate(msg_id):
            logger.debug("[%s] Duplicate message %s ignored", self.name, msg_id)
            return
        self._remember_reply_req_id(msg_id, self._payload_req_id(payload))

        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
        sender_id = str(sender.get("userid") or "").strip()
        chat_id = str(body.get("chatid") or sender_id).strip()
        if not chat_id:
            logger.debug("[%s] Missing chat id, skipping message", self.name)
            return

        is_group = str(body.get("chattype") or "").lower() == "group"
        if is_group:
            if not self._is_group_allowed(chat_id, sender_id):
                logger.debug("[%s] Group %s / sender %s blocked by policy", self.name, chat_id, sender_id)
                return

        # 鑾峰彇娑堟伅鍐呭
        content = str(body.get("content") or "").strip()
        msg_type = str(body.get("msgtype") or "text").lower()

        # 缇よ亰涓渶瑕佹鏌ユ満鍣ㄤ汉鏄惁琚?@
        if is_group:
            # 棣栧厛妫€鏌ユ槸鍚﹂€氳繃 mentioned_userid_list 琚?@
            is_mentioned = _is_bot_mentioned(body, self._bot_id)
            
            # 濡傛灉娌℃湁琚?@锛屾鏌ユ槸鍚﹂€氳繃鏂囨湰 @mention 鍖归厤锛堝 Agent 妯″紡锛?            if not is_mentioned:
                target_agents = self._mention_router.resolve_target_agents(content)
                if not target_agents:
                    logger.debug(
                        "[%s] Bot not mentioned in group chat %s, ignoring. "
                        "bot_id=%s, content=%r",
                        self.name, chat_id, self._bot_id, content[:100]
                    )
                    return
                logger.debug("[%s] Matched agents via mention_router: %s", self.name, target_agents)
            else:
                logger.debug("[%s] Bot was @mentioned in group chat %s", self.name, chat_id)
