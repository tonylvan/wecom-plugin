     1|"""
     2|WeCom (Enterprise WeChat) platform adapter.
     3|
     4|Uses the WeCom AI Bot WebSocket gateway for inbound and outbound messages.
     5|The adapter focuses on the core gateway path:
     6|
     7|- authenticate via ``aibot_subscribe``
     8|- receive inbound ``aibot_msg_callback`` events
     9|- send outbound markdown messages via ``aibot_send_msg``
    10|- upload outbound media via ``aibot_upload_media_*`` and send native attachments
    11|- best-effort download of inbound image/file attachments for agent context
    12|
    13|Configuration in config.yaml:
    14|    platforms:
    15|      wecom:
    16|        enabled: true
    17|        extra:
    18|          bot_id: "your-bot-id"          # or WECOM_BOT_ID env var
    19|          secret: "your-secret"          # or WECOM_SECRET env var
    20|          websocket_url: "wss://openws.work.weixin.qq.com"
    21|          dm_policy: "open"              # open | allowlist | disabled | pairing
    22|          allow_from: ["user_id_1"]
    23|          group_policy: "open"           # open | allowlist | disabled
    24|          group_allow_from: ["group_id_1"]
    25|          groups:
    26|            group_id_1:
    27|              allow_from: ["user_id_1"]
    28|"""
    29|
    30|from __future__ import annotations
    31|
    32|import asyncio
    33|import base64
    34|import hashlib
    35|import json
    36|import logging
    37|import mimetypes
    38|import os
    39|import re
    40|import uuid
    41|from datetime import datetime, timezone
    42|from pathlib import Path
    43|from typing import Any, Dict, List, Optional, Tuple
    44|from urllib.parse import unquote, urlparse
    45|
    46|try:
    47|    import aiohttp
    48|    AIOHTTP_AVAILABLE = True
    49|except ImportError:
    50|    AIOHTTP_AVAILABLE = False
    51|    aiohttp = None  # type: ignore[assignment]
    52|
    53|try:
    54|    import httpx
    55|    HTTPX_AVAILABLE = True
    56|except ImportError:
    57|    HTTPX_AVAILABLE = False
    58|    httpx = None  # type: ignore[assignment]
    59|
    60|from gateway.config import Platform, PlatformConfig
    61|from gateway.platforms.helpers import MessageDeduplicator
    62|from gateway.platforms.mention_router import MentionRouter
    63|from gateway.platforms.base import (
    64|    BasePlatformAdapter,
    65|    MessageEvent,
    66|    MessageType,
    67|    SendResult,
    68|    cache_document_from_bytes,
    69|    cache_image_from_bytes,
    70|)
    71|
    72|logger = logging.getLogger(__name__)
    73|
    74|DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"
    75|
    76|APP_CMD_SUBSCRIBE = "aibot_subscribe"
    77|APP_CMD_CALLBACK = "aibot_msg_callback"
    78|APP_CMD_LEGACY_CALLBACK = "aibot_callback"
    79|APP_CMD_EVENT_CALLBACK = "aibot_event_callback"
    80|APP_CMD_SEND = "aibot_send_msg"
    81|APP_CMD_RESPONSE = "aibot_respond_msg"
    82|APP_CMD_PING = "ping"
    83|APP_CMD_UPLOAD_MEDIA_INIT = "aibot_upload_media_init"
    84|APP_CMD_UPLOAD_MEDIA_CHUNK = "aibot_upload_media_chunk"
    85|APP_CMD_UPLOAD_MEDIA_FINISH = "aibot_upload_media_finish"
    86|
    87|CALLBACK_COMMANDS = {APP_CMD_CALLBACK, APP_CMD_LEGACY_CALLBACK}
    88|NON_RESPONSE_COMMANDS = CALLBACK_COMMANDS | {APP_CMD_EVENT_CALLBACK}
    89|
    90|MAX_MESSAGE_LENGTH = 4000
    91|CONNECT_TIMEOUT_SECONDS = 20.0
    92|REQUEST_TIMEOUT_SECONDS = 15.0
    93|HEARTBEAT_INTERVAL_SECONDS = 30.0
    94|RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
    95|
    96|DEDUP_MAX_SIZE = 1000
    97|
    98|IMAGE_MAX_BYTES = 10 * 1024 * 1024
    99|VIDEO_MAX_BYTES = 10 * 1024 * 1024
   100|VOICE_MAX_BYTES = 2 * 1024 * 1024
   101|FILE_MAX_BYTES = 20 * 1024 * 1024
   102|ABSOLUTE_MAX_BYTES = FILE_MAX_BYTES
   103|UPLOAD_CHUNK_SIZE = 512 * 1024
   104|MAX_UPLOAD_CHUNKS = 100
   105|VOICE_SUPPORTED_MIMES = {"audio/amr"}
   106|
   107|
   108|def check_wecom_requirements() -> bool:
   109|    """Check if WeCom runtime dependencies are available."""
   110|    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE
   111|
   112|
   113|def _coerce_list(value: Any) -> List[str]:
   114|    """Coerce config values into a trimmed string list."""
   115|    if value is None:
   116|        return []
   117|    if isinstance(value, str):
   118|        return [item.strip() for item in value.split(",") if item.strip()]
   119|    if isinstance(value, (list, tuple, set)):
   120|        return [str(item).strip() for item in value if str(item).strip()]
   121|    return [str(value).strip()] if str(value).strip() else []
   122|
   123|
   124|def _normalize_entry(raw: str) -> str:
   125|    """Normalize allowlist entries such as ``wecom:user:foo``."""
   126|    value = str(raw).strip()
   127|    value = re.sub(r"^wecom:", "", value, flags=re.IGNORECASE)
   128|    value = re.sub(r"^(user|group):", "", value, flags=re.IGNORECASE)
   129|    return value.strip()
   130|
   131|
   132|def _entry_matches(entries: List[str], target: str) -> bool:
   133|    """Case-insensitive allowlist match with ``*`` support."""
   134|    normalized_target = str(target).strip().lower()
   135|    for entry in entries:
   136|        normalized = _normalize_entry(entry).lower()
   137|        if normalized == "*" or normalized == normalized_target:
   138|            return True
   139|    return False
   140|
   141|
   142|class WeComAdapter(BasePlatformAdapter):
   143|    """WeCom AI Bot adapter backed by a persistent WebSocket connection."""
   144|
   145|    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
   146|    # Threshold for detecting WeCom client-side message splits.
   147|    # When a chunk is near the 4000-char limit, a continuation is almost certain.
   148|    _SPLIT_THRESHOLD = 3900
   149|
   150|    def __init__(self, config: PlatformConfig):
   151|        super().__init__(config, Platform.WECOM)
   152|
   153|        extra = config.extra or {}
   154|        self._bot_id = str(extra.get("bot_id") or os.getenv("WECOM_BOT_ID", "")).strip()
   155|        self._secret = str(extra.get("secret") or os.getenv("WECOM_SECRET", "")).strip()
   156|        self._ws_url = str(
   157|            extra.get("websocket_url")
   158|            or extra.get("websocketUrl")
   159|            or os.getenv("WECOM_WEBSOCKET_URL", DEFAULT_WS_URL)
   160|        ).strip() or DEFAULT_WS_URL
   161|
   162|        self._dm_policy = str(extra.get("dm_policy") or os.getenv("WECOM_DM_POLICY", "open")).strip().lower()
   163|        self._allow_from = _coerce_list(extra.get("allow_from") or extra.get("allowFrom"))
   164|
   165|        self._group_policy = str(extra.get("group_policy") or os.getenv("WECOM_GROUP_POLICY", "open")).strip().lower()
   166|        self._group_allow_from = _coerce_list(extra.get("group_allow_from") or extra.get("groupAllowFrom"))
   167|        self._groups = extra.get("groups") if isinstance(extra.get("groups"), dict) else {}
   168|
   169|        self._session: Optional["aiohttp.ClientSession"] = None
   170|        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
   171|        self._http_client: Optional["httpx.AsyncClient"] = None
   172|        self._listen_task: Optional[asyncio.Task] = None
   173|        self._heartbeat_task: Optional[asyncio.Task] = None
   174|        self._pending_responses: Dict[str, asyncio.Future] = {}
   175|        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE)
   176|        self._reply_req_ids: Dict[str, str] = {}
   177|
   178|        # Text batching: merge rapid successive messages (Telegram-style).
   179|        # WeCom clients split long messages around 4000 chars.
   180|        self._text_batch_delay_seconds = float(os.getenv("HERMES_WECOM_TEXT_BATCH_DELAY_SECONDS", "0.6"))
   181|        self._text_batch_split_delay_seconds = float(os.getenv("HERMES_WECOM_TEXT_BATCH_SPLIT_DELAY_SECONDS", "2.0"))
   182|        self._pending_text_batches: Dict[str, MessageEvent] = {}
   183|        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}
   184|
   185|        # Multi-agent support: parse @mentions and route to different agents
   186|        # in group chats. Disabled by default — requires explicit config.
   187|        self._mention_router = MentionRouter.from_wecom_extra(extra)
   188|        self._multi_agent_chains: Dict[str, asyncio.Task] = {}  # chat_id → chain task
   189|
   190|    # ------------------------------------------------------------------
   191|    # Connection lifecycle
   192|    # ------------------------------------------------------------------
   193|
   194|    async def connect(self) -> bool:
   195|        """Connect to the WeCom AI Bot gateway."""
   196|        if not AIOHTTP_AVAILABLE:
   197|            message = "WeCom startup failed: aiohttp not installed"
   198|            self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
   199|            logger.warning("[%s] %s. Run: pip install aiohttp", self.name, message)
   200|            return False
   201|        if not HTTPX_AVAILABLE:
   202|            message = "WeCom startup failed: httpx not installed"
   203|            self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
   204|            logger.warning("[%s] %s. Run: pip install httpx", self.name, message)
   205|            return False
   206|        if not self._bot_id or not self._secret:
   207|            message = "WeCom startup failed: WECOM_BOT_ID and WECOM_SECRET are required"
   208|            self._set_fatal_error("wecom_missing_credentials", message, retryable=True)
   209|            logger.warning("[%s] %s", self.name, message)
   210|            return False
   211|
   212|        try:
   213|            self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
   214|            await self._open_connection()
   215|            self._mark_connected()
   216|            self._listen_task = asyncio.create_task(self._listen_loop())
   217|            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
   218|            logger.info("[%s] Connected to %s", self.name, self._ws_url)
   219|            return True
   220|        except Exception as exc:
   221|            message = f"WeCom startup failed: {exc}"
   222|            self._set_fatal_error("wecom_connect_error", message, retryable=True)
   223|            logger.error("[%s] Failed to connect: %s", self.name, exc, exc_info=True)
   224|            await self._cleanup_ws()
   225|            if self._http_client:
   226|                await self._http_client.aclose()
   227|                self._http_client = None
   228|            return False
   229|
   230|    async def disconnect(self) -> None:
   231|        """Disconnect from WeCom."""
   232|        self._running = False
   233|        self._mark_disconnected()
   234|
   235|        if self._listen_task:
   236|            self._listen_task.cancel()
   237|            try:
   238|                await self._listen_task
   239|            except asyncio.CancelledError:
   240|                pass
   241|            self._listen_task = None
   242|
   243|        if self._heartbeat_task:
   244|            self._heartbeat_task.cancel()
   245|            try:
   246|                await self._heartbeat_task
   247|            except asyncio.CancelledError:
   248|                pass
   249|            self._heartbeat_task = None
   250|
   251|        self._fail_pending_responses(RuntimeError("WeCom adapter disconnected"))
   252|        await self._cleanup_ws()
   253|
   254|        if self._http_client:
   255|            await self._http_client.aclose()
   256|            self._http_client = None
   257|
   258|        self._dedup.clear()
   259|        logger.info("[%s] Disconnected", self.name)
   260|
   261|    async def _cleanup_ws(self) -> None:
   262|        """Close the live websocket/session, if any."""
   263|        if self._ws and not self._ws.closed:
   264|            await self._ws.close()
   265|        self._ws = None
   266|
   267|        if self._session and not self._session.closed:
   268|            await self._session.close()
   269|        self._session = None
   270|
   271|    async def _open_connection(self) -> None:
   272|        """Open and authenticate a websocket connection."""
   273|        await self._cleanup_ws()
   274|        self._session = aiohttp.ClientSession(trust_env=True)
   275|        self._ws = await self._session.ws_connect(
   276|            self._ws_url,
   277|            heartbeat=HEARTBEAT_INTERVAL_SECONDS * 2,
   278|            timeout=CONNECT_TIMEOUT_SECONDS,
   279|        )
   280|
   281|        req_id = self._new_req_id("subscribe")
   282|        await self._send_json(
   283|            {
   284|                "cmd": APP_CMD_SUBSCRIBE,
   285|                "headers": {"req_id": req_id},
   286|                "body": {"bot_id": self._bot_id, "secret": self._secret},
   287|            }
   288|        )
   289|
   290|        auth_payload = await self._wait_for_handshake(req_id)
   291|        errcode = auth_payload.get("errcode", 0)
   292|        if errcode not in (0, None):
   293|            errmsg = auth_payload.get("errmsg", "authentication failed")
   294|            raise RuntimeError(f"{errmsg} (errcode={errcode})")
   295|
   296|    async def _wait_for_handshake(self, req_id: str) -> Dict[str, Any]:
   297|        """Wait for the subscribe acknowledgement."""
   298|        if not self._ws:
   299|            raise RuntimeError("WebSocket not initialized")
   300|
   301|        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_SECONDS
   302|        while True:
   303|            remaining = deadline - asyncio.get_running_loop().time()
   304|            if remaining <= 0:
   305|                raise TimeoutError("Timed out waiting for WeCom subscribe acknowledgement")
   306|
   307|            msg = await asyncio.wait_for(self._ws.receive(), timeout=remaining)
   308|            if msg.type == aiohttp.WSMsgType.TEXT:
   309|                payload = self._parse_json(msg.data)
   310|                if not payload:
   311|                    continue
   312|                if payload.get("cmd") == APP_CMD_PING:
   313|                    continue
   314|                if self._payload_req_id(payload) == req_id:
   315|                    return payload
   316|                logger.debug("[%s] Ignoring pre-auth payload: %s", self.name, payload.get("cmd"))
   317|            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
   318|                raise RuntimeError("WeCom websocket closed during authentication")
   319|
   320|    async def _listen_loop(self) -> None:
   321|        """Read websocket events forever, reconnecting on errors."""
   322|        backoff_idx = 0
   323|        while self._running:
   324|            try:
   325|                await self._read_events()
   326|                backoff_idx = 0
   327|            except asyncio.CancelledError:
   328|                return
   329|            except Exception as exc:
   330|                if not self._running:
   331|                    return
   332|                logger.warning("[%s] WebSocket error: %s", self.name, exc)
   333|                self._fail_pending_responses(RuntimeError("WeCom connection interrupted"))
   334|
   335|                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
   336|                backoff_idx += 1
   337|                await asyncio.sleep(delay)
   338|
   339|                try:
   340|                    await self._open_connection()
   341|                    backoff_idx = 0
   342|                    logger.info("[%s] Reconnected", self.name)
   343|                except Exception as reconnect_exc:
   344|                    logger.warning("[%s] Reconnect failed: %s", self.name, reconnect_exc)
   345|
   346|    async def _read_events(self) -> None:
   347|        """Read websocket frames until the connection closes."""
   348|        if not self._ws:
   349|            raise RuntimeError("WebSocket not connected")
   350|
   351|        while self._running and self._ws and not self._ws.closed:
   352|            msg = await self._ws.receive()
   353|            if msg.type == aiohttp.WSMsgType.TEXT:
   354|                payload = self._parse_json(msg.data)
   355|                if payload:
   356|                    await self._dispatch_payload(payload)
   357|            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
   358|                raise RuntimeError("WeCom websocket closed")
   359|
   360|    async def _heartbeat_loop(self) -> None:
   361|        """Send lightweight application-level pings."""
   362|        try:
   363|            while self._running:
   364|                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
   365|                if not self._ws or self._ws.closed:
   366|                    continue
   367|                try:
   368|                    await self._send_json(
   369|                        {
   370|                            "cmd": APP_CMD_PING,
   371|                            "headers": {"req_id": self._new_req_id("ping")},
   372|                            "body": {},
   373|                        }
   374|                    )
   375|                except Exception as exc:
   376|                    logger.debug("[%s] Heartbeat send failed: %s", self.name, exc)
   377|        except asyncio.CancelledError:
   378|            pass
   379|
   380|    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
   381|        """Route inbound websocket payloads."""
   382|        req_id = self._payload_req_id(payload)
   383|        cmd = str(payload.get("cmd") or "")
   384|
   385|        if req_id and req_id in self._pending_responses and cmd not in NON_RESPONSE_COMMANDS:
   386|            future = self._pending_responses.get(req_id)
   387|            if future and not future.done():
   388|                future.set_result(payload)
   389|            return
   390|
   391|        if cmd in CALLBACK_COMMANDS:
   392|            await self._on_message(payload)
   393|            return
   394|        if cmd in {APP_CMD_PING, APP_CMD_EVENT_CALLBACK}:
   395|            return
   396|
   397|        logger.debug("[%s] Ignoring websocket payload: %s", self.name, cmd or payload)
   398|
   399|    def _fail_pending_responses(self, exc: Exception) -> None:
   400|        """Fail all outstanding request futures."""
   401|        for req_id, future in list(self._pending_responses.items()):
   402|            if not future.done():
   403|                future.set_exception(exc)
   404|            self._pending_responses.pop(req_id, None)
   405|
   406|    async def _send_json(self, payload: Dict[str, Any]) -> None:
   407|        """Send a raw JSON frame over the active websocket."""
   408|        if not self._ws or self._ws.closed:
   409|            raise RuntimeError("WeCom websocket is not connected")
   410|        await self._ws.send_json(payload)
   411|
   412|    async def _send_request(self, cmd: str, body: Dict[str, Any], timeout: float = REQUEST_TIMEOUT_SECONDS) -> Dict[str, Any]:
   413|        """Send a JSON request and await the correlated response."""
   414|        if not self._ws or self._ws.closed:
   415|            raise RuntimeError("WeCom websocket is not connected")
   416|
   417|        req_id = self._new_req_id(cmd)
   418|        future = asyncio.get_running_loop().create_future()
   419|        self._pending_responses[req_id] = future
   420|        try:
   421|            await self._send_json({"cmd": cmd, "headers": {"req_id": req_id}, "body": body})
   422|            response = await asyncio.wait_for(future, timeout=timeout)
   423|            return response
   424|        finally:
   425|            self._pending_responses.pop(req_id, None)
   426|
   427|    async def _send_reply_request(
   428|        self,
   429|        reply_req_id: str,
   430|        body: Dict[str, Any],
   431|        cmd: str = APP_CMD_RESPONSE,
   432|        timeout: float = REQUEST_TIMEOUT_SECONDS,
   433|    ) -> Dict[str, Any]:
   434|        """Send a reply frame correlated to an inbound callback req_id."""
   435|        if not self._ws or self._ws.closed:
   436|            raise RuntimeError("WeCom websocket is not connected")
   437|
   438|        normalized_req_id = str(reply_req_id or "").strip()
   439|        if not normalized_req_id:
   440|            raise ValueError("reply_req_id is required")
   441|
   442|        future = asyncio.get_running_loop().create_future()
   443|        self._pending_responses[normalized_req_id] = future
   444|        try:
   445|            await self._send_json(
   446|                {"cmd": cmd, "headers": {"req_id": normalized_req_id}, "body": body}
   447|            )
   448|            response = await asyncio.wait_for(future, timeout=timeout)
   449|            return response
   450|        finally:
   451|            self._pending_responses.pop(normalized_req_id, None)
   452|
   453|    @staticmethod
   454|    def _new_req_id(prefix: str) -> str:
   455|        return f"{prefix}-{uuid.uuid4().hex}"
   456|
   457|    @staticmethod
   458|    def _payload_req_id(payload: Dict[str, Any]) -> str:
   459|        headers = payload.get("headers")
   460|        if isinstance(headers, dict):
   461|            return str(headers.get("req_id") or "")
   462|        return ""
   463|
   464|    @staticmethod
   465|    def _parse_json(raw: Any) -> Optional[Dict[str, Any]]:
   466|        try:
   467|            payload = json.loads(raw)
   468|        except Exception:
   469|            logger.debug("Failed to parse WeCom payload: %r", raw)
   470|            return None
   471|        return payload if isinstance(payload, dict) else None
   472|
   473|    # ------------------------------------------------------------------
   474|    # Inbound message parsing
   475|    # ------------------------------------------------------------------
   476|
   477|    async def _on_message(self, payload: Dict[str, Any]) -> None:
   478|        """Process an inbound WeCom message callback event."""
   479|        body = payload.get("body")
   480|        if not isinstance(body, dict):
   481|            return
   482|
   483|        msg_id = str(body.get("msgid") or self._payload_req_id(payload) or uuid.uuid4().hex)
   484|        if self._dedup.is_duplicate(msg_id):
   485|            logger.debug("[%s] Duplicate message %s ignored", self.name, msg_id)
   486|            return
   487|        self._remember_reply_req_id(msg_id, self._payload_req_id(payload))
   488|
   489|        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
   490|        sender_id = str(sender.get("userid") or "").strip()
   491|        chat_id = str(body.get("chatid") or sender_id).strip()
   492|        if not chat_id:
   493|            logger.debug("[%s] Missing chat id, skipping message", self.name)
   494|            return
   495|
   496|        is_group = str(body.get("chattype") or "").lower() == "group"
   497|        if is_group:
   498|            if not self._is_group_allowed(chat_id, sender_id):
   499|                logger.debug("[%s] Group %s / sender %s blocked by policy", self.name, chat_id, sender_id)
   500|                return
   501|