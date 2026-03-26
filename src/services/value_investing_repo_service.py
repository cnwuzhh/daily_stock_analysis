# -*- coding: utf-8 -*-
"""Helpers for external value-investing prompt and report repo integration."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.data.stock_mapping import STOCK_NAME_MAP
from src.services.name_to_code_resolver import resolve_name_to_code

logger = logging.getLogger(__name__)

_VALUE_INVESTING_PROMPT_RELATIVE_PATH = Path("我的投资体系报告库") / "价值投资体系.txt"
_VALUE_INVESTING_REPORTS_RELATIVE_DIR = Path("我的投资体系报告库") / "01_个股深度分析"
_CANONICAL_ACTIONS = ("建仓", "加仓", "持有", "观望", "减仓", "卖出")


def _build_unique_local_name_to_code() -> dict[str, str]:
    name_counts: dict[str, int] = {}
    for name in STOCK_NAME_MAP.values():
        if name:
            name_counts[name] = name_counts.get(name, 0) + 1
    return {
        name: code
        for code, name in STOCK_NAME_MAP.items()
        if name and name_counts.get(name) == 1
    }


_UNIQUE_LOCAL_NAME_TO_CODE = _build_unique_local_name_to_code()


@dataclass
class ValueInvestingArchiveResult:
    saved_path: str = ""
    committed: bool = False
    pushed: bool = False
    skipped_reason: str = ""


def _as_path(value: Any) -> Optional[Path]:
    text = str(value or "").strip()
    return Path(text) if text else None


def _resolve_repo_path(config: Any) -> Optional[Path]:
    return _as_path(getattr(config, "value_investing_repo_path", ""))


def resolve_value_investing_prompt_path(config: Any) -> Optional[Path]:
    explicit = _as_path(getattr(config, "value_investing_prompt_file", ""))
    if explicit:
        return explicit
    repo_path = _resolve_repo_path(config)
    if not repo_path:
        return None
    return repo_path / _VALUE_INVESTING_PROMPT_RELATIVE_PATH


def load_value_investing_prompt_override(config: Any) -> str:
    prompt_path = resolve_value_investing_prompt_path(config)
    if not prompt_path or not prompt_path.is_file():
        return ""
    try:
        return prompt_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("Failed to read value investing prompt from %s: %s", prompt_path, exc)
        return ""


def _normalize_report_code(stock_code: str) -> str:
    code = str(stock_code or "").strip()
    if not code:
        return "unknown"
    lower_code = code.lower()
    if lower_code.startswith("hk"):
        return code[2:]
    return code.upper()


def _sanitize_filename_part(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(value or "").strip())
    cleaned = cleaned.strip(" ._")
    return cleaned or fallback


def _normalize_lookup_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).strip()


def _extract_stock_code_from_text(text: str) -> str:
    raw_text = str(text or "")
    direct_match = re.search(r"(?<!\d)((?:[03648]\d{5}|92\d{4}))(?!\d)", raw_text)
    if direct_match:
        return direct_match.group(1)
    hk_match = re.search(r"(?<![a-zA-Z])(hk\d{5})(?!\d)", raw_text, re.IGNORECASE)
    if hk_match:
        return hk_match.group(1).upper()

    normalized_text = _normalize_lookup_text(raw_text)
    for name, code in sorted(_UNIQUE_LOCAL_NAME_TO_CODE.items(), key=lambda item: len(item[0]), reverse=True):
        if _normalize_lookup_text(name) and _normalize_lookup_text(name) in normalized_text:
            return code
    return ""


def _extract_stock_code(message: str, context: Optional[dict[str, Any]], analysis_markdown: str = "") -> str:
    if isinstance(context, dict):
        code = str(context.get("stock_code") or "").strip()
        if code:
            return code
        stock_info = context.get("stock_info")
        if isinstance(stock_info, dict):
            info_code = str(stock_info.get("code") or "").strip()
            if info_code:
                return info_code

    for candidate in (message, analysis_markdown):
        code = _extract_stock_code_from_text(candidate)
        if code:
            return code

    text = str(message or "")
    return resolve_name_to_code(text) or ""


def _extract_stock_name(stock_code: str, message: str, context: Optional[dict[str, Any]]) -> str:
    if isinstance(context, dict):
        stock_name = str(context.get("stock_name") or "").strip()
        if stock_name:
            return stock_name
        stock_info = context.get("stock_info")
        if isinstance(stock_info, dict):
            info_name = str(stock_info.get("name") or "").strip()
            if info_name:
                return info_name

    normalized_code = _normalize_report_code(stock_code)
    fallback_name = _sanitize_filename_part(message[:20], normalized_code)
    if isinstance(context, dict):
        match = re.search(r"分析([\u3400-\u9fffA-Za-z0-9]+)", str(message or ""))
        if match:
            fallback_name = _sanitize_filename_part(match.group(1), normalized_code)
    return STOCK_NAME_MAP.get(normalized_code, fallback_name)


def _extract_conclusion(analysis_markdown: str) -> str:
    text = str(analysis_markdown or "")
    patterns = (
        r"当前结论[：:]\s*(建仓|加仓|持有|观望|减仓|卖出)",
        r"价值底仓建议[\s\S]{0,80}?操作[：:]\s*(建仓|加仓|持有|观望|减仓|卖出|买入)",
        r"操作建议[：:]\s*(建仓|加仓|持有|观望|减仓|卖出|买入)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            action = match.group(1)
            return "建仓" if action == "买入" else action

    keyword_map = {
        "减仓": "减仓",
        "卖出": "卖出",
        "加仓": "加仓",
        "持有": "持有",
        "建仓": "建仓",
        "买入": "建仓",
        "可分批布局": "建仓",
        "观望": "观望",
        "等待更好价格": "观望",
    }
    for keyword, action in keyword_map.items():
        if keyword in text:
            return action
    return "观望"


def _compose_report_content(
    *,
    stock_name: str,
    stock_code: str,
    conclusion: str,
    analysis_markdown: str,
    now: datetime,
) -> str:
    body = str(analysis_markdown or "").strip()
    lines = [
        f"# {stock_name}（{stock_code}）投资体系分析报告",
        "",
        f"- 分析日期：{now.strftime('%Y.%m.%d')}",
        f"- 当前结论：{conclusion}",
        "- 数据来源：daily_stock_analysis 价值投资分析 + 外部投资体系提示词",
        "",
    ]
    if body.startswith("#"):
        lines.extend(["## AI 分析原文", "", body])
    else:
        lines.append(body)
    return "\n".join(lines).rstrip() + "\n"


def _run_git(repo_path: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repo_path), *args]
    return subprocess.run(command, capture_output=True, text=True, check=check)


def _git_env_with_author(config: Any) -> Optional[dict[str, str]]:
    author_name = str(getattr(config, "value_investing_git_author_name", "") or "").strip()
    author_email = str(getattr(config, "value_investing_git_author_email", "") or "").strip()
    if not author_name or not author_email:
        return None
    env = dict(os.environ)
    env["GIT_AUTHOR_NAME"] = author_name
    env["GIT_AUTHOR_EMAIL"] = author_email
    env["GIT_COMMITTER_NAME"] = author_name
    env["GIT_COMMITTER_EMAIL"] = author_email
    return env


def _ensure_git_safe_directory(repo_path: Path) -> None:
    _run_git(repo_path, "config", "--global", "--add", "safe.directory", str(repo_path), check=False)


def _run_git_with_env(
    repo_path: Path,
    *args: str,
    check: bool = True,
    env: Optional[dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repo_path), *args]
    return subprocess.run(command, capture_output=True, text=True, check=check, env=env)


def archive_value_investing_report(
    *,
    message: str,
    analysis_markdown: str,
    context: Optional[dict[str, Any]],
    config: Any,
    now: Optional[datetime] = None,
) -> ValueInvestingArchiveResult:
    repo_path = _resolve_repo_path(config)
    if not repo_path:
        return ValueInvestingArchiveResult(skipped_reason="repo_path_not_configured")
    if not repo_path.is_dir():
        return ValueInvestingArchiveResult(skipped_reason="repo_path_missing")
    if not (repo_path / ".git").exists():
        return ValueInvestingArchiveResult(skipped_reason="repo_not_git")

    stock_code = _extract_stock_code(message, context, analysis_markdown)
    if not stock_code:
        logger.info("Skip archiving value investing report: stock code unresolved for message=%r", message)
        return ValueInvestingArchiveResult(skipped_reason="stock_code_unresolved")

    stock_name = _extract_stock_name(stock_code, message, context)
    conclusion = _extract_conclusion(analysis_markdown)
    now = now or datetime.now()
    year_dir = repo_path / _VALUE_INVESTING_REPORTS_RELATIVE_DIR / f"{now.year}年"
    year_dir.mkdir(parents=True, exist_ok=True)

    report_code = _sanitize_filename_part(_normalize_report_code(stock_code), "unknown")
    report_name = _sanitize_filename_part(stock_name, report_code)
    report_conclusion = conclusion if conclusion in _CANONICAL_ACTIONS else "观望"
    report_filename = f"{now.strftime('%Y.%m.%d')}_{report_name}_{report_code}_{report_conclusion}.md"
    report_path = year_dir / report_filename
    report_content = _compose_report_content(
        stock_name=stock_name,
        stock_code=report_code,
        conclusion=report_conclusion,
        analysis_markdown=analysis_markdown,
        now=now,
    )
    report_path.write_text(report_content, encoding="utf-8")

    result = ValueInvestingArchiveResult(saved_path=str(report_path))
    if not getattr(config, "value_investing_report_auto_push", False):
        return result

    try:
        _ensure_git_safe_directory(repo_path)
        git_env = _git_env_with_author(config)
        _run_git_with_env(repo_path, "add", str(report_path), env=git_env)
        staged = _run_git_with_env(
            repo_path,
            "diff",
            "--cached",
            "--quiet",
            "--",
            str(report_path),
            check=False,
            env=git_env,
        )
        if staged.returncode == 0:
            return result

        commit_message = f"add value investing report for {report_code} on {now.strftime('%Y.%m.%d')}"
        _run_git_with_env(repo_path, "commit", "-m", commit_message, env=git_env)
        result.committed = True
        _run_git_with_env(repo_path, "push", "origin", "HEAD", env=git_env)
        result.pushed = True
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Value investing report git sync failed in %s: %s",
            repo_path,
            (exc.stderr or exc.stdout or str(exc)).strip(),
        )
    return result
