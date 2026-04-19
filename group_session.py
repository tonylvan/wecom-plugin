"""
Group Session Store — manages multi-agent conversation state within a
WeCom group chat.

Unlike the main SessionStore (which manages one conversation per chat),
this tracks the state of multi-agent "discussion chains" — which agents
have been triggered, their responses, chain depth, and user interruption.

This is an in-memory store (reset on gateway restart) because discussion
chains are ephemeral and should not persist across restarts.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class AgentTurnRecord:
    """Record of one agent's turn in a group discussion chain."""
    agent_id: str
    agent_name: str
    request_text: str          # The text this agent was asked to respond to
    response_text: str         # The agent's response
    mentions_in_response: List[str]  # @mentions found in the response
    started_at: float
    completed_at: float


@dataclass
class GroupDiscussionChain:
    """State of an active multi-agent discussion chain in a group chat."""
    chat_id: str                          # The WeCom group chat ID
    original_user_message: str            # The user's original message
    original_sender_id: str               # The user who started the chain
    triggered_agents: List[str] = field(default_factory=list)  # Order of agents triggered
    turn_records: List[AgentTurnRecord] = field(default_factory=list)
    chain_depth: int = 0                  # Current chain depth (user→A = 1, A→B = 2, ...)
    max_chain_length: int = 5
    started_at: float = field(default_factory=time.time)
    completed: bool = False
    interrupted_by_user: bool = False
    # Cooldown tracking to prevent rapid-fire agent triggers
    last_trigger_at: float = 0.0
    cooldown_seconds: float = 3.0

    def can_trigger_next(self, target_agent_id: str) -> bool:
        """Check if we can trigger another agent in the chain."""
        # Can't trigger same agent twice in one chain
        if target_agent_id in self.triggered_agents:
            return False
        # Chain length check
        if self.chain_depth >= self.max_chain_length:
            return False
        # Cooldown check
        if self.last_trigger_at > 0:
            elapsed = time.time() - self.last_trigger_at
            if elapsed < self.cooldown_seconds:
                return False
        return True

    def add_turn(self, record: AgentTurnRecord) -> None:
        """Record an agent's completed turn."""
        self.turn_records.append(record)
        if record.agent_id not in self.triggered_agents:
            self.triggered_agents.append(record.agent_id)
        self.chain_depth += 1
        self.last_trigger_at = time.time()

    def get_conversation_context(self) -> str:
        """Build conversation context string for the next agent."""
        if not self.turn_records:
            return self.original_user_message

        lines = [f"[User] {self.original_user_message}"]
        for turn in self.turn_records:
            lines.append(f"[{turn.agent_name}] {turn.response_text}")
        return "\n\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "original_message": self.original_user_message,
            "sender_id": self.original_sender_id,
            "triggered_agents": self.triggered_agents,
            "chain_depth": self.chain_depth,
            "max_chain_length": self.max_chain_length,
            "started_at": self.started_at,
            "completed": self.completed,
        }


class GroupSessionStore:
    """In-memory store for active group discussion chains."""

    def __init__(self) -> None:
        # chat_id → GroupDiscussionChain
        self._chains: Dict[str, GroupDiscussionChain] = {}
        self._lock = asyncio.Lock()

    async def get_or_create_chain(
        self,
        chat_id: str,
        user_message: str,
        sender_id: str,
        max_chain_length: int = 5,
        cooldown_seconds: float = 3.0,
    ) -> GroupDiscussionChain:
        """Get existing chain or create a new one for this group chat."""
        async with self._lock:
            chain = self._chains.get(chat_id)
            if chain and not chain.completed and not chain.interrupted_by_user:
                return chain

            # Create new chain
            new_chain = GroupDiscussionChain(
                chat_id=chat_id,
                original_user_message=user_message,
                original_sender_id=sender_id,
                max_chain_length=max_chain_length,
                cooldown_seconds=cooldown_seconds,
            )
            self._chains[chat_id] = new_chain
            return new_chain

    async def get_chain(self, chat_id: str) -> Optional[GroupDiscussionChain]:
        """Get existing chain, if any."""
        async with self._lock:
            return self._chains.get(chat_id)

    async def complete_chain(self, chat_id: str) -> None:
        """Mark a chain as completed."""
        async with self._lock:
            chain = self._chains.get(chat_id)
            if chain:
                chain.completed = True

    async def interrupt_chain(self, chat_id: str) -> None:
        """Mark a chain as interrupted by user."""
        async with self._lock:
            chain = self._chains.get(chat_id)
            if chain:
                chain.interrupted_by_user = True

    async def clear_chain(self, chat_id: str) -> None:
        """Remove a chain from the store."""
        async with self._lock:
            self._chains.pop(chat_id, None)

    async def is_chain_active(self, chat_id: str) -> bool:
        """Check if there's an active (incomplete, non-interrupted) chain."""
        async with self._lock:
            chain = self._chains.get(chat_id)
            return chain is not None and not chain.completed and not chain.interrupted_by_user

    async def cleanup_expired(self, max_age_seconds: float = 300.0) -> int:
        """Remove chains older than max_age_seconds. Returns count removed."""
        async with self._lock:
            now = time.time()
            expired = [
                cid for cid, chain in self._chains.items()
                if now - chain.started_at > max_age_seconds
            ]
            for cid in expired:
                del self._chains[cid]
            return len(expired)


# Module-level singleton
_group_session_store: Optional[GroupSessionStore] = None


def get_group_session_store() -> GroupSessionStore:
    """Get the global GroupSessionStore singleton."""
    global _group_session_store
    if _group_session_store is None:
        _group_session_store = GroupSessionStore()
    return _group_session_store


def reset_group_session_store() -> None:
    """Reset the singleton (useful for testing)."""
    global _group_session_store
    _group_session_store = None