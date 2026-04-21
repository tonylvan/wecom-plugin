# WeCom Gateway Plugin

Hermes Agent 企业微信（WeCom）网关插件。

## 文件说明

- `wecom.py` - 企业微信 WebSocket 模式适配器（主要文件）
- `wecom_callback.py` - 企业微信 HTTP Callback 模式适配器
- `wecom_crypto.py` - 企业微信消息加解密模块
- `mention_router.py` - 群聊 @mention 解析器（支持多 Agent 群聊）
- `group_session.py` - 群聊会话管理

## 多 Agent 群聊支持

支持在群聊中 @不同的 Agent，实现多 Agent 群聊讨论：

- 用户可以 `@AgentA @AgentB` 同时触发多个 Agent
- Agent 回复中可以 `@AgentC` 触发其他 Agent（链式对话）
- 自动防止无限循环（maxChainLength 配置）

## 配置示例

```yaml
# ~/.hermes/config.yaml
gateway:
  platforms:
    wecom:
      botId: "your-bot-id"
      secret: "your-secret"
      
  # 多 Agent 群聊配置
  multiAgent:
    enabled: true
    crossAgent:
      enabled: true
      maxChainLength: 5
      chainCooldownSeconds: 3
```

## License

MIT
