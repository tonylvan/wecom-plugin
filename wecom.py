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
   142|def _is_bot_mentioned(body: Dict[str, Any], bot_id: str) -> bool:
   143|    """
   144|    Check if the bot is mentioned in a group chat message.
   145|    
   146|    WeCom sends mentioned user IDs in the `mentioned_userid_list` field.
   147|    """
   148|    if not bot_id:
   149|        return False
   150|    
   151|    # Get mentioned user list from WeCom message
   152|    mentioned_list = body.get("mentioned_userid_list") or []
   153|    if not isinstance(mentioned_list, list):
   154|        mentioned_list = [mentioned_list] if mentioned_list else []
   155|    
   156|    # Check if bot's userid is in the mentioned list
   157|    return bot_id in mentioned_list
   158|
   159|
   160|class WeComAdapter(BasePlatformAdapter):
   161|    """WeCom AI Bot adapter backed by a persistent WebSocket connection."""
   162|
   163|    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
   164|    # Threshold for detecting WeCom client-side message splits.
   165|    # When a chunk is near the 4000-char limit, a continuation is almost certain.
   166|    _SPLIT_THRESHOLD = 3900
   167|
   168|    def __init__(self, config: PlatformConfig):
   169|        super().__init__(config, Platform.WECOM)
   170|
   171|        extra = config.extra or {}
   172|        self._bot_id = str(extra.get("bot_id") or os.getenv("WECOM_BOT_ID", "")).strip()
   173|        self._secret = str(extra.get("secret") or os.getenv("WECOM_SECRET", "")).strip()
   174|        self._ws_url = str(
   175|            extra.get("websocket_url")
   176|            or extra.get("websocketUrl")
   177|            or os.getenv("WECOM_WEBSOCKET_URL", DEFAULT_WS_URL)
   178|        ).strip() or DEFAULT_WS_URL
   179|
   180|        self._dm_policy = str(extra.get("dm_policy") or os.getenv("WECOM_DM_POLICY", "open")).strip().lower()
   181|        self._allow_from = _coerce_list(extra.get("allow_from") or extra.get("allowFrom"))
   182|
   183|        self._group_policy = str(extra.get("group_policy") or os.getenv("WECOM_GROUP_POLICY", "open")).strip().lower()
   184|        self._group_allow_from = _coerce_list(extra.get("group_allow_from") or extra.get("groupAllowFrom"))
   185|        self._groups = extra.get("groups") if isinstance(extra.get("groups"), dict) else {}
   186|
   187|        self._session: Optional["aiohttp.ClientSession"] = None
   188|        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
   189|        self._http_client: Optional["httpx.AsyncClient"] = None
   190|        self._listen_task: Optional[asyncio.Task] = None
   191|        self._heartbeat_task: Optional[asyncio.Task] = None
   192|        self._pending_responses: Dict[str, asyncio.Future] = {}
   193|        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE)
   194|        self._reply_req_ids: Dict[str, str] = {}
   195|
   196|        # Text batching: merge rapid successive messages (Telegram-style).
   197|        # WeCom clients split long messages around 4000 chars.
   198|        self._text_batch_delay_seconds = float(os.getenv("HERMES_WECOM_TEXT_BATCH_DELAY_SECONDS", "0.6"))
   199|        self._text_batch_split_delay_seconds = float(os.getenv("HERMES_WECOM_TEXT_BATCH_SPLIT_DELAY_SECONDS", "2.0"))
   200|        self._pending_text_batches: Dict[str, MessageEvent] = {}
   201|        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}
   202|
   203|        # Multi-agent support: parse @mentions and route to different agents
   204|        # in group chats. Disabled by default — requires explicit config.
   205|        self._mention_router = MentionRouter.from_wecom_extra(extra)
   206|        self._multi_agent_chains: Dict[str, asyncio.Task] = {}  # chat_id → chain task
   207|
   208|    # ------------------------------------------------------------------
   209|    # Connection lifecycle
   210|    # ------------------------------------------------------------------
   211|
   212|    async def connect(self) -> bool:
   213|        """Connect to the WeCom AI Bot gateway."""
   214|        if not AIOHTTP_AVAILABLE:
   215|            message = "WeCom startup failed: aiohttp not installed"
   216|            self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
   217|            logger.warning("[%s] %s. Run: pip install aiohttp", self.name, message)
   218|            return False
   219|        if not HTTPX_AVAILABLE:
   220|            message = "WeCom startup failed: httpx not installed"
   221|            self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
   222|            logger.warning("[%s] %s. Run: pip install httpx", self.name, message)
   223|            return False
   224|        if not self._bot_id or not self._secret:
   225|            message = "WeCom startup failed: WECOM_BOT_ID and WECOM_SECRET are required"
   226|            self._set_fatal_error("wecom_missing_credentials", message, retryable=True)
   227|            logger.warning("[%s] %s", self.name, message)
   228|            return False
   229|
   230|        try:
   231|            self._http_client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
   232|            await self._open_connection()
   233|            self._mark_connected()
   234|            self._listen_task = asyncio.create_task(self._listen_loop())
   235|            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
   236|            logger.info("[%s] Connected to %s", self.name, self._ws_url)
   237|            return True
   238|        except Exception as exc:
   239|            message = f"WeCom startup failed: {exc}"
   240|            self._set_fatal_error("wecom_connect_error", message, retryable=True)
   241|            logger.error("[%s] Failed to connect: %s", self.name, exc, exc_info=True)
   242|            await self._cleanup_ws()
   243|            if self._http_client:
   244|                await self._http_client.aclose()
   245|                self._http_client = None
   246|            return False
   247|
   248|    async def disconnect(self) -> None:
   249|        """Disconnect from WeCom."""
   250|        self._running = False
   251|        self._mark_disconnected()
   252|
   253|        if self._listen_task:
   254|            self._listen_task.cancel()
   255|            try:
   256|                await self._listen_task
   257|            except asyncio.CancelledError:
   258|                pass
   259|            self._listen_task = None
   260|
   261|        if self._heartbeat_task:
   262|            self._heartbeat_task.cancel()
   263|            try:
   264|                await self._heartbeat_task
   265|            except asyncio.CancelledError:
   266|                pass
   267|
   268|    async def _cleanup_ws(self) -> None:
   269|        """Close the live websocket/session, if any."""
   270|        if self._ws and not self._ws.closed:
   271|            await self._ws.close()
   272|        self._ws = None
   273|
   274|        if self._session and not self._session.closed:
   275|            await self._session.close()
   276|        self._session = None
   277|
   278|    def _is_group_allowed(self, chat_id: str, sender_id: str) -> bool:
   279|        """
   280|        Check if a group chat message should be processed based on policy.
   281|        
   282|        Policies:
   283|        - "open": Allow all group messages (but still need @mention)
   284|        - "allowlist": Only allow from specific groups/members
   285|        - "disabled": Block all group messages
   286|        """
   287|        if self._group_policy == "disabled":
   288|            return False
   289|        
   290|        if self._group_policy == "allowlist":
   291|            # Check if group is in allowlist
   292|            if not _entry_matches(self._group_allow_from, chat_id):
   293|                return False
   294|            
   295|            # Check group-specific member allowlist
   296|            group_config = self._groups.get(chat_id, {})
   297|            group_allow = _coerce_list(group_config.get("allow_from"))
   298|            if group_allow and not _entry_matches(group_allow, sender_id):
   299|                return False
   300|        
   301|        return True
   302|
   303|    async def _open_connection(self) -> None:
   304|        """Open and authenticate a websocket connection."""
   305|        await self._cleanup_ws()
   306|        self._session = aiohttp.ClientSession(trust_env=True)
   307|        self._ws = await self._session.ws_connect(
   308|            self._ws_url,
   309|            heartbeat=HEARTBEAT_INTERVAL_SECONDS * 2,
   310|            timeout=CONNECT_TIMEOUT_SECONDS,
   311|        )
   312|
   313|        req_id = self._new_req_id("subscribe")
   314|        await self._send_json(
   315|            {
   316|                "cmd": APP_CMD_SUBSCRIBE,
   317|                "headers": {"req_id": req_id},
   318|                "body": {"bot_id": self._bot_id, "secret": self._secret},
   319|            }
   320|        )
   321|
   322|        auth_payload = await self._wait_for_handshake(req_id)
   323|        errcode = auth_payload.get("errcode", 0)
   324|        if errcode not in (0, None):
   325|            errmsg = auth_payload.get("errmsg", "authentication failed")
   326|            raise RuntimeError(f"{errmsg} (errcode={errcode})")
   327|
   328|    async def _wait_for_handshake(self, req_id: str) -> Dict[str, Any]:
   329|        """Wait for the subscribe acknowledgement."""
   330|        if not self._ws:
   331|            raise RuntimeError("WebSocket not initialized")
   332|
   333|        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_SECONDS
   334|        while True:
   335|            remaining = deadline - asyncio.get_running_loop().time()
   336|            if remaining <= 0:
   337|                raise TimeoutError("Timed out waiting for WeCom subscribe acknowledgement")
   338|
   339|            msg = await asyncio.wait_for(self._ws.receive(), timeout=remaining)
   340|            if msg.type == aiohttp.WSMsgType.TEXT:
   341|                payload = self._parse_json(msg.data)
   342|                if not payload:
   343|                    continue
   344|                if payload.get("cmd") == APP_CMD_PING:
   345|                    continue
   346|                if self._payload_req_id(payload) == req_id:
   347|                    return payload
   348|                logger.debug("[%s] Ignoring pre-auth payload: %s", self.name, payload.get("cmd"))
   349|            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
   350|                raise RuntimeError("WeCom websocket closed during authentication")
   351|
   352|    async def _listen_loop(self) -> None:
   353|        """Read websocket events forever, reconnecting on errors."""
   354|        backoff_idx = 0
   355|        while self._running:
   356|            try:
   357|                await self._read_events()
   358|                backoff_idx = 0
   359|            except asyncio.CancelledError:
   360|                return
   361|            except Exception as exc:
   362|                if not self._running:
   363|                    return
   364|                logger.warning("[%s] WebSocket error: %s", self.name, exc)
   365|                self._fail_pending_responses(RuntimeError("WeCom connection interrupted"))
   366|
   367|                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
   368|                backoff_idx += 1
   369|                await asyncio.sleep(delay)
   370|
   371|                try:
   372|                    await self._open_connection()
   373|                    backoff_idx = 0
   374|                    logger.info("[%s] Reconnected", self.name)
   375|                except Exception as reconnect_exc:
   376|                    logger.warning("[%s] Reconnect failed: %s", self.name, reconnect_exc)
   377|
   378|    async def _read_events(self) -> None:
   379|        """Read websocket frames until the connection closes."""
   380|        if not self._ws:
   381|            raise RuntimeError("WebSocket not connected")
   382|
   383|        while self._running and self._ws and not self._ws.closed:
   384|            msg = await self._ws.receive()
   385|            if msg.type == aiohttp.WSMsgType.TEXT:
   386|                payload = self._parse_json(msg.data)
   387|                if payload:
   388|                    await self._dispatch_payload(payload)
   389|            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
   390|                raise RuntimeError("WeCom websocket closed")
   391|
   392|    async def _heartbeat_loop(self) -> None:
   393|        """Send lightweight application-level pings."""
   394|        try:
   395|            while self._running:
   396|                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
   397|                if not self._ws or self._ws.closed:
   398|                    continue
   399|                try:
   400|                    await self._send_json(
   401|                        {
   402|                            "cmd": APP_CMD_PING,
   403|                            "headers": {"req_id": self._new_req_id("ping")},
   404|                            "body": {},
   405|                        }
   406|                    )
   407|                except Exception as exc:
   408|                    logger.debug("[%s] Heartbeat send failed: %s", self.name, exc)
   409|        except asyncio.CancelledError:
   410|            pass
   411|
   412|    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
   413|        """Route inbound websocket payloads."""
   414|        req_id = self._payload_req_id(payload)
   415|        cmd = str(payload.get("cmd") or "")
   416|
   417|        if req_id and req_id in self._pending_responses and cmd not in NON_RESPONSE_COMMANDS:
   418|            future = self._pending_responses.get(req_id)
   419|            if future and not future.done():
   420|                future.set_result(payload)
   421|            return
   422|
   423|        if cmd in CALLBACK_COMMANDS:
   424|            await self._on_message(payload)
   425|            return
   426|        if cmd in {APP_CMD_PING, APP_CMD_EVENT_CALLBACK}:
   427|            return
   428|
   429|        logger.debug("[%s] Ignoring websocket payload: %s", self.name, cmd or payload)
   430|
   431|    def _fail_pending_responses(self, exc: Exception) -> None:
   432|        """Fail all outstanding request futures."""
   433|        for req_id, future in list(self._pending_responses.items()):
   434|            if not future.done():
   435|                future.set_exception(exc)
   436|            self._pending_responses.pop(req_id, None)
   437|
   438|    async def _send_json(self, payload: Dict[str, Any]) -> None:
   439|        """Send a raw JSON frame over the active websocket."""
   440|        if not self._ws or self._ws.closed:
   441|            raise RuntimeError("WeCom websocket is not connected")
   442|        await self._ws.send_json(payload)
   443|
   444|    async def _send_request(self, cmd: str, body: Dict[str, Any], timeout: float = REQUEST_TIMEOUT_SECONDS) -> Dict[str, Any]:
   445|        """Send a JSON request and await the correlated response."""
   446|        if not self._ws or self._ws.closed:
   447|            raise RuntimeError("WeCom websocket is not connected")
   448|
   449|        req_id = self._new_req_id(cmd)
   450|        future = asyncio.get_running_loop().create_future()
   451|        self._pending_responses[req_id] = future
   452|        try:
   453|            await self._send_json({"cmd": cmd, "headers": {"req_id": req_id}, "body": body})
   454|            response = await asyncio.wait_for(future, timeout=timeout)
   455|            return response
   456|        finally:
   457|            self._pending_responses.pop(req_id, None)
   458|
   459|    async def _send_reply_request(
   460|        self,
   461|        reply_req_id: str,
   462|        body: Dict[str, Any],
   463|        cmd: str = APP_CMD_RESPONSE,
   464|        timeout: float = REQUEST_TIMEOUT_SECONDS,
   465|    ) -> Dict[str, Any]:
   466|        """Send a reply frame correlated to an inbound callback req_id."""
   467|        if not self._ws or self._ws.closed:
   468|            raise RuntimeError("WeCom websocket is not connected")
   469|
   470|        normalized_req_id = str(reply_req_id or "").strip()
   471|        if not normalized_req_id:
   472|            raise ValueError("reply_req_id is required")
   473|
   474|        future = asyncio.get_running_loop().create_future()
   475|        self._pending_responses[normalized_req_id] = future
   476|        try:
   477|            await self._send_json(
   478|                {"cmd": cmd, "headers": {"req_id": normalized_req_id}, "body": body}
   479|            )
   480|            response = await asyncio.wait_for(future, timeout=timeout)
   481|            return response
   482|        finally:
   483|            self._pending_responses.pop(normalized_req_id, None)
   484|
   485|    @staticmethod
   486|    def _new_req_id(prefix: str) -> str:
   487|        return f"{prefix}-{uuid.uuid4().hex}"
   488|
   489|    @staticmethod
   490|    def _payload_req_id(payload: Dict[str, Any]) -> str:
   491|        headers = payload.get("headers")
   492|        if isinstance(headers, dict):
   493|            return str(headers.get("req_id") or "")
   494|        return ""
   495|
   496|    @staticmethod
   497|    def _parse_json(raw: Any) -> Optional[Dict[str, Any]]:
   498|        try:
   499|            payload = json.loads(raw)
   500|        except Exception:
   501|            logger.debug("Failed to parse WeCom payload: %r", raw)
   502|            return None
   503|        return payload if isinstance(payload, dict) else None
   504|
   505|    # ------------------------------------------------------------------
   506|    # Inbound message parsing
   507|    # ------------------------------------------------------------------
   508|
   509|    async def _on_message(self, payload: Dict[str, Any]) -> None:
   510|        """Process an inbound WeCom message callback event."""
   511|        body = payload.get("body")
   512|        if not isinstance(body, dict):
   513|            return
   514|
   515|        msg_id = str(body.get("msgid") or self._payload_req_id(payload) or uuid.uuid4().hex)
   516|        if self._dedup.is_duplicate(msg_id):
   517|            logger.debug("[%s] Duplicate message %s ignored", self.name, msg_id)
   518|            return
   519|        self._remember_reply_req_id(msg_id, self._payload_req_id(payload))
   520|
   521|        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
   522|        sender_id = str(sender.get("userid") or "").strip()
   523|        chat_id = str(body.get("chatid") or sender_id).strip()
   524|        if not chat_id:
   525|            logger.debug("[%s] Missing chat id, skipping message", self.name)
   526|            return
   527|
   528|        is_group = str(body.get("chattype") or "").lower() == "group"
   529|        if is_group:
   530|            if not self._is_group_allowed(chat_id, sender_id):
   531|                logger.debug("[%s] Group %s / sender %s blocked by policy", self.name, chat_id, sender_id)
   532|                return

        # 获取消息内容
        content = str(body.get("content") or "").strip()
        msg_type = str(body.get("msgtype") or "text").lower()

        # 群聊中需要检查机器人是否被 @
        if is_group:
            # 首先检查是否通过 mentioned_userid_list 被 @
            is_mentioned = _is_bot_mentioned(body, self._bot_id)
            
            # 如果没有被 @，检查是否通过文本 @mention 匹配（多 Agent 模式）
            if not is_mentioned:
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
