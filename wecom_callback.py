     1|"""WeCom callback-mode adapter for self-built enterprise applications.
     2|
     3|Unlike the bot/websocket adapter in ``wecom.py``, this handles the standard
     4|WeCom callback flow: WeCom POSTs encrypted XML to an HTTP endpoint, the
     5|adapter decrypts it, queues the message for the agent, and immediately
     6|acknowledges.  The agent's reply is delivered later via the proactive
     7|``message/send`` API using an access-token.
     8|
     9|Supports multiple self-built apps under one gateway instance, scoped by
    10|``corp_id:user_id`` to avoid cross-corp collisions.
    11|"""
    12|
    13|from __future__ import annotations
    14|
    15|import asyncio
    16|import logging
    17|import socket as _socket
    18|import time
    19|from typing import Any, Dict, List, Optional
    20|from xml.etree import ElementTree as ET
    21|
    22|try:
    23|    from aiohttp import web
    24|
    25|    AIOHTTP_AVAILABLE = True
    26|except ImportError:
    27|    web = None  # type: ignore[assignment]
    28|    AIOHTTP_AVAILABLE = False
    29|
    30|try:
    31|    import httpx
    32|
    33|    HTTPX_AVAILABLE = True
    34|except ImportError:
    35|    httpx = None  # type: ignore[assignment]
    36|    HTTPX_AVAILABLE = False
    37|
    38|from gateway.config import Platform, PlatformConfig
    39|from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
    40|from gateway.platforms.wecom_crypto import WXBizMsgCrypt, WeComCryptoError
    41|
    42|logger = logging.getLogger(__name__)
    43|
    44|DEFAULT_HOST = "0.0.0.0"
    45|DEFAULT_PORT = 8645
    46|DEFAULT_PATH = "/wecom/callback"
    47|ACCESS_TOKEN_TTL_SECONDS=***
    48|MESSAGE_DEDUP_TTL_SECONDS = 300
    49|
    50|
    51|def check_wecom_callback_requirements() -> bool:
    52|    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE
    53|
    54|
    55|class WecomCallbackAdapter(BasePlatformAdapter):
    56|    def __init__(self, config: PlatformConfig):
    57|        super().__init__(config, Platform.WECOM_CALLBACK)
    58|        extra = config.extra or {}
    59|        self._host = str(extra.get("host") or DEFAULT_HOST)
    60|        self._port = int(extra.get("port") or DEFAULT_PORT)
    61|        self._path = str(extra.get("path") or DEFAULT_PATH)
    62|        self._apps: List[Dict[str, Any]] = self._normalize_apps(extra)
    63|        self._runner: Optional[web.AppRunner] = None
    64|        self._site: Optional[web.TCPSite] = None
    65|        self._app: Optional[web.Application] = None
    66|        self._http_client: Optional[httpx.AsyncClient] = None
    67|        self._message_queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
    68|        self._poll_task: Optional[asyncio.Task] = None
    69|        self._seen_messages: Dict[str, float] = {}
    70|        self._user_app_map: Dict[str, str] = {}
    71|        self._access_tokens: Dict[str, Dict[str, Any]] = {}
    72|
    73|    # ------------------------------------------------------------------
    74|    # App normalisation
    75|    # ------------------------------------------------------------------
    76|
    77|    @staticmethod
    78|    def _user_app_key(corp_id: str, user_id: str) -> str:
    79|        return f"{corp_id}:{user_id}" if corp_id else user_id
    80|
    81|    @staticmethod
    82|    def _normalize_apps(extra: Dict[str, Any]) -> List[Dict[str, Any]]:
    83|        apps = extra.get("apps")
    84|        if isinstance(apps, list) and apps:
    85|            return [dict(app) for app in apps if isinstance(app, dict)]
    86|        if extra.get("corp_id"):
    87|            return [
    88|                {
    89|                    "name": extra.get("name") or "default",
    90|                    "corp_id": extra.get("corp_id", ""),
    91|                    "corp_secret": extra.get("corp_secret", ""),
    92|                    "agent_id": str(extra.get("agent_id", "")),
    93|                    "token": extra.get("token", ""),
    94|                    "encoding_aes_key": extra.get("encoding_aes_key", ""),
    95|                }
    96|            ]
    97|        return []
    98|
    99|    # ------------------------------------------------------------------
   100|    # Lifecycle
   101|    # ------------------------------------------------------------------
   102|
   103|    async def connect(self) -> bool:
   104|        if not self._apps:
   105|            logger.warning("[WecomCallback] No callback apps configured")
   106|            return False
   107|        if not check_wecom_callback_requirements():
   108|            logger.warning("[WecomCallback] aiohttp/httpx not installed")
   109|            return False
   110|
   111|        # Quick port-in-use check.
   112|        try:
   113|            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
   114|                sock.settimeout(1)
   115|                sock.connect(("127.0.0.1", self._port))
   116|            logger.error("[WecomCallback] Port %d already in use", self._port)
   117|            return False
   118|        except (ConnectionRefusedError, OSError):
   119|            pass
   120|
   121|        try:
   122|            self._http_client = httpx.AsyncClient(timeout=20.0)
   123|            self._app = web.Application()
   124|            self._app.router.add_get("/health", self._handle_health)
   125|            self._app.router.add_get(self._path, self._handle_verify)
   126|            self._app.router.add_post(self._path, self._handle_callback)
   127|            self._runner = web.AppRunner(self._app)
   128|            await self._runner.setup()
   129|            self._site = web.TCPSite(self._runner, self._host, self._port)
   130|            await self._site.start()
   131|            self._poll_task = asyncio.create_task(self._poll_loop())
   132|            self._mark_connected()
   133|            logger.info(
   134|                "[WecomCallback] HTTP server listening on %s:%s%s",
   135|                self._host, self._port, self._path,
   136|            )
   137|            for app in self._apps:
   138|                try:
   139|                    await self._refresh_access_token(app)
   140|                except Exception as exc:
   141|                    logger.warning(
   142|                        "[WecomCallback] Initial token refresh failed for app '%s': %s",
   143|                        app.get("name", "default"), exc,
   144|                    )
   145|            return True
   146|        except Exception:
   147|            await self._cleanup()
   148|            logger.exception("[WecomCallback] Failed to start")
   149|            return False
   150|
   151|    async def disconnect(self) -> None:
   152|        self._running = False
   153|        if self._poll_task:
   154|            self._poll_task.cancel()
   155|            try:
   156|                await self._poll_task
   157|            except asyncio.CancelledError:
   158|                pass
   159|            self._poll_task = None
   160|        await self._cleanup()
   161|        self._mark_disconnected()
   162|        logger.info("[WecomCallback] Disconnected")
   163|
   164|    async def _cleanup(self) -> None:
   165|        self._site = None
   166|        if self._runner:
   167|            await self._runner.cleanup()
   168|            self._runner = None
   169|        self._app = None
   170|        if self._http_client:
   171|            await self._http_client.aclose()
   172|            self._http_client = None
   173|
   174|    # ------------------------------------------------------------------
   175|    # Outbound: proactive send via access-token API
   176|    # ------------------------------------------------------------------
   177|
   178|    async def send(
   179|        self,
   180|        chat_id: str,
   181|        content: str,
   182|        reply_to: Optional[str] = None,
   183|        metadata: Optional[Dict[str, Any]] = None,
   184|    ) -> SendResult:
   185|        app = self._resolve_app_for_chat(chat_id)
   186|        touser = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
   187|        try:
   188|            token = await self._get_access_token(app)
   189|            payload = {
   190|                "touser": touser,
   191|                "msgtype": "text",
   192|                "agentid": int(str(app.get("agent_id") or 0)),
   193|                "text": {"content": content[:2048]},
   194|                "safe": 0,
   195|            }
   196|            resp = await self._http_client.post(
   197|                f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={token}",
   198|                json=payload,
   199|            )
   200|            data = resp.json()
   201|            if data.get("errcode") != 0:
   202|                return SendResult(success=False, error=str(data))
   203|            return SendResult(
   204|                success=True,
   205|                message_id=str(data.get("msgid", "")),
   206|                raw_response=data,
   207|            )
   208|        except Exception as exc:
   209|            return SendResult(success=False, error=str(exc))
   210|
   211|    def _resolve_app_for_chat(self, chat_id: str) -> Dict[str, Any]:
   212|        """Pick the app associated with *chat_id*, falling back sensibly."""
   213|        app_name = self._user_app_map.get(chat_id)
   214|        if not app_name and ":" not in chat_id:
   215|            # Legacy bare user_id — try to find a unique match.
   216|            matching = [k for k in self._user_app_map if k.endswith(f":{chat_id}")]
   217|            if len(matching) == 1:
   218|                app_name = self._user_app_map.get(matching[0])
   219|        app = self._get_app_by_name(app_name) if app_name else None
   220|        return app or self._apps[0]
   221|
   222|    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
   223|        return {"name": chat_id, "type": "dm"}
   224|
   225|    # ------------------------------------------------------------------
   226|    # Inbound: HTTP callback handlers
   227|    # ------------------------------------------------------------------
   228|
   229|    async def _handle_health(self, request: web.Request) -> web.Response:
   230|        return web.json_response({"status": "ok", "platform": "wecom_callback"})
   231|
   232|    async def _handle_verify(self, request: web.Request) -> web.Response:
   233|        """GET endpoint — WeCom URL verification handshake."""
   234|        msg_signature = request.query.get("msg_signature", "")
   235|        timestamp = request.query.get("timestamp", "")
   236|        nonce = request.query.get("nonce", "")
   237|        echostr = request.query.get("echostr", "")
   238|        for app in self._apps:
   239|            try:
   240|                crypt = self._crypt_for_app(app)
   241|                plain = crypt.verify_url(msg_signature, timestamp, nonce, echostr)
   242|                return web.Response(text=plain, content_type="text/plain")
   243|            except Exception:
   244|                continue
   245|        return web.Response(status=403, text="signature verification failed")
   246|
   247|    async def _handle_callback(self, request: web.Request) -> web.Response:
   248|        """POST endpoint — receive an encrypted message callback."""
   249|        msg_signature = request.query.get("msg_signature", "")
   250|        timestamp = request.query.get("timestamp", "")
   251|        nonce = request.query.get("nonce", "")
   252|        body = await request.text()
   253|
   254|        for app in self._apps:
   255|            try:
   256|                decrypted = self._decrypt_request(
   257|                    app, body, msg_signature, timestamp, nonce,
   258|                )
   259|                event = self._build_event(app, decrypted)
   260|                if event is not None:
   261|                    # Record which app this user belongs to.
   262|                    if event.source and event.source.user_id:
   263|                        map_key = self._user_app_key(
   264|                            str(app.get("corp_id") or ""), event.source.user_id,
   265|                        )
   266|                        self._user_app_map[map_key] = app["name"]
   267|                    await self._message_queue.put(event)
   268|                # Immediately acknowledge — the agent's reply will arrive
   269|                # later via the proactive message/send API.
   270|                return web.Response(text="success", content_type="text/plain")
   271|            except WeComCryptoError:
   272|                continue
   273|            except Exception:
   274|                logger.exception("[WecomCallback] Error handling message")
   275|                break
   276|        return web.Response(status=400, text="invalid callback payload")
   277|
   278|    async def _poll_loop(self) -> None:
   279|        """Drain the message queue and dispatch to the gateway runner."""
   280|        while True:
   281|            event = await self._message_queue.get()
   282|            try:
   283|                task = asyncio.create_task(self.handle_message(event))
   284|                self._background_tasks.add(task)
   285|                task.add_done_callback(self._background_tasks.discard)
   286|            except Exception:
   287|                logger.exception("[WecomCallback] Failed to enqueue event")
   288|
   289|    # ------------------------------------------------------------------
   290|    # XML / crypto helpers
   291|    # ------------------------------------------------------------------
   292|
   293|    def _decrypt_request(
   294|        self, app: Dict[str, Any], body: str,
   295|        msg_signature: str, timestamp: str, nonce: str,
   296|    ) -> str:
   297|        root = ET.fromstring(body)
   298|        encrypt = root.findtext("Encrypt", default="")
   299|        crypt = self._crypt_for_app(app)
   300|        return crypt.decrypt(msg_signature, timestamp, nonce, encrypt).decode("utf-8")
   301|
   302|    def _build_event(self, app: Dict[str, Any], xml_text: str) -> Optional[MessageEvent]:
   303|        root = ET.fromstring(xml_text)
   304|        msg_type = (root.findtext("MsgType") or "").lower()
   305|        # Silently acknowledge lifecycle events.
   306|        if msg_type == "event":
   307|            event_name = (root.findtext("Event") or "").lower()
   308|            if event_name in {"enter_agent", "subscribe"}:
   309|                return None
   310|        if msg_type not in {"text", "event"}:
   311|            return None
   312|
   313|        user_id = root.findtext("FromUserName", default="")
   314|        corp_id = root.findtext("ToUserName", default=app.get("corp_id", ""))
   315|        scoped_chat_id = self._user_app_key(corp_id, user_id)
   316|        content = root.findtext("Content", default="").strip()
   317|        if not content and msg_type == "event":
   318|            content = "/start"
   319|        msg_id = (
   320|            root.findtext("MsgId")
   321|            or f"{user_id}:{root.findtext('CreateTime', default='0')}"
   322|        )
   323|        source = self.build_source(
   324|            chat_id=scoped_chat_id,
   325|            chat_name=user_id,
   326|            chat_type="dm",
   327|            user_id=user_id,
   328|            user_name=user_id,
   329|        )
   330|        return MessageEvent(
   331|            text=content,
   332|            message_type=MessageType.TEXT,
   333|            source=source,
   334|            raw_message=xml_text,
   335|            message_id=msg_id,
   336|        )
   337|
   338|    def _crypt_for_app(self, app: Dict[str, Any]) -> WXBizMsgCrypt:
   339|        return WXBizMsgCrypt(
   340|            token=str(app.get("token") or ""),
   341|            encoding_aes_key=str(app.get("encoding_aes_key") or ""),
   342|            receive_id=str(app.get("corp_id") or ""),
   343|        )
   344|
   345|    def _get_app_by_name(self, name: Optional[str]) -> Optional[Dict[str, Any]]:
   346|        if not name:
   347|            return None
   348|        for app in self._apps:
   349|            if app.get("name") == name:
   350|                return app
   351|        return None
   352|
   353|    # ------------------------------------------------------------------
   354|    # Access-token management
   355|    # ------------------------------------------------------------------
   356|
   357|    async def _get_access_token(self, app: Dict[str, Any]) -> str:
   358|        cached = self._access_tokens.get(app["name"])
   359|        now = time.time()
   360|        if cached and cached.get("expires_at", 0) > now + 60:
   361|            return cached["token"]
   362|        return await self._refresh_access_token(app)
   363|
   364|    async def _refresh_access_token(self, app: Dict[str, Any]) -> str:
   365|        resp = await self._http_client.get(
   366|            "https://qyapi.weixin.qq.com/cgi-bin/gettoken",
   367|            params={
   368|                "corpid": app.get("corp_id"),
   369|                "corpsecret": app.get("corp_secret"),
   370|            },
   371|        )
   372|        data = resp.json()
   373|        if data.get("errcode") != 0:
   374|            raise RuntimeError(f"WeCom token refresh failed: {data}")
   375|        token = data["access_token"]
   376|        expires_in = int(data.get("expires_in", ACCESS_TOKEN_TTL_SECONDS))
   377|        self._access_tokens[app["name"]] = {
   378|            "token": token,
   379|            "expires_at": time.time() + expires_in,
   380|        }
   381|        logger.info(
   382|            "[WecomCallback] Token refreshed for app '%s' (corp=%s), expires in %ss",
   383|            app.get("name", "default"),
   384|            app.get("corp_id", ""),
   385|            expires_in,
   386|        )
   387|        return token
   388|