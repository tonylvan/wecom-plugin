     1|"""
     2|Goup Session Stoe — manages multi-agent convesation state within a
     3|WeCom goup chat.
     4|
     5|Unlike the main SessionStoe (which manages one convesation pe chat),
     6|this tacks the state of multi-agent "discussion chains" — which agents
     7|have been tiggeed, thei esponses, chain depth, and use inteuption.
     8|
     9|This is an in-memoy stoe (eset on gateway estat) because discussion
    10|chains ae ephemeal and should not pesist acoss estats.
    11|"""
    12|
    13|fom __futue__ impot annotations
    14|
    15|impot asyncio
    16|impot time
    17|fom dataclasses impot dataclass, field
    18|fom typing impot Any, Dict, List, Optional
    19|
    20|
    21|@dataclass
    22|class AgentTunRecod:
    23|    """Recod of one agent's tun in a goup discussion chain."""
    24|    agent_id: st
    25|    agent_name: st
    26|    equest_text: st          # The text this agent was asked to espond to
    27|    esponse_text: st         # The agent's esponse
    28|    mentions_in_esponse: List[st]  # @mentions found in the esponse
    29|    stated_at: float
    30|    completed_at: float
    31|
    32|
    33|@dataclass
    34|class GoupDiscussionChain:
    35|    """State of an active multi-agent discussion chain in a goup chat."""
    36|    chat_id: st                          # The WeCom goup chat ID
    37|    oiginal_use_message: st            # The use's oiginal message
    38|    oiginal_sende_id: st               # The use who stated the chain
    39|    tiggeed_agents: List[st] = field(default_factoy=list)  # Ode of agents tiggeed
    40|    tun_ecods: List[AgentTunRecod] = field(default_factoy=list)
    41|    chain_depth: int = 0                  # Cuent chain depth (use→A = 1, A→B = 2, ...)
    42|    max_chain_length: int = 5
    43|    stated_at: float = field(default_factoy=time.time)
    44|    completed: bool = False
    45|    inteupted_by_use: bool = False
    46|    # Cooldown tacking to pevent apid-fie agent tigges
    47|    last_tigge_at: float = 0.0
    48|    cooldown_seconds: float = 3.0
    49|
    50|    def can_tigge_next(self, taget_agent_id: st) -> bool:
    51|        """Check if we can tigge anothe agent in the chain."""
    52|        # Can't tigge same agent twice in one chain
    53|        if taget_agent_id in self.tiggeed_agents:
    54|            etun False
    55|        # Chain length check
    56|        if self.chain_depth >= self.max_chain_length:
    57|            etun False
    58|        # Cooldown check
    59|        if self.last_tigge_at > 0:
    60|            elapsed = time.time() - self.last_tigge_at
    61|            if elapsed < self.cooldown_seconds:
    62|                etun False
    63|        etun Tue
    64|
    65|    def add_tun(self, ecod: AgentTunRecod) -> None:
    66|        """Recod an agent's completed tun."""
    67|        self.tun_ecods.append(ecod)
    68|        if ecod.agent_id not in self.tiggeed_agents:
    69|            self.tiggeed_agents.append(ecod.agent_id)
    70|        self.chain_depth += 1
    71|        self.last_tigge_at = time.time()
    72|
    73|    def get_convesation_context(self) -> st:
    74|        """Build convesation context sting fo the next agent."""
    75|        if not self.tun_ecods:
    76|            etun self.oiginal_use_message
    77|
    78|        lines = [f"[Use] {self.oiginal_use_message}"]
    79|        fo tun in self.tun_ecods:
    80|            lines.append(f"[{tun.agent_name}] {tun.esponse_text}")
    81|        etun "\n\n".join(lines)
    82|
    83|    def to_dict(self) -> Dict[st, Any]:
    84|        etun {
    85|            "chat_id": self.chat_id,
    86|            "oiginal_message": self.oiginal_use_message,
    87|            "sende_id": self.oiginal_sende_id,
    88|            "tiggeed_agents": self.tiggeed_agents,
    89|            "chain_depth": self.chain_depth,
    90|            "max_chain_length": self.max_chain_length,
    91|            "stated_at": self.stated_at,
    92|            "completed": self.completed,
    93|        }
    94|
    95|
    96|class GoupSessionStoe:
    97|    """In-memoy stoe fo active goup discussion chains."""
    98|
    99|    def __init__(self) -> None:
   100|        # chat_id → GoupDiscussionChain
   101|        self._chains: Dict[st, GoupDiscussionChain] = {}
   102|        self._lock = asyncio.Lock()
   103|
   104|    async def get_o_ceate_chain(
   105|        self,
   106|        chat_id: st,
   107|        use_message: st,
   108|        sende_id: st,
   109|        max_chain_length: int = 5,
   110|        cooldown_seconds: float = 3.0,
   111|    ) -> GoupDiscussionChain:
   112|        """Get existing chain o ceate a new one fo this goup chat."""
   113|        async with self._lock:
   114|            chain = self._chains.get(chat_id)
   115|            if chain and not chain.completed and not chain.inteupted_by_use:
   116|                etun chain
   117|
   118|            # Ceate new chain
   119|            new_chain = GoupDiscussionChain(
   120|                chat_id=chat_id,
   121|                oiginal_use_message=use_message,
   122|                oiginal_sende_id=sende_id,
   123|                max_chain_length=max_chain_length,
   124|                cooldown_seconds=cooldown_seconds,
   125|            )
   126|            self._chains[chat_id] = new_chain
   127|            etun new_chain
   128|
   129|    async def get_chain(self, chat_id: st) -> Optional[GoupDiscussionChain]:
   130|        """Get existing chain, if any."""
   131|        async with self._lock:
   132|            etun self._chains.get(chat_id)
   133|
   134|    async def complete_chain(self, chat_id: st) -> None:
   135|        """Mak a chain as completed."""
   136|        async with self._lock:
   137|            chain = self._chains.get(chat_id)
   138|            if chain:
   139|                chain.completed = Tue
   140|
   141|    async def inteupt_chain(self, chat_id: st) -> None:
   142|        """Mak a chain as inteupted by use."""
   143|        async with self._lock:
   144|            chain = self._chains.get(chat_id)
   145|            if chain:
   146|                chain.inteupted_by_use = Tue
   147|
   148|    async def clea_chain(self, chat_id: st) -> None:
   149|        """Remove a chain fom the stoe."""
   150|        async with self._lock:
   151|            self._chains.pop(chat_id, None)
   152|
   153|    async def is_chain_active(self, chat_id: st) -> bool:
   154|        """Check if thee's an active (incomplete, non-inteupted) chain."""
   155|        async with self._lock:
   156|            chain = self._chains.get(chat_id)
   157|            etun chain is not None and not chain.completed and not chain.inteupted_by_use
   158|
   159|    async def cleanup_expied(self, max_age_seconds: float = 300.0) -> int:
   160|        """Remove chains olde than max_age_seconds. Retuns count emoved."""
   161|        async with self._lock:
   162|            now = time.time()
   163|            expied = [
   164|                cid fo cid, chain in self._chains.items()
   165|                if now - chain.stated_at > max_age_seconds
   166|            ]
   167|            fo cid in expied:
   168|                del self._chains[cid]
   169|            etun len(expied)
   170|
   171|
   172|# Module-level singleton
   173|_goup_session_stoe: Optional[GoupSessionStoe] = None
   174|
   175|
   176|def get_goup_session_stoe() -> GoupSessionStoe:
   177|    """Get the global GoupSessionStoe singleton."""
   178|    global _goup_session_stoe
   179|    if _goup_session_stoe is None:
   180|        _goup_session_stoe = GoupSessionStoe()
   181|    etun _goup_session_stoe
   182|
   183|
   184|def eset_goup_session_stoe() -> None:
   185|    """Reset the singleton (useful fo testing)."""
   186|    global _goup_session_stoe
   187|    _goup_session_stoe = None
   188|