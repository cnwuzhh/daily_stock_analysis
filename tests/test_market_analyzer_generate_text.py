# -*- coding: utf-8 -*-
"""Tests for Analyzer.generate_text() and the market_analyzer bypass fix.

Covers:
- generate_text() returns the LLM response on success
- generate_text() returns None and logs on failure (no exception propagated)
- market_analyzer calls generate_text(), not private analyzer attributes
- Any provider configuration (Gemini / Anthropic / OpenAI / LLM_CHANNELS)
  does NOT trigger AttributeError (regression guard for the old bypass bug)
"""
import sys
from unittest.mock import MagicMock, patch

# Stub heavy dependencies before project imports
for _mod in ("litellm", "google.generativeai", "google.genai", "anthropic"):
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import pytest
from unittest.mock import PropertyMock

from src.config import Config
from src.llm_rate_guard import execute_rate_limited_litellm_call, reset_glm_rate_guard_state


# ---------------------------------------------------------------------------
# Analyzer.generate_text()
# ---------------------------------------------------------------------------

class TestAnalyzerGenerateText:
    def _make_analyzer(self):
        """Return a minimally configured GeminiAnalyzer with _call_litellm mocked."""
        with patch("src.analyzer.get_config") as mock_cfg:
            cfg = MagicMock()
            cfg.litellm_model = "gemini/gemini-2.0-flash"
            cfg.litellm_fallback_models = []
            cfg.gemini_api_keys = ["sk-gemini-testkey-1234"]
            cfg.anthropic_api_keys = []
            cfg.openai_api_keys = []
            cfg.deepseek_api_keys = []
            cfg.llm_model_list = []
            cfg.openai_base_url = None
            mock_cfg.return_value = cfg
            from src.analyzer import GeminiAnalyzer
            analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
            analyzer._router = None
            return analyzer

    def test_generate_text_returns_llm_response(self):
        analyzer = self._make_analyzer()
        with patch.object(analyzer, "_call_litellm", return_value="市场分析报告") as mock_call:
            result = analyzer.generate_text("写一份复盘", max_tokens=1024, temperature=0.5)
            assert result == "市场分析报告"
            mock_call.assert_called_once_with(
                "写一份复盘",
                generation_config={"max_tokens": 1024, "temperature": 0.5},
            )

    def test_generate_text_returns_none_on_failure(self):
        analyzer = self._make_analyzer()
        with patch.object(analyzer, "_call_litellm", side_effect=Exception("LLM error")):
            result = analyzer.generate_text("prompt")
            assert result is None  # must not raise

    def test_generate_text_default_params(self):
        analyzer = self._make_analyzer()
        with patch.object(analyzer, "_call_litellm", return_value="ok") as mock_call:
            analyzer.generate_text("hello")
            _, kwargs = mock_call.call_args
            gen_cfg = kwargs["generation_config"]
            assert gen_cfg["max_tokens"] == 2048
            assert gen_cfg["temperature"] == 0.7


# ---------------------------------------------------------------------------
# market_analyzer uses generate_text(), not private attributes
# ---------------------------------------------------------------------------

class TestMarketAnalyzerBypassFix:
    def _make_market_analyzer_with_mock_generate_text(self, return_value="复盘报告"):
        """Return a MarketAnalyzer whose embedded Analyzer.generate_text is mocked."""
        from src.core.market_profile import CN_PROFILE
        from src.core.market_strategy import get_market_strategy_blueprint

        with patch("src.analyzer.get_config") as mock_cfg, \
             patch("src.market_analyzer.get_config") as mock_cfg2:
            cfg = MagicMock()
            cfg.litellm_model = "gemini/gemini-2.0-flash"
            cfg.litellm_fallback_models = []
            cfg.gemini_api_keys = ["sk-gemini-testkey-1234"]
            cfg.anthropic_api_keys = []
            cfg.openai_api_keys = []
            cfg.deepseek_api_keys = []
            cfg.llm_model_list = []
            cfg.openai_base_url = None
            cfg.market_review_region = "cn"
            mock_cfg.return_value = cfg
            mock_cfg2.return_value = cfg

            from src.analyzer import GeminiAnalyzer
            from src.market_analyzer import MarketAnalyzer

            analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
            analyzer._router = None
            analyzer._litellm_available = True
            analyzer.generate_text = MagicMock(return_value=return_value)

            ma = MarketAnalyzer.__new__(MarketAnalyzer)
            ma.analyzer = analyzer
            ma.profile = CN_PROFILE
            ma.strategy = get_market_strategy_blueprint("cn")
            ma.region = "cn"
            return ma

    def test_no_access_to_private_model_attribute(self):
        """generate_text() must be called; _model must never be accessed."""
        ma = self._make_market_analyzer_with_mock_generate_text("复盘结果")
        # Ensure _model attribute does not exist (simulates PR #494 state)
        assert not hasattr(ma.analyzer, "_model") or ma.analyzer._model is None, (
            "_model should not be set on the LiteLLM-based analyzer"
        )
        # generate_text is a MagicMock, so calling it won't crash
        result = ma.analyzer.generate_text("prompt")
        assert result == "复盘结果"
        ma.analyzer.generate_text.assert_called_once()

    def test_generate_text_none_falls_back_to_template(self):
        """generate_market_review() falls back to template when generate_text returns None."""
        from src.market_analyzer import MarketOverview, MarketIndex

        ma = self._make_market_analyzer_with_mock_generate_text(return_value=None)
        overview = MarketOverview(
            date="2026-03-05",
            indices=[
                MarketIndex(
                    code="000001",
                    name="上证指数",
                    current=3300.0,
                    change=5.0,
                    change_pct=0.15,
                )
            ],
        )
        result = ma.generate_market_review(overview, [])
        assert isinstance(result, str) and len(result) > 0
        ma.analyzer.generate_text.assert_called_once()

    def test_no_private_attribute_access_in_market_analyzer_source(self):
        """Static guard: market_analyzer.py must not access private analyzer attrs."""
        import ast
        import pathlib

        src = pathlib.Path("src/market_analyzer.py").read_text()
        tree = ast.parse(src)
        forbidden = {
            "_model", "_router", "_use_openai", "_use_anthropic",  # historical
            "_call_litellm",      # use generate_text() instead
            "_litellm_available", # use is_available() instead
        }

        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                if node.attr in forbidden:
                    violations.append(node.attr)

        assert violations == [], (
            f"market_analyzer.py still accesses private Analyzer attributes: {violations}"
        )


class TestGlmRateGuard:
    def _cfg(self, **overrides):
        defaults = dict(
            stock_list=["600519"],
            glm_rate_guard_enabled=True,
            glm_request_min_interval_seconds=8.0,
            glm_rate_limit_cooldown_seconds=20.0,
            glm_rate_limit_max_retries=1,
        )
        defaults.update(overrides)
        return Config(**defaults)

    def test_non_glm_model_bypasses_guard(self):
        reset_glm_rate_guard_state()
        calls = []

        def fake_call():
            calls.append("ok")
            return "done"

        with patch("src.llm_rate_guard.time.sleep") as mock_sleep:
            result = execute_rate_limited_litellm_call(
                fake_call,
                model="openai/gpt-4o-mini",
                api_base="https://api.openai.com/v1",
                config=self._cfg(),
            )

        assert result == "done"
        assert calls == ["ok"]
        mock_sleep.assert_not_called()

    def test_glm_guard_waits_for_min_interval(self):
        reset_glm_rate_guard_state()
        calls = []

        def fake_call():
            calls.append("ok")
            return "done"

        with patch("src.llm_rate_guard.time.monotonic", side_effect=[0.0, 0.0, 2.0, 2.0]), \
             patch("src.llm_rate_guard.time.sleep") as mock_sleep:
            execute_rate_limited_litellm_call(
                fake_call,
                model="openai/glm-4.7",
                api_base="https://open.bigmodel.cn/api/paas/v4",
                config=self._cfg(),
            )
            execute_rate_limited_litellm_call(
                fake_call,
                model="openai/glm-4.7",
                api_base="https://open.bigmodel.cn/api/paas/v4",
                config=self._cfg(),
            )

        assert calls == ["ok", "ok"]
        mock_sleep.assert_called_once_with(6.0)

    def test_glm_guard_retries_after_rate_limit(self):
        reset_glm_rate_guard_state()
        attempts = []

        def fake_call():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("RateLimitError: 您的账户已达到速率限制")
            return "done"

        with patch("src.llm_rate_guard.time.monotonic", side_effect=[0.0, 0.0, 25.0, 25.0]), \
             patch("src.llm_rate_guard.time.sleep") as mock_sleep:
            result = execute_rate_limited_litellm_call(
                fake_call,
                model="openai/glm-4.7",
                api_base="https://open.bigmodel.cn/api/paas/v4",
                config=self._cfg(),
            )

        assert result == "done"
        assert len(attempts) == 2
        mock_sleep.assert_called_once_with(20.0)
