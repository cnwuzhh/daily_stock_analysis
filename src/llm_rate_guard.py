import logging
import threading
import time
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)

_GLM_REQUEST_LOCK = threading.Lock()
_GLM_LAST_REQUEST_STARTED_AT: Optional[float] = None


def reset_glm_rate_guard_state() -> None:
    """Reset in-memory GLM throttling state for tests."""
    global _GLM_LAST_REQUEST_STARTED_AT
    with _GLM_REQUEST_LOCK:
        _GLM_LAST_REQUEST_STARTED_AT = None


def _is_glm_target(model: str, api_base: Optional[str]) -> bool:
    model_text = str(model or "").strip().lower()
    model_short = model_text.split("/")[-1] if "/" in model_text else model_text
    base_text = str(api_base or "").strip().lower()
    return model_short.startswith("glm-") or "open.bigmodel.cn" in base_text


def _looks_like_rate_limit_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        token in text
        for token in (
            "ratelimit",
            "rate limit",
            "too many requests",
            "达到速率限制",
            "速率限制",
            "请求频率",
            "频率限制",
        )
    )


def _get_bool(config: Any, field_name: str, default: bool) -> bool:
    value = getattr(config, field_name, default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    return text not in {"0", "false", "no", "off"}


def _get_float(config: Any, field_name: str, default: float) -> float:
    value = getattr(config, field_name, default)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _get_int(config: Any, field_name: str, default: int) -> int:
    value = getattr(config, field_name, default)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def execute_rate_limited_litellm_call(
    call: Callable[[], Any],
    *,
    model: str,
    api_base: Optional[str],
    config: Any,
) -> Any:
    """Serialize and pace GLM requests to reduce provider-side rate limits."""
    if not _is_glm_target(model, api_base):
        return call()

    if not _get_bool(config, "glm_rate_guard_enabled", True):
        return call()

    min_interval = _get_float(config, "glm_request_min_interval_seconds", 8.0)
    cooldown = _get_float(config, "glm_rate_limit_cooldown_seconds", 20.0)
    max_retries = _get_int(config, "glm_rate_limit_max_retries", 1)

    global _GLM_LAST_REQUEST_STARTED_AT
    with _GLM_REQUEST_LOCK:
        for attempt in range(max_retries + 1):
            now = time.monotonic()
            wait_seconds = 0.0
            if _GLM_LAST_REQUEST_STARTED_AT is not None:
                wait_seconds = min_interval - (now - _GLM_LAST_REQUEST_STARTED_AT)
            if wait_seconds > 0:
                logger.info(
                    "GLM request guard sleeping %.2fs before %s",
                    wait_seconds,
                    model,
                )
                time.sleep(wait_seconds)

            _GLM_LAST_REQUEST_STARTED_AT = time.monotonic()

            try:
                return call()
            except Exception as exc:
                if attempt >= max_retries or not _looks_like_rate_limit_error(exc):
                    raise
                logger.warning(
                    "GLM rate limit detected for %s; cooling down %.2fs before retry %s/%s",
                    model,
                    cooldown,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(max(cooldown, min_interval))

        return call()
