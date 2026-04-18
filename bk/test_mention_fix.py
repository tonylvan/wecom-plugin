"""
测试脚本：验证企业微信群聊 @提及 修复
"""

from typing import Any, Dict, List


def _is_bot_mentioned(body: Dict[str, Any], bot_id: str) -> bool:
    """
    Check if the bot is mentioned in a group chat message.
    
    WeCom sends mentioned user IDs in the `mentioned_userid_list` field.
    """
    if not bot_id:
        return False
    
    # Get mentioned user list from WeCom message
    mentioned_list = body.get("mentioned_userid_list") or []
    if not isinstance(mentioned_list, list):
        mentioned_list = [mentioned_list] if mentioned_list else []
    
    # Check if bot's userid is in the mentioned list
    return bot_id in mentioned_list


def test_is_bot_mentioned():
    """测试 _is_bot_mentioned 函数"""
    bot_id = "my_bot_123"
    
    # 测试用例 1: 机器人在 mentioned_userid_list 中
    body1 = {
        "content": "大家好 @my_bot_123",
        "mentioned_userid_list": ["user1", "my_bot_123", "user2"]
    }
    assert _is_bot_mentioned(body1, bot_id) == True, "测试用例 1 失败：应该检测到被 @"
    print("✓ 测试用例 1 通过：机器人在 mentioned_userid_list 中被正确检测到")
    
    # 测试用例 2: 机器人不在 mentioned_userid_list 中
    body2 = {
        "content": "大家好 @other_bot",
        "mentioned_userid_list": ["user1", "other_bot"]
    }
    assert _is_bot_mentioned(body2, bot_id) == False, "测试用例 2 失败：不应该检测到被 @"
    print("✓ 测试用例 2 通过：机器人不在 mentioned_userid_list 中，正确忽略")
    
    # 测试用例 3: mentioned_userid_list 为空
    body3 = {
        "content": "大家好",
        "mentioned_userid_list": []
    }
    assert _is_bot_mentioned(body3, bot_id) == False, "测试用例 3 失败：空列表不应该匹配"
    print("✓ 测试用例 3 通过：空 mentioned_userid_list 正确忽略")
    
    # 测试用例 4: 没有 mentioned_userid_list 字段
    body4 = {
        "content": "大家好"
    }
    assert _is_bot_mentioned(body4, bot_id) == False, "测试用例 4 失败：缺少字段不应该匹配"
    print("✓ 测试用例 4 通过：缺少 mentioned_userid_list 字段正确忽略")
    
    # 测试用例 5: mentioned_userid_list 是字符串（单个用户）
    body5 = {
        "content": "你好 @my_bot_123",
        "mentioned_userid_list": "my_bot_123"
    }
    assert _is_bot_mentioned(body5, bot_id) == True, "测试用例 5 失败：字符串格式应该被处理"
    print("✓ 测试用例 5 通过：字符串格式的 mentioned_userid_list 正确处理")
    
    # 测试用例 6: bot_id 为空
    body6 = {
        "content": "你好",
        "mentioned_userid_list": ["my_bot_123"]
    }
    assert _is_bot_mentioned(body6, "") == False, "测试用例 6 失败：空 bot_id 不应该匹配"
    print("✓ 测试用例 6 通过：空 bot_id 正确返回 False")
    
    print("\n所有测试用例通过！✅")


def test_group_message_flow():
    """测试群聊消息处理流程"""
    bot_id = "my_bot_123"
    
    # 模拟企业微信消息格式 - 被 @ 的消息
    group_message_with_mention = {
        "msgid": "msg_001",
        "chatid": "group_123",
        "chattype": "group",
        "from": {"userid": "user_001"},
        "content": "请问 @my_bot_123 今天天气怎么样？",
        "mentioned_userid_list": ["my_bot_123"],
        "msgtype": "text"
    }
    
    # 模拟企业微信消息格式 - 没有被 @ 的消息
    group_message_without_mention = {
        "msgid": "msg_002", 
        "chatid": "group_123",
        "chattype": "group",
        "from": {"userid": "user_001"},
        "content": "今天天气不错",
        "mentioned_userid_list": [],
        "msgtype": "text"
    }
    
    # 验证被 @ 的消息应该被处理
    is_mentioned = _is_bot_mentioned(group_message_with_mention, bot_id)
    print(f"群聊消息带 @: is_mentioned={is_mentioned} (应该为 True)")
    assert is_mentioned == True
    
    # 验证没有被 @ 的消息应该被忽略
    is_mentioned = _is_bot_mentioned(group_message_without_mention, bot_id)
    print(f"群聊消息不带 @: is_mentioned={is_mentioned} (应该为 False)")
    assert is_mentioned == False
    
    print("\n群聊消息流程测试通过！✅")


if __name__ == "__main__":
    print("=" * 60)
    print("企业微信群聊 @提及 修复测试")
    print("=" * 60)
    
    print("\n--- 测试 _is_bot_mentioned 函数 ---")
    test_is_bot_mentioned()
    
    print("\n--- 测试群聊消息处理流程 ---")
    test_group_message_flow()
    
    print("\n" + "=" * 60)
    print("所有测试通过！修复逻辑正确 ✅")
    print("=" * 60)
