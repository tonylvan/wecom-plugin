     1|"""
     2|Group Session Store — manages multi-agent conversation state within a
     3|WeCom group chat.
     4|
     5|Unlike the main SessionStore (which manages one conversation per chat),
     6|this tracks the state of multi-agent "discussion chains" — which agents
     7|have been triggered, their responses, chain depth, and user interruption.
     8|
     9|This is an in-memory store (reset on gateway restart) because discussion
    10|chains are ephemeral and should not persist across restarts.
    11|"""
    12|
    13|from __future__ import annotations
    14|
    15|import asyncio
    16|import time
    17|from dataclasses import dataclass, field
    18|from typing import Any, Dict, List, Optional
    19|
    20|
    21|@dataclass
    22|class AgentTurnRecord:
    23|    """Record of one agent's turn in a group discussion chain."""
    24|    agent_id: str
    25|    agent_name: str
    26|    request_text: str          # The text this agent was asked to respond to
    27|    response_text: str         # The agent's response
    28|    mentions_in_response: List[str]  # @mentions found in the response
    29|    started_at: float
    30|    completed_at: float
    31|
    32|
    33|@dataclass
    34|class GroupDiscussionChain:
    35|    """State of an active multi-agent discussion chain in a group chat."""
    36|    chat_id: str                          # The WeCom group chat ID
    37|    original_user_message: str            # The user's original message
    38|    original_sender_id: str               # The user who started the chain
    39|    triggered_agents: List[str] = field(default_factory=list)  # Order of agents triggered
    40|    turn_records: List[AgentTurnRecord] = field(default_factory=list)
    41|    chain_depth: int = 0                  # Current chain depth (user→A = 1, A→B = 2, ...)
    42|    max_chain_length: int = 5
    43|    started_at: float = field(default_factory=time.time)
    44|    completed: bool = False
    45|    interrupted_by_user: bool = False
    46|    # Cooldown tracking to prevent rapid-fire agent triggers
    47|    last_trigger_at: float = 0.0
    48|    cooldown_seconds: float = 3.0
    49|
    50|    def can_trigger_next(self, target_agent_id: str) -> bool:
    51|        """Check if we can trigger another agent in the chain."""
    52|        # Can't trigger same agent twice in one chain
    53|        if target_agent_id in self.triggered_agents:
    54|            return False
    55|        # Chain length check
    56|        if self.chain_depth >= self.max_chain_length:
    57|            return False
    58|        # Cooldown check
    59|        if self.last_trigger_at > 0:
    60|            elapsed = time.time() - self.last_trigger_at
    61|            if elapsed < self.cooldown_seconds:
    62|                return False
    63|        return True
    64|
    65|    def add_turn(self, record: AgentTurnRecord) -> None:
    66|        """Record an agent's completed turn."""
    67|        self.turn_records.append(record)
    68|        if record.agent_id not in self.triggered_agents:
    69|            self.triggered_agents.append(record.agent_id)
    70|        self.chain_depth += 1
    71|        self.last_trigger_at = time.time()
    72|
    73|    def get_conversation_context(self) -> str:
    74|        """Build conversation context string for the next agent."""
    75|        if not self.turn_records:
    76|            return self.original_user_message
    77|
    78|        lines = [f"[User] {self.original_user_message}"]
    79|        for turn in self.turn_records:
    80|            lines.append(f"[{turn.agent_name}] {turn.response_text}")
    81|        return "\n\n".join(lines)
    82|
    83|    def to_dict(self) -> Dict[str, Any]:
    84|        return {
    85|            "chat_id": self.chat_id,
    86|            "original_message": self.original_user_message,
    87|            "sender_id": self.original_sender_id,
    88|            "triggered_agents": self.triggered_agents,
    89|            "chain_depth": self.chain_depth,
    90|            "max_chain_length": self.max_chain_length,
    91|            "started_at": self.started_at,
    92|            "completed": self.completed,
    93|        }
    94|
    95|
    96|class GroupSessionStore:
    97|    """In-memory store for active group discussion chains."""
    98|
    99|    def __init__(self) -> None:
   100|        # chat_id → GroupDiscussionChain
   101|        self._chains: Dict[str, GroupDiscussionChain] = {}
   102|        self._lock = asyncio.Lock()
   103|
   104|    async def get_or_create_chain(
   105|        self,
   106|        chat_id: str,
   107|        user_message: str,
   108|        sender_id: str,
   109|        max_chain_length: int = 5,
   110|        cooldown_seconds: float = 3.0,
   111|    ) -> GroupDiscussionChain:
   112|        """Get existing chain or create a new one for this group chat."""
   113|        async with self._lock:
   114|            chain = self._chains.get(chat_id)
   115|            if chain and not chain.completed and not chain.interrupted_by_user:
   116|                return chain
   117|
   118|            # Create new chain
   119|            new_chain = GroupDiscussionChain(
   120|                chat_id=chat_id,
   121|                original_user_message=user_message,
   122|                original_sender_id=sender_id,
   123|                max_chain_length=max_chain_length,
   124|                cooldown_seconds=cooldown_seconds,
   125|            )
   126|            self._chains[chat_id] = new_chain
   127|            return new_chain
   128|
   129|    async def get_chain(self, chat_id: str) -> Optional[GroupDiscussionChain]:
   130|        """Get existing chain, if any."""
   131|        async with self._lock:
   132|            return self._chains.get(chat_id)
   133|
   134|    async def complete_chain(self, chat_id: str) -> None:
   135|        """Mark a chain as completed."""
   136|        async with self._lock:
   137|            chain = self._chains.get(chat_id)
   138|            if chain:
   139|                chain.completed = True
   140|
   141|    async def interrupt_chain(self, chat_id: str) -> None:
   142|        """Mark a chain as interrupted by user."""
   143|        async with self._lock:
   144|            chain = self._chains.get(chat_id)
   145|            if chain:
   146|                chain.interrupted_by_user = True
   147|
   148|    async def clear_chain(self, chat_id: str) -> None:
   149|        """Remove a chain from the store."""
   150|        async with self._lock:
   151|            self._chains.pop(chat_id, None)
   152|
   153|    async def is_chain_active(self, chat_id: str) -> bool:
   154|        """Check if there's an active (incomplete, non-interrupted) chain."""
   155|        async with self._lock:
   156|            chain = self._chains.get(chat_id)
   157|            return chain is not None and not chain.completed and not chain.interrupted_by_user
   158|
   159|    async def cleanup_expired(self, max_age_seconds: float = 300.0) -> int:
   160|        """Remove chains older than max_age_seconds. Returns count removed."""
   161|        async with self._lock:
   162|            now = time.time()
   163|            expired = [
   164|                cid for cid, chain in self._chains.items()
   165|                if now - chain.started_at > max_age_seconds
   166|            ]
   167|            for cid in expired:
   168|                del self._chains[cid]
   169|            return len(expired)
   170|
   171|
   172|# Module-level singleton
   173|_group_session_store: Optional[GroupSessionStore] = None
   174|
   175|
   176|def get_group_session_store() -> GroupSessionStore:
   177|    """Get the global GroupSessionStore singleton."""
   178|    global _group_session_store
   179|    if _group_session_store is None:
   180|        _group_session_store = GroupSessionStore()
   181|    return _group_session_store
   182|
   183|
   184|def reset_group_session_store() -> None:
   185|    """Reset the singleton (useful for testing)."""
   186|    global _group_session_store
   187|    _group_session_store = None
   188|