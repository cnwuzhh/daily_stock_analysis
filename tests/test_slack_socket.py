# -*- coding: utf-8 -*-

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
import sys
import types

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

if "markdown2" not in sys.modules:
    markdown2_stub = types.ModuleType("markdown2")
    markdown2_stub.markdown = lambda text, *args, **kwargs: text
    sys.modules["markdown2"] = markdown2_stub


class SlackSocketModeTestCase(unittest.TestCase):
    def test_should_handle_dm_and_mentions_only(self):
        from bot.platforms.slack_socket import _should_handle_message

        self.assertTrue(_should_handle_message({"channel_type": "im", "text": "用价值投资分析五粮液"}, "U_BOT"))
        self.assertTrue(_should_handle_message({"channel_type": "channel", "text": "<@U_BOT> 用价值投资分析五粮液"}, "U_BOT"))
        self.assertFalse(_should_handle_message({"channel_type": "channel", "text": "用价值投资分析五粮液"}, "U_BOT"))
        self.assertFalse(_should_handle_message({"channel_type": "channel", "text": "<@U_BOT>   "}, "U_BOT"))

    def test_start_bot_stream_clients_starts_slack_socket_when_enabled(self):
        config = SimpleNamespace(
            dingtalk_stream_enabled=False,
            feishu_stream_enabled=False,
            slack_socket_enabled=True,
        )

        def _simulate_start_bot_stream_clients(cfg):
            if getattr(cfg, "slack_socket_enabled", False):
                from bot.platforms import start_slack_socket_background, SLACK_SDK_AVAILABLE
                if SLACK_SDK_AVAILABLE:
                    start_slack_socket_background()

        with patch("bot.platforms.SLACK_SDK_AVAILABLE", True), \
             patch("bot.platforms.start_slack_socket_background", return_value=True) as mock_start:
            _simulate_start_bot_stream_clients(config)

        mock_start.assert_called_once()

    def test_slack_socket_processes_value_investing_slash_command(self):
        from bot.platforms.slack_socket import SlackSocketClient

        client = SlackSocketClient.__new__(SlackSocketClient)
        client._web_client = MagicMock()

        fake_executor = MagicMock()
        fake_executor.chat.return_value = SimpleNamespace(success=True, content="分析完成", error=None)
        fake_config = SimpleNamespace()

        with patch("src.config.get_config", return_value=fake_config), \
             patch("src.agent.factory.build_agent_executor", return_value=fake_executor), \
             patch.object(client, "_detect_skill", return_value=("value_investing", "价值投资")), \
             patch.object(client, "_post_message") as mock_post, \
             patch("api.v1.endpoints.agent._prefetch_value_investing_context", return_value={"skills": ["value_investing"], "stock_code": "000858", "stock_name": "五粮液"}):
            client._process_slash_command({"text": "用价值投资分析五粮液", "channel_id": "C1", "user_id": "U1"})

        fake_executor.chat.assert_called_once()
        mock_post.assert_called_once()
        posted_text = mock_post.call_args.args[1]
        self.assertIn("000858", posted_text)
        self.assertIn("五粮液", posted_text)

    def test_slack_socket_processes_dm_message_event(self):
        from bot.platforms.slack_socket import SlackSocketClient

        client = SlackSocketClient.__new__(SlackSocketClient)

        with patch.object(client, "_run_ai_ask") as mock_run, \
             patch.object(client, "_post_message") as mock_post:
            client._process_message_event({"text": "用价值投资分析五粮液", "channel": "D1", "user": "U1"})

        mock_post.assert_called_once()
        mock_run.assert_called_once_with(text="用价值投资分析五粮液", channel_id="D1", user_id="U1")

    def test_slack_socket_returns_supported_modes_for_modes_query(self):
        from bot.platforms.slack_socket import SlackSocketClient

        client = SlackSocketClient.__new__(SlackSocketClient)
        fake_config = SimpleNamespace()
        fake_skill_manager = MagicMock()
        fake_skill_manager.list_skills.return_value = [
            SimpleNamespace(name="bull_trend", display_name="多头趋势", description="顺势交易", default_priority=20, user_invocable=True),
            SimpleNamespace(name="value_investing", display_name="价值投资", description="长期估值分析", default_priority=10, user_invocable=True),
        ]

        with patch("src.config.get_config", return_value=fake_config), \
             patch("src.agent.factory.get_skill_manager", return_value=fake_skill_manager), \
             patch("src.agent.skills.defaults.get_primary_default_skill_id", return_value="bull_trend"), \
             patch.object(client, "_post_message") as mock_post:
            client._run_ai_ask(text="当前支持哪些分析模式", channel_id="D1", user_id="U1")

        mock_post.assert_called_once()
        posted_text = mock_post.call_args.args[1]
        self.assertIn("当前支持的股票分析模式", posted_text)
        self.assertIn("价值投资", posted_text)
        self.assertIn("多头趋势", posted_text)


if __name__ == "__main__":
    unittest.main()
