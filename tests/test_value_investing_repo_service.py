# -*- coding: utf-8 -*-

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.services.value_investing_repo_service import (
    archive_value_investing_report,
    load_value_investing_prompt_override,
)


class TestValueInvestingRepoService:
    def test_load_value_investing_prompt_override_from_repo_default_path(self, tmp_path: Path):
        prompt_path = tmp_path / "我的投资体系报告库" / "价值投资体系.txt"
        prompt_path.parent.mkdir(parents=True)
        prompt_path.write_text("外部价值投资提示词", encoding="utf-8")

        cfg = SimpleNamespace(
            value_investing_repo_path=str(tmp_path),
            value_investing_prompt_file="",
        )

        assert load_value_investing_prompt_override(cfg) == "外部价值投资提示词"

    def test_archive_value_investing_report_writes_expected_markdown_file(self, tmp_path: Path):
        cfg = SimpleNamespace(
            value_investing_repo_path=str(tmp_path),
            value_investing_report_auto_push=False,
        )
        (tmp_path / ".git").mkdir()

        result = archive_value_investing_report(
            message="用价值投资分析贵州茅台",
            analysis_markdown="## 4. 仓位与操作建议\n\n价值底仓建议：\n\n- 操作：持有\n",
            context={"skills": ["value_investing"], "stock_code": "600519", "stock_name": "贵州茅台"},
            config=cfg,
        )

        report_path = Path(result.saved_path)
        assert report_path.exists()
        assert report_path.name.endswith("_贵州茅台_600519_持有.md")
        content = report_path.read_text(encoding="utf-8")
        assert "# 贵州茅台（600519）投资体系分析报告" in content
        assert "- 当前结论：持有" in content

    def test_archive_value_investing_report_resolves_name_from_message_without_prefetch(self, tmp_path: Path):
        cfg = SimpleNamespace(
            value_investing_repo_path=str(tmp_path),
            value_investing_report_auto_push=False,
        )
        (tmp_path / ".git").mkdir()

        result = archive_value_investing_report(
            message="用价值投资分析五粮液",
            analysis_markdown="## 四、仓位建议\n\n操作建议：建仓\n",
            context={"skills": ["value_investing"]},
            config=cfg,
        )

        report_path = Path(result.saved_path)
        assert report_path.exists()
        assert report_path.name.endswith("_五粮液_000858_建仓.md")

    def test_archive_value_investing_report_uses_safe_directory_and_git_author_env(self, tmp_path: Path):
        cfg = SimpleNamespace(
            value_investing_repo_path=str(tmp_path),
            value_investing_report_auto_push=True,
            value_investing_git_author_name="bot",
            value_investing_git_author_email="bot@example.com",
        )
        (tmp_path / ".git").mkdir()

        calls = []

        def fake_git(repo_path, *args, check=True, env=None):
            calls.append((args, env, check))

            class Result:
                returncode = 1 if args[:3] == ("diff", "--cached", "--quiet") else 0

            return Result()

        with patch("src.services.value_investing_repo_service._run_git_with_env", side_effect=fake_git), patch(
            "src.services.value_investing_repo_service._run_git",
        ) as mock_plain_git:
            archive_value_investing_report(
                message="用价值投资分析贵州茅台",
                analysis_markdown="操作建议：持有",
                context={"stock_code": "600519", "stock_name": "贵州茅台"},
                config=cfg,
            )

        mock_plain_git.assert_called_once()
        assert mock_plain_git.call_args.args[1:] == ("config", "--global", "--add", "safe.directory", str(tmp_path))
        commit_envs = [env for args, env, _check in calls if args and args[0] == "commit"]
        assert commit_envs
        assert commit_envs[0]["GIT_AUTHOR_NAME"] == "bot"
        assert commit_envs[0]["GIT_AUTHOR_EMAIL"] == "bot@example.com"
