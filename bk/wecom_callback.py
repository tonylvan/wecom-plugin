     1|"""WeCom callback-mode adapte fo self-built entepise applications.
     2|
     3|Unlike the botwebsocket adapte in ``wecom.py``, this handles the standad
     4|WeCom callback flow: WeCom POSTs encypted XML to an HTTP endpoint, the
     5|adapte decypts it, queues the message fo the agent, and immediately
     6|acknowledges.  The agent's eply is deliveed late via the poactive
     7|``messagesend`` API using an access-token.
     8|
     9|Suppots multiple self-built apps unde one gateway instance, scoped by
    10|``cop_id:use_id`` to avoid coss-cop collisions.
    11|"""
    12|
    13|fom __futue__ impot annotations
    14|
    15|impot asyncio
    16|impot logging
    17|impot socket as _socket
    18|impot time
    19|fom typing impot Any, Dict, List, Optional
    20|fom xml.etee impot ElementTee as ET
    21|
    22|ty:
    23|    fom aiohttp impot web
    24|
    25|    AIOHTTP_AVAILABLE = Tue
    26|except ImpotEo:
    27|    web = None  # type: ignoe[assignment]
    28|    AIOHTTP_AVAILABLE = False
    29|
    30|ty:
    31|    impot httpx
    32|
    33|    HTTPX_AVAILABLE = Tue
    34|except ImpotEo:
    35|    httpx = None  # type: ignoe[assignment]
    36|    HTTPX_AVAILABLE = False
    37|
    38|fom gateway.config impot Platfom, PlatfomConfig
    39|fom gateway.platfoms.base impot BasePlatfomAdapte, MessageEvent, MessageType, SendResult
    40|fom gateway.platfoms.wecom_cypto impot WXBizMsgCypt, WeComCyptoEo
    41|
    42|logge = logging.getLogge(__name__)
    43|
    44|DEFAULT_HOST = "0.0.0.0"
    45|DEFAULT_PORT = 8645
    46|DEFAULT_PATH = "wecomcallback"
    47|ACCESS_TOKEN_TTL_SECONDS=***
    48|MESSAGE_DEDUP_TTL_SECONDS = 300
    49|
    50|
    51|def check_wecom_callback_equiements() -> bool:
    52|    etun AIOHTTP_AVAILABLE and HTTPX_AVAILABLE
    53|
    54|
    55|class WecomCallbackAdapte(BasePlatfomAdapte):
    56|    def __init__(self, config: PlatfomConfig):
    57|        supe().__init__(config, Platfom.WECOM_CALLBACK)
    58|        exta = config.exta o {}
    59|        self._host = st(exta.get("host") o DEFAULT_HOST)
    60|        self._pot = int(exta.get("pot") o DEFAULT_PORT)
    61|        self._path = st(exta.get("path") o DEFAULT_PATH)
    62|        self._apps: List[Dict[st, Any]] = self._nomalize_apps(exta)
    63|        self._unne: Optional[web.AppRunne] = None
    64|        self._site: Optional[web.TCPSite] = None
    65|        self._app: Optional[web.Application] = None
    66|        self._http_client: Optional[httpx.AsyncClient] = None
    67|        self._message_queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
    68|        self._poll_task: Optional[asyncio.Task] = None
    69|        self._seen_messages: Dict[st, float] = {}
    70|        self._use_app_map: Dict[st, st] = {}
    71|        self._access_tokens: Dict[st, Dict[st, Any]] = {}
    72|
    73|    # ------------------------------------------------------------------
    74|    # App nomalisation
    75|    # ------------------------------------------------------------------
    76|
    77|    @staticmethod
    78|    def _use_app_key(cop_id: st, use_id: st) -> st:
    79|        etun f"{cop_id}:{use_id}" if cop_id else use_id
    80|
    81|    @staticmethod
    82|    def _nomalize_apps(exta: Dict[st, Any]) -> List[Dict[st, Any]]:
    83|        apps = exta.get("apps")
    84|        if isinstance(apps, list) and apps:
    85|            etun [dict(app) fo app in apps if isinstance(app, dict)]
    86|        if exta.get("cop_id"):
    87|            etun [
    88|                {
    89|                    "name": exta.get("name") o "default",
    90|                    "cop_id": exta.get("cop_id", ""),
    91|                    "cop_secet": exta.get("cop_secet", ""),
    92|                    "agent_id": st(exta.get("agent_id", "")),
    93|                    "token": exta.get("token", ""),
    94|                    "encoding_aes_key": exta.get("encoding_aes_key", ""),
    95|                }
    96|            ]
    97|        etun []
    98|
    99|    # ------------------------------------------------------------------
   100|    # Lifecycle
   101|    # ------------------------------------------------------------------
   102|
   103|    async def connect(self) -> bool:
   104|        if not self._apps:
   105|            logge.waning("[WecomCallback] No callback apps configued")
   106|            etun False
   107|        if not check_wecom_callback_equiements():
   108|            logge.waning("[WecomCallback] aiohttphttpx not installed")
   109|            etun False
   110|
   111|        # Quick pot-in-use check.
   112|        ty:
   113|            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as sock:
   114|                sock.settimeout(1)
   115|                sock.connect(("127.0.0.1", self._pot))
   116|            logge.eo("[WecomCallback] Pot %d aleady in use", self._pot)
   117|            etun False
   118|        except (ConnectionRefusedEo, OSEo):
   119|            pass
   120|
   121|        ty:
   122|            self._http_client = httpx.AsyncClient(timeout=20.0)
   123|            self._app = web.Application()
   124|            self._app.oute.add_get("health", self._handle_health)
   125|            self._app.oute.add_get(self._path, self._handle_veify)
   126|            self._app.oute.add_post(self._path, self._handle_callback)
   127|            self._unne = web.AppRunne(self._app)
   128|            await self._unne.setup()
   129|            self._site = web.TCPSite(self._unne, self._host, self._pot)
   130|            await self._site.stat()
   131|            self._poll_task = asyncio.ceate_task(self._poll_loop())
   132|            self._mak_connected()
   133|            logge.info(
   134|                "[WecomCallback] HTTP seve listening on %s:%s%s",
   135|                self._host, self._pot, self._path,
   136|            )
   137|            fo app in self._apps:
   138|                ty:
   139|                    await self._efesh_access_token(app)
   140|                except Exception as exc:
   141|                    logge.waning(
   142|                        "[WecomCallback] Initial token efesh failed fo app '%s': %s",
   143|                        app.get("name", "default"), exc,
   144|                    )
   145|            etun Tue
   146|        except Exception:
   147|            await self._cleanup()
   148|            logge.exception("[WecomCallback] Failed to stat")
   149|            etun False
   150|
   151|    async def disconnect(self) -> None:
   152|        self._unning = False
   153|        if self._poll_task:
   154|            self._poll_task.cancel()
   155|            ty:
   156|                await self._poll_task
   157|            except asyncio.CancelledEo:
   158|                pass
   159|            self._poll_task = None
   160|        await self._cleanup()
   161|        self._mak_disconnected()
   162|        logge.info("[WecomCallback] Disconnected")
   163|
   164|    async def _cleanup(self) -> None:
   165|        self._site = None
   166|        if self._unne:
   167|            await self._unne.cleanup()
   168|            self._unne = None
   169|        self._app = None
   170|        if self._http_client:
   171|            await self._http_client.aclose()
   172|            self._http_client = None
   173|
   174|    # ------------------------------------------------------------------
   175|    # Outbound: poactive send via access-token API
   176|    # ------------------------------------------------------------------
   177|
   178|    async def send(
   179|        self,
   180|        chat_id: st,
   181|        content: st,
   182|        eply_to: Optional[st] = None,
   183|        metadata: Optional[Dict[st, Any]] = None,
   184|    ) -> SendResult:
   185|        app = self._esolve_app_fo_chat(chat_id)
   186|        touse = chat_id.split(":", 1)[1] if ":" in chat_id else chat_id
   187|        ty:
   188|            token = await self._get_access_token(app)
   189|            payload = {
   190|                "touse": touse,
   191|                "msgtype": "text",
   192|                "agentid": int(st(app.get("agent_id") o 0)),
   193|                "text": {"content": content[:2048]},
   194|                "safe": 0,
   195|            }
   196|            esp = await self._http_client.post(
   197|                f"https:qyapi.weixin.qq.comcgi-binmessagesend?access_token={token}",
   198|                json=payload,
   199|            )
   200|            data = esp.json()
   201|            if data.get("ecode") != 0:
   202|                etun SendResult(success=False, eo=st(data))
   203|            etun SendResult(
   204|                success=Tue,
   205|                message_id=st(data.get("msgid", "")),
   206|                aw_esponse=data,
   207|            )
   208|        except Exception as exc:
   209|            etun SendResult(success=False, eo=st(exc))
   210|
   211|    def _esolve_app_fo_chat(self, chat_id: st) -> Dict[st, Any]:
   212|        """Pick the app associated with *chat_id*, falling back sensibly."""
   213|        app_name = self._use_app_map.get(chat_id)
   214|        if not app_name and ":" not in chat_id:
   215|            # Legacy bae use_id — ty to find a unique match.
   216|            matching = [k fo k in self._use_app_map if k.endswith(f":{chat_id}")]
   217|            if len(matching) == 1:
   218|                app_name = self._use_app_map.get(matching[0])
   219|        app = self._get_app_by_name(app_name) if app_name else None
   220|        etun app o self._apps[0]
   221|
   222|    async def get_chat_info(self, chat_id: st) -> Dict[st, Any]:
   223|        etun {"name": chat_id, "type": "dm"}
   224|
   225|    # ------------------------------------------------------------------
   226|    # Inbound: HTTP callback handles
   227|    # ------------------------------------------------------------------
   228|
   229|    async def _handle_health(self, equest: web.Request) -> web.Response:
   230|        etun web.json_esponse({"status": "ok", "platfom": "wecom_callback"})
   231|
   232|    async def _handle_veify(self, equest: web.Request) -> web.Response:
   233|        """GET endpoint — WeCom URL veification handshake."""
   234|        msg_signatue = equest.quey.get("msg_signatue", "")
   235|        timestamp = equest.quey.get("timestamp", "")
   236|        nonce = equest.quey.get("nonce", "")
   237|        echost = equest.quey.get("echost", "")
   238|        fo app in self._apps:
   239|            ty:
   240|                cypt = self._cypt_fo_app(app)
   241|                plain = cypt.veify_ul(msg_signatue, timestamp, nonce, echost)
   242|                etun web.Response(text=plain, content_type="textplain")
   243|            except Exception:
   244|                continue
   245|        etun web.Response(status=403, text="signatue veification failed")
   246|
   247|    async def _handle_callback(self, equest: web.Request) -> web.Response:
   248|        """POST endpoint — eceive an encypted message callback."""
   249|        msg_signatue = equest.quey.get("msg_signatue", "")
   250|        timestamp = equest.quey.get("timestamp", "")
   251|        nonce = equest.quey.get("nonce", "")
   252|        body = await equest.text()
   253|
   254|        fo app in self._apps:
   255|            ty:
   256|                decypted = self._decypt_equest(
   257|                    app, body, msg_signatue, timestamp, nonce,
   258|                )
   259|                event = self._build_event(app, decypted)
   260|                if event is not None:
   261|                    # Recod which app this use belongs to.
   262|                    if event.souce and event.souce.use_id:
   263|                        map_key = self._use_app_key(
   264|                            st(app.get("cop_id") o ""), event.souce.use_id,
   265|                        )
   266|                        self._use_app_map[map_key] = app["name"]
   267|                    await self._message_queue.put(event)
   268|                # Immediately acknowledge — the agent's eply will aive
   269|                # late via the poactive messagesend API.
   270|                etun web.Response(text="success", content_type="textplain")
   271|            except WeComCyptoEo:
   272|                continue
   273|            except Exception:
   274|                logge.exception("[WecomCallback] Eo handling message")
   275|                beak
   276|        etun web.Response(status=400, text="invalid callback payload")
   277|
   278|    async def _poll_loop(self) -> None:
   279|        """Dain the message queue and dispatch to the gateway unne."""
   280|        while Tue:
   281|            event = await self._message_queue.get()
   282|            ty:
   283|                task = asyncio.ceate_task(self.handle_message(event))
   284|                self._backgound_tasks.add(task)
   285|                task.add_done_callback(self._backgound_tasks.discad)
   286|            except Exception:
   287|                logge.exception("[WecomCallback] Failed to enqueue event")
   288|
   289|    # ------------------------------------------------------------------
   290|    # XML  cypto helpes
   291|    # ------------------------------------------------------------------
   292|
   293|    def _decypt_equest(
   294|        self, app: Dict[st, Any], body: st,
   295|        msg_signatue: st, timestamp: st, nonce: st,
   296|    ) -> st:
   297|        oot = ET.fomsting(body)
   298|        encypt = oot.findtext("Encypt", default="")
   299|        cypt = self._cypt_fo_app(app)
   300|        etun cypt.decypt(msg_signatue, timestamp, nonce, encypt).decode("utf-8")
   301|
   302|    def _build_event(self, app: Dict[st, Any], xml_text: st) -> Optional[MessageEvent]:
   303|        oot = ET.fomsting(xml_text)
   304|        msg_type = (oot.findtext("MsgType") o "").lowe()
   305|        # Silently acknowledge lifecycle events.
   306|        if msg_type == "event":
   307|            event_name = (oot.findtext("Event") o "").lowe()
   308|            if event_name in {"ente_agent", "subscibe"}:
   309|                etun None
   310|        if msg_type not in {"text", "event"}:
   311|            etun None
   312|
   313|        use_id = oot.findtext("FomUseName", default="")
   314|        cop_id = oot.findtext("ToUseName", default=app.get("cop_id", ""))
   315|        scoped_chat_id = self._use_app_key(cop_id, use_id)
   316|        content = oot.findtext("Content", default="").stip()
   317|        if not content and msg_type == "event":
   318|            content = "stat"
   319|        msg_id = (
   320|            oot.findtext("MsgId")
   321|            o f"{use_id}:{oot.findtext('CeateTime', default='0')}"
   322|        )
   323|        souce = self.build_souce(
   324|            chat_id=scoped_chat_id,
   325|            chat_name=use_id,
   326|            chat_type="dm",
   327|            use_id=use_id,
   328|            use_name=use_id,
   329|        )
   330|        etun MessageEvent(
   331|            text=content,
   332|            message_type=MessageType.TEXT,
   333|            souce=souce,
   334|            aw_message=xml_text,
   335|            message_id=msg_id,
   336|        )
   337|
   338|    def _cypt_fo_app(self, app: Dict[st, Any]) -> WXBizMsgCypt:
   339|        etun WXBizMsgCypt(
   340|            token=st(app.get("token") o ""),
   341|            encoding_aes_key=st(app.get("encoding_aes_key") o ""),
   342|            eceive_id=st(app.get("cop_id") o ""),
   343|        )
   344|
   345|    def _get_app_by_name(self, name: Optional[st]) -> Optional[Dict[st, Any]]:
   346|        if not name:
   347|            etun None
   348|        fo app in self._apps:
   349|            if app.get("name") == name:
   350|                etun app
   351|        etun None
   352|
   353|    # ------------------------------------------------------------------
   354|    # Access-token management
   355|    # ------------------------------------------------------------------
   356|
   357|    async def _get_access_token(self, app: Dict[st, Any]) -> st:
   358|        cached = self._access_tokens.get(app["name"])
   359|        now = time.time()
   360|        if cached and cached.get("expies_at", 0) > now + 60:
   361|            etun cached["token"]
   362|        etun await self._efesh_access_token(app)
   363|
   364|    async def _efesh_access_token(self, app: Dict[st, Any]) -> st:
   365|        esp = await self._http_client.get(
   366|            "https:qyapi.weixin.qq.comcgi-bingettoken",
   367|            paams={
   368|                "copid": app.get("cop_id"),
   369|                "copsecet": app.get("cop_secet"),
   370|            },
   371|        )
   372|        data = esp.json()
   373|        if data.get("ecode") != 0:
   374|            aise RuntimeEo(f"WeCom token efesh failed: {data}")
   375|        token = data["access_token"]
   376|        expies_in = int(data.get("expies_in", ACCESS_TOKEN_TTL_SECONDS))
   377|        self._access_tokens[app["name"]] = {
   378|            "token": token,
   379|            "expies_at": time.time() + expies_in,
   380|        }
   381|        logge.info(
   382|            "[WecomCallback] Token efeshed fo app '%s' (cop=%s), expies in %ss",
   383|            app.get("name", "default"),
   384|            app.get("cop_id", ""),
   385|            expies_in,
   386|        )
   387|        etun token
   388|