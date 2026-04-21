"""
Mention Router — parse @mentions in WeCom group chat messages and route
to the correct multi-agent configuration.

Supports:
  - @Alpha, @Alpha助手, @alpha (configurable mention patterns)
  - Multiple mentions in one message (returns ordered list)
  - Extract agent-specific message text (strips mention markers)
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple


# ASCII + CJK full-width punctuation that can follow a mention
# Updated to allow CJK characters and other non-ASCII chars as boundaries.
# This supports inputs like "@HERMES你" where the mention is followed immediately by text.
_MENTION_BOUNDARY_RIGHT = r"(?![a-zA-Z0-9_])"
# Left boundary: @ must not be preceded by ASCII alphanumerics or @
_MENTION_BOUNDARY_LEFT = r"(?<![a-zA-Z0-9_@])"


class AgentConfig:
    """Configuration for a single multi-agent participant."""

    def __init__(self, agent_id: str, config: Dict[str, Any]) -> None:
        self.agent_id = agent_id
        self.name = str(config.get("name", agent_id))
        self.mention_patterns: List[str] = [
            p for p in config.get("mention_patterns", [f"@{self.name}", f"@{agent_id}"])
            if p
        ]
        # If no patterns configured, generate defaults
        if not self.mention_patterns:
            self.mention_patterns = [f"@{self.name}", f"@{agent_id}"]

        # Per-agent model override (optional)
        self.model: Optional[str] = config.get("model")
        self.system_prompt: Optional[str] = config.get("system_prompt")
        self.enabled_toolsets: Optional[List[str]] = config.get("enabled_toolsets")

    def __repr__(self) -> str:
        return f"AgentConfig({self.agent_id!r}, name={self.name!r})"


class MentionRouter:
    """Parse @mentions from group chat messages and resolve to AgentConfig."""

    def __init__(self, multi_agent_config: Dict[str, Any]) -> None:
        """
        Args:
            multi_agent_config: The `multi_agent` section from wecom extra config.
                Expected structure:
                {
                    "enabled": true,
                    "default_agent": "alpha",
                    "agents": {
                        "alpha": {"name": "Alpha助手", "mention_patterns": ["@Alpha", "@Alpha助手"], ...},
                        "beta": {...},
                    },
                    "cross_agent": {
                        "enabled": true,
                        "max_chain_length": 5,
                        "chain_cooldown_seconds": 3,
                    }
                }
        """
        self.enabled: bool = bool(multi_agent_config.get("enabled", False))
        self.default_agent_id: str = str(
            multi_agent_config.get("default_agent", "default")
        )

        # Host agent — always receives all group messages for summarization
        self.host_agent_id: str = str(
            multi_agent_config.get("host_agent", "")
        ).strip()
        self.host_always_active: bool = bool(
            multi_agent_config.get("host_always_active", True)
        )

        # Build agent registry
        self.agents: Dict[str, AgentConfig] = {}
        raw_agents = multi_agent_config.get("agents", {})
        if isinstance(raw_agents, dict):
            for agent_id, agent_cfg in raw_agents.items():
                if isinstance(agent_cfg, dict):
                    self.agents[agent_id] = AgentConfig(agent_id, agent_cfg)

        # Cross-agent chaining config
        cross_cfg = multi_agent_config.get("cross_agent", {})
        self.cross_agent_enabled: bool = bool(
            cross_cfg.get("enabled", True)
        )
        self.max_chain_length: int = int(cross_cfg.get("max_chain_length", 5))
        self.chain_cooldown_seconds: float = float(
            cross_cfg.get("chain_cooldown_seconds", 3)
        )

        # Compile mention regex patterns
        self._compiled_patterns: List[Tuple[str, re.Pattern]] = []
        for agent_id, agent in self.agents.items():
            for pattern in agent.mention_patterns:
                escaped = re.escape(pattern)
                # Case-insensitive matching with boundary assertions
                regex = re.compile(
                    f"(?i){_MENTION_BOUNDARY_LEFT}{escaped}(?={_MENTION_BOUNDARY_RIGHT}|$)",
                )
                self._compiled_patterns.append((agent_id, regex))

    def parse_mentions(self, text: str) -> List[str]:
        """Return ordered list of agent_ids mentioned in *text* (first occurrence order)."""
        if not text or not self.enabled:
            return []

        # Find all matches with their positions, then sort by position
        matches: List[Tuple[int, str]] = []
        seen_ids: set = set()
        for agent_id, regex in self._compiled_patterns:
            match = regex.search(text)
            if match and agent_id not in seen_ids:
                matches.append((match.start(), agent_id))
                seen_ids.add(agent_id)

        # Sort by position in text (first mention first)
        matches.sort(key=lambda x: x[0])
        return [agent_id for _, agent_id in matches]

    def resolve_target_agents(self, text: str) -> List[str]:
        """Return list of agent_ids to trigger. Empty list means use default."""
        mentions = self.parse_mentions(text)
        if mentions:
            return mentions
        # No mention — return empty, caller should use default_agent_id
        return []

    def get_agent_config(self, agent_id: str) -> Optional[AgentConfig]:
        """Get AgentConfig by agent_id."""
        return self.agents.get(agent_id)

    def extract_clean_text(self, text: str) -> str:
        """Remove @mention markers from text, return clean message."""
        result = text
        for _, regex in self._compiled_patterns:
            result = regex.sub("", result).strip()
        # Clean up extra whitespace
        result = re.sub(r"\n{3,}", "\n\n", result)
        return result.strip() or text

    def extract_mentions_from_response(self, response_text: str) -> List[str]:
        """
        Scan an agent's response text for @mentions of other agents.
        Used for cross-agent chaining.
        """
        return self.parse_mentions(response_text)

    @classmethod
    def from_wecom_extra(cls, extra: Dict[str, Any]) -> "MentionRouter":
        """Create a MentionRouter from the WeCom adapter's extra config dict."""
        multi_agent = extra.get("multi_agent", {})
        if not isinstance(multi_agent, dict):
            multi_agent = {}
        return cls(multi_agent)
