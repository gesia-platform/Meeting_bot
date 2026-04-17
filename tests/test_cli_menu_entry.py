from __future__ import annotations

import sys
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zoom_meeting_bot_cli import main as cli_main
from zoom_meeting_bot_cli.skill_manager import SkillAsset


class CliMenuEntryTests(unittest.TestCase):
    def test_main_without_args_opens_menu(self) -> None:
        with patch.object(cli_main, "handle_menu", return_value=0) as mocked_menu:
            result = cli_main.main([])
        self.assertEqual(result, 0)
        mocked_menu.assert_called_once()
        args = mocked_menu.call_args.args[0]
        self.assertEqual(args.config, cli_main.default_config_path())

    def test_menu_command_routes_to_menu_handler(self) -> None:
        with patch.object(cli_main, "handle_menu", return_value=0) as mocked_menu:
            result = cli_main.main(["menu"])
        self.assertEqual(result, 0)
        mocked_menu.assert_called_once()

    def test_build_menu_create_session_args_minimal_flow(self) -> None:
        with (
            patch.object(
                cli_main,
                "_prompt_menu_text",
                side_effect=[
                    "https://zoom.us/j/123456789",
                    "pass-1234",
                ],
            ),
        ):
            args = cli_main._build_menu_create_session_args(cli_main.default_config_path())

        self.assertIsNotNone(args)
        assert args is not None
        self.assertEqual(args.join_url, "https://zoom.us/j/123456789")
        self.assertEqual(args.passcode, "pass-1234")
        self.assertEqual(args.meeting_topic, "")
        self.assertEqual(args.requested_by, "")
        self.assertEqual(args.instructions, "")
        self.assertTrue(args.open)

    def test_handle_menu_create_session_returns_zero_when_cancelled(self) -> None:
        with (
            patch.object(cli_main, "_menu_require_config", return_value=True),
            patch.object(cli_main, "_build_menu_create_session_args", return_value=None),
            patch.object(cli_main, "handle_create_session") as mocked_create,
        ):
            result = cli_main._handle_menu_create_session(cli_main.default_config_path())

        self.assertEqual(result, 0)
        mocked_create.assert_not_called()

    def test_handle_menu_skill_activate_returns_zero_when_no_assets(self) -> None:
        with (
            patch.object(cli_main, "_load_effective_config", return_value={}),
            patch.object(cli_main, "list_generated_skill_assets", return_value=[]),
        ):
            result = cli_main._handle_menu_skill_activate(cli_main.default_config_path())

        self.assertEqual(result, 0)

    def test_handle_menu_skill_activate_applies_selected_asset(self) -> None:
        asset = SkillAsset(
            index=1,
            path=Path(r"C:\tmp\skill\SKILL.md"),
            relative_path=r"skills\generated\demo\SKILL.md",
            folder_name="demo",
            name="데모 스타일",
            description="설명",
            is_active=False,
        )
        config = {"skills": {}}
        with (
            patch.object(cli_main, "_load_effective_config", return_value=config),
            patch.object(cli_main, "list_generated_skill_assets", return_value=[asset]),
            patch.object(cli_main, "_prompt_menu_text", return_value="1"),
            patch.object(cli_main, "activate_meeting_output_override") as mocked_activate,
        ):
            result = cli_main._handle_menu_skill_activate(cli_main.default_config_path())

        self.assertEqual(result, 0)
        mocked_activate.assert_called_once()
        kwargs = mocked_activate.call_args.kwargs
        self.assertEqual(kwargs["config"], config)
        self.assertEqual(kwargs["skill_path"], asset.path)

if __name__ == "__main__":
    unittest.main()
