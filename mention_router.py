     1|"""
     2|Mention Router — parse @mentions in WeCom group chat messages and route
     3|to the correct multi-agent configuration.
     4|
     5|Supports:
     6|  - @Alpha, @Alpha助手, @alpha (configurable mention patterns)
     7|  - Multiple mentions in one message (returns ordered list)
     8|  - Extract agent-specific message text (strips mention markers)
     9|"""
    10|
    11|from __future__ import annotations
    12|
    13|import re
    14|from typing import Any, Dict, List, Optional, Tuple
    15|
    16|
    17|# ASCII + CJK full-width punctuation that can follow a mention
    18|_MENTION_BOUNDARY_RIGHT = r"[\s,.:;!?，。！？；：、)\]）】」』]"
    19|# Left boundary: @ must not be preceded by word chars or dots
    20|_MENTION_BOUNDARY_LEFT = r"(?<!\w)"
    21|
    22|
    23|class AgentConfig:
    24|    """Configuration for a single multi-agent participant."""
    25|
    26|    def __init__(self, agent_id: str, config: Dict[str, Any]) -> None:
    27|        self.agent_id = agent_id
    28|        self.name = str(config.get("name", agent_id))
    29|        self.mention_patterns: List[str] = [
    30|            p for p in config.get("mention_patterns", [f"@{self.name}", f"@{agent_id}"])
    31|            if p
    32|        ]
    33|        # If no patterns configured, generate defaults
    34|        if not self.mention_patterns:
    35|            self.mention_patterns = [f"@{self.name}", f"@{agent_id}"]
    36|
    37|        # Per-agent model override (optional)
    38|        self.model: Optional[str] = config.get("model")
    39|        self.system_prompt: Optional[str] = config.get("system_prompt")
    40|        self.enabled_toolsets: Optional[List[str]] = config.get("enabled_toolsets")
    41|
    42|    def __repr__(self) -> str:
    43|        return f"AgentConfig({self.agent_id!r}, name={self.name!r})"
    44|
    45|
    46|class MentionRouter:
    47|    """Parse @mentions from group chat messages and resolve to AgentConfig."""
    48|
    49|    def __init__(self, multi_agent_config: Dict[str, Any]) -> None:
    50|        """
    51|        Args:
    52|            multi_agent_config: The `multi_agent` section from wecom extra config.
    53|                Expected structure:
    54|                {
    55|                    "enabled": true,
    56|                    "default_agent": "alpha",
    57|                    "agents": {
    58|                        "alpha": {"name": "Alpha助手", "mention_patterns": ["@Alpha", "@Alpha助手"], ...},
    59|                        "beta": {...},
    60|                    },
    61|                    "cross_agent": {
    62|                        "enabled": true,
    63|                        "max_chain_length": 5,
    64|                        "chain_cooldown_seconds": 3,
    65|                    }
    66|                }
    67|        """
    68|        self.enabled: bool = bool(multi_agent_config.get("enabled", False))
    69|        self.default_agent_id: str = str(
    70|            multi_agent_config.get("default_agent", "default")
    71|        )
    72|
    73|        # Build agent registry
    74|        self.agents: Dict[str, AgentConfig] = {}
    75|        raw_agents = multi_agent_config.get("agents", {})
    76|        if isinstance(raw_agents, dict):
    77|            for agent_id, agent_cfg in raw_agents.items():
    78|                if isinstance(agent_cfg, dict):
    79|                    self.agents[agent_id] = AgentConfig(agent_id, agent_cfg)
    80|
    81|        # Cross-agent chaining config
    82|        cross_cfg = multi_agent_config.get("cross_agent", {})
    83|        self.cross_agent_enabled: bool = bool(
    84|            cross_cfg.get("enabled", True)
    85|        )
    86|        self.max_chain_length: int = int(cross_cfg.get("max_chain_length", 5))
    87|        self.chain_cooldown_seconds: float = float(
    88|            cross_cfg.get("chain_cooldown_seconds", 3)
    89|        )
    90|
    91|        # Compile mention regex patterns
    92|        self._compiled_patterns: List[Tuple[str, re.Pattern]] = []
    93|        for agent_id, agent in self.agents.items():
    94|            for pattern in agent.mention_patterns:
    95|                escaped = re.escape(pattern)
    96|                # Case-insensitive matching with boundary assertions
    97|                regex = re.compile(
    98|                    f"(?i){_MENTION_BOUNDARY_LEFT}{escaped}(?={_MENTION_BOUNDARY_RIGHT}|$)",
    99|                )
   100|                self._compiled_patterns.append((agent_id, regex))
   101|
   102|    def parse_mentions(self, text: str) -> List[str]:
   103|        """Return ordered list of agent_ids mentioned in *text* (first occurrence order)."""
   104|        if not text or not self.enabled:
   105|            return []
   106|
   107|        # Find all matches with their positions, then sort by position
   108|        matches: List[Tuple[int, str]] = []
   109|        seen_ids: set = set()
   110|        for agent_id, regex in self._compiled_patterns:
   111|            match = regex.search(text)
   112|            if match and agent_id not in seen_ids:
   113|                matches.append((match.start(), agent_id))
   114|                seen_ids.add(agent_id)
   115|
   116|        # Sort by position in text (first mention first)
   117|        matches.sort(key=lambda x: x[0])
   118|        return [agent_id for _, agent_id in matches]
   119|
   120|    def resolve_target_agents(self, text: str) -> List[str]:
   121|        """Return list of agent_ids to trigger. Empty list means use default."""
   122|        mentions = self.parse_mentions(text)
   123|        if mentions:
   124|            return mentions
   125|        # No mention — return empty, caller should use default_agent_id
   126|        return []
   127|
   128|    def get_agent_config(self, agent_id: str) -> Optional[AgentConfig]:
   129|        """Get AgentConfig by agent_id."""
   130|        return self.agents.get(agent_id)
   131|
   132|    def extract_clean_text(self, text: str) -> str:
   133|        """Remove @mention markers from text, return clean message."""
   134|        result = text
   135|        for _, regex in self._compiled_patterns:
   136|            result = regex.sub("", result).strip()
   137|        # Clean up extra whitespace
   138|        result = re.sub(r"\n{3,}", "\n\n", result)
   139|        return result.strip() or text
   140|
   141|    def extract_mentions_from_response(self, response_text: str) -> List[str]:
   142|        """
   143|        Scan an agent's response text for @mentions of other agents.
   144|        Used for cross-agent chaining.
   145|        """
   146|        return self.parse_mentions(response_text)
   147|
   148|    @classmethod
   149|    def from_wecom_extra(cls, extra: Dict[str, Any]) -> "MentionRouter":
   150|        """Create a MentionRouter from the WeCom adapter's extra config dict."""
   151|        multi_agent = extra.get("multi_agent", {})
   152|        if not isinstance(multi_agent, dict):
   153|            multi_agent = {}
   154|        return cls(multi_agent)
   155|