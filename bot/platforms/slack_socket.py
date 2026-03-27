# -*- coding: utf-8 -*-
"""Slack Socket Mode client for AI ask-stock."""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from slack_sdk import WebClient
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler

    SLACK_SDK_AVAILABLE = True
except ImportError:
    WebClient = None
    App = None
    SocketModeHandler = None
    SLACK_SDK_AVAILABLE = False
    logger.warning("[Slack Socket] slack_sdk/slack_bolt 未安装，Socket Mode 不可用")
    logger.warning("[Slack Socket] 请运行: pip install slack_sdk slack_bolt")

from src.data.stock_mapping import STOCK_NAME_MAP


def _build_code_to_name() -> dict[str, str]:
    return {str(code).upper(): str(name) for code, name in STOCK_NAME_MAP.items() if code and name}


_CODE_TO_NAME = _build_code_to_name()


def _normalize_plain_text(text: str) -> str:
    return re.sub(r"<@[^>]+>", " ", str(text or "")).strip()


def _should_handle_message(event: dict, bot_user_id: str) -> bool:
    if event.get("subtype"):
        return False
    if event.get("bot_id"):
        return False
    text = str(event.get("text") or "")
    channel_type = str(event.get("channel_type") or "")
    if channel_type == "im":
        return bool(_normalize_plain_text(text))
    mention_token = f"<@{bot_user_id}>" if bot_user_id else ""
    return bool(mention_token and mention_token in text and _normalize_plain_text(text))


def _format_answer_text(result_text: str, stock_code: str = "", stock_name: str = "", skill_name: str = "") -> str:
    title = stock_name or _CODE_TO_NAME.get(str(stock_code or "").upper(), "")
    header_bits = [bit for bit in [stock_code, title, skill_name] if bit]
    header = " | ".join(header_bits)
    if header:
        return f"{header}\n{'-' * 24}\n{result_text}"
    return result_text


def _build_processing_notice(text: str) -> str:
    preview = str(text or "").strip()
    if len(preview) > 60:
        preview = preview[:57] + "..."
    return f"已收到，正在分析：{preview}"


def _build_modes_text(config) -> str:
    from src.agent.factory import get_skill_manager
    from src.agent.skills.defaults import get_primary_default_skill_id

    skill_manager = get_skill_manager(config)
    available_skills = sorted(
        [skill for skill in skill_manager.list_skills() if getattr(skill, "user_invocable", True)],
        key=lambda skill: (
            int(getattr(skill, "default_priority", 100)),
            str(getattr(skill, "display_name", "") or ""),
            str(getattr(skill, "name", "") or ""),
        ),
    )
    default_skill_id = get_primary_default_skill_id(available_skills)
    lines = ["当前支持的股票分析模式："]
    for idx, skill in enumerate(available_skills, start=1):
        skill_id = str(getattr(skill, "name", "") or "").strip()
        skill_name = str(getattr(skill, "display_name", skill_id) or skill_id).strip()
        desc = str(getattr(skill, "description", "") or "").strip()
        suffix = "（默认）" if skill_id == default_skill_id else ""
        line = f"{idx}. {skill_name} [{skill_id}]{suffix}"
        if desc:
            line += f" - {desc}"
        lines.append(line)
    lines.append("\n使用示例：用价值投资分析五粮液")
    return "\n".join(lines)


def _is_modes_query(text: str) -> bool:
    normalized = str(text or "").strip().lower()
    candidates = (
        "模式",
        "有哪些模式",
        "支持的模式",
        "分析模式",
        "分析策略",
        "支持哪些分析",
        "mode",
        "modes",
        "skills",
        "strategies",
        "帮助",
        "help",
    )
    return any(token in normalized for token in candidates)


class SlackSocketClient:
    def __init__(self, app_token: Optional[str] = None, bot_token: Optional[str] = None):
        if not SLACK_SDK_AVAILABLE:
            raise ImportError("slack_sdk 未安装")

        from src.config import get_config

        config = get_config()
        self._app_token = app_token or getattr(config, "slack_app_token", None)
        self._bot_token = bot_token or getattr(config, "slack_bot_token", None)
        if not self._app_token or not self._bot_token:
            raise ValueError("Slack Socket Mode 需要配置 SLACK_APP_TOKEN 和 SLACK_BOT_TOKEN")

        self._web_client = WebClient(token=self._bot_token)
        self._app = App(token=self._bot_token)
        self._handler = SocketModeHandler(self._app, self._app_token)
        auth = self._web_client.auth_test()
        self._bot_user_id = str(auth.data.get("user_id") or "")
        self._background_thread: Optional[threading.Thread] = None
        self._running = False
        self._register_handlers()

    def start(self) -> None:
        logger.info("[Slack Socket] 正在启动 Socket Mode 客户端...")
        self._running = True
        logger.info("[Slack Socket] 客户端已连接，等待消息...")
        self._handler.start()

    def start_background(self) -> None:
        if self._background_thread and self._background_thread.is_alive():
            logger.warning("[Slack Socket] 客户端已在运行")
            return
        self._running = True
        self._background_thread = threading.Thread(target=self._run_in_background, daemon=True, name="SlackSocketClient")
        self._background_thread.start()
        logger.info("[Slack Socket] 后台客户端已启动")

    def _run_in_background(self) -> None:
        while self._running:
            try:
                self.start()
            except Exception as exc:
                logger.error("[Slack Socket] 运行异常: %s", exc)
                if self._running:
                    logger.info("[Slack Socket] 5 秒后重连...")
                    time.sleep(5)

    def stop(self) -> None:
        self._running = False
        try:
            self._handler.close()
        except Exception:
            pass
        logger.info("[Slack Socket] 客户端已停止")

    def _register_handlers(self) -> None:
        @self._app.middleware  # type: ignore[misc]
        def _log_all_requests(logger, body, next):
            try:
                logger.info("[Slack Socket] Incoming Bolt body keys=%s type=%s command=%s", list((body or {}).keys()), (body or {}).get('type'), (body or {}).get('command'))
            except Exception:
                pass
            return next()

        @self._app.command("/ask")
        def _handle_ask(ack, body, command=None, logger=None):
            if logger:
                logger.info("[Slack Socket] Matched /ask command body=%s", body)
            ack("已收到 AI 问股请求，正在分析...")
            payload = command or body or {}
            threading.Thread(target=self._process_slash_command, args=(payload,), daemon=True).start()

        @self._app.event("message")
        def _handle_message_events(body, event, logger=None, say=None):
            if logger:
                logger.info("[Slack Socket] Received message event channel=%s channel_type=%s text=%r", event.get("channel"), event.get("channel_type"), event.get("text"))
            if not _should_handle_message(event, self._bot_user_id):
                return
            threading.Thread(target=self._process_message_event, args=(event,), daemon=True).start()

    def _process_slash_command(self, payload: dict) -> None:
        text = str(payload.get("text") or "").strip()
        channel_id = str(payload.get("channel_id") or "")
        user_id = str(payload.get("user_id") or "")
        logger.info("[Slack Socket] Processing /ask command channel=%s user=%s text=%r", channel_id, user_id, text)
        if not text or not channel_id:
            self._post_message(channel_id, "用法: /ask <问题>")
            return
        self._run_ai_ask(text=text, channel_id=channel_id, user_id=user_id)

    def _process_message_event(self, event: dict) -> None:
        text = _normalize_plain_text(str(event.get("text") or ""))
        channel_id = str(event.get("channel") or "")
        user_id = str(event.get("user") or "")
        logger.info("[Slack Socket] Processing message event channel=%s user=%s text=%r", channel_id, user_id, text)
        if not text or not channel_id:
            return
        self._post_message(channel_id, _build_processing_notice(text))
        self._run_ai_ask(text=text, channel_id=channel_id, user_id=user_id)

    def _run_ai_ask(self, *, text: str, channel_id: str, user_id: str) -> None:
        try:
            from src.agent.factory import build_agent_executor, get_skill_manager
            from api.v1.endpoints.agent import _prefetch_value_investing_context

            from src.config import get_config
            config = get_config()
            if _is_modes_query(text):
                self._post_message(channel_id, _build_modes_text(config))
                return
            skill_id, skill_name = self._detect_skill(text, config)
            skills = [skill_id] if skill_id else None
            ctx = {"skills": skills} if skills else {}
            if skill_id == "value_investing":
                ctx = _prefetch_value_investing_context(ctx, text)

            session_id = f"slack_{channel_id}_{user_id}"
            executor = build_agent_executor(config, skills=skills)
            result = executor.chat(message=text, session_id=session_id, context=ctx or None)
            if result.success:
                stock_code = str((ctx or {}).get("stock_code") or "")
                stock_name = str((ctx or {}).get("stock_name") or "")
                self._post_message(channel_id, _format_answer_text(result.content, stock_code, stock_name, skill_name))
            else:
                self._post_message(channel_id, f"⚠️ 分析失败: {result.error}")
        except Exception as exc:
            logger.exception("[Slack Socket] AI ask 失败")
            self._post_message(channel_id, f"⚠️ Slack AI 问股执行出错: {exc}")

    def _detect_skill(self, text: str, config) -> tuple[str, str]:
        try:
            skill_manager = get_skill_manager(config)
            alias_pairs: list[tuple[str, str, str]] = []
            for skill in skill_manager.list_skills():
                skill_id = str(getattr(skill, "name", "") or "").strip()
                skill_name = str(getattr(skill, "display_name", skill_id) or skill_id).strip()
                aliases = [skill_id, skill_name] + list(getattr(skill, "aliases", []) or [])
                for alias in aliases:
                    alias_text = str(alias or "").strip()
                    if alias_text:
                        alias_pairs.append((alias_text, skill_id, skill_name))
            alias_pairs.sort(key=lambda item: (len(item[0]), item[0]), reverse=True)
            for alias_text, skill_id, skill_name in alias_pairs:
                if alias_text in text:
                    return skill_id, skill_name
        except Exception as exc:
            logger.debug("[Slack Socket] 技能识别失败: %s", exc)
        return "", ""

    def _post_message(self, channel_id: str, text: str) -> None:
        if not channel_id:
            logger.warning("[Slack Socket] 缺少 channel_id，无法回消息")
            return
        try:
            self._web_client.chat_postMessage(channel=channel_id, text=text)
        except Exception as exc:
            logger.error("[Slack Socket] chat_postMessage 失败: %s", exc)


_slack_socket_client: Optional[SlackSocketClient] = None


def get_slack_socket_client() -> Optional[SlackSocketClient]:
    global _slack_socket_client
    if _slack_socket_client is None and SLACK_SDK_AVAILABLE:
        try:
            _slack_socket_client = SlackSocketClient()
        except (ImportError, ValueError) as exc:
            logger.warning("[Slack Socket] 无法创建客户端: %s", exc)
            return None
    return _slack_socket_client


def start_slack_socket_background() -> bool:
    client = get_slack_socket_client()
    if client:
        client.start_background()
        return True
    return False
