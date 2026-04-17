from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from local_meeting_ai_runtime.meeting_output_skill import resolve_generated_meeting_output_dir
from zoom_meeting_bot_cli.config import (
    DEFAULT_WHISPER_CPP_MODEL_NAME,
    build_default_config,
    default_config_path,
    suggest_whisper_cpp_command,
    suggest_whisper_cpp_model,
)
from zoom_meeting_bot_cli.package_manager import build_distribution_bundle
from zoom_meeting_bot_cli.runtime_env import build_runtime_env


class PackagingPathsTest(unittest.TestCase):
    def test_default_config_path_uses_workspace_env_when_present(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"ZOOM_MEETING_BOT_HOME": temp_dir},
            clear=False,
        ):
            self.assertEqual(default_config_path(), Path(temp_dir) / "zoom-meeting-bot.config.json")

    def test_runtime_env_keeps_default_skill_in_package_and_data_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"ZOOM_MEETING_BOT_HOME": temp_dir},
            clear=False,
        ):
            config = build_default_config()

            env = build_runtime_env(config)

            self.assertTrue(Path(env["DELEGATE_MEETING_OUTPUT_SKILL_PATH"]).is_file())
            self.assertIn(str(Path(temp_dir)), env["DELEGATE_STORE_PATH"])
            self.assertIn(str(Path(temp_dir)), env["DELEGATE_EXPORT_DIR"])
            self.assertIn(str(Path(temp_dir)), env["DELEGATE_AUDIO_ARCHIVE_DIR"])
            self.assertIn(str(Path(temp_dir)), env["DELEGATE_GENERATED_MEETING_OUTPUT_DIR"])
            self.assertEqual(env["DELEGATE_MEETING_ARTIFACT_PDF_RENDERER"], "html")

    def test_generated_skill_dir_defaults_to_workspace_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"ZOOM_MEETING_BOT_HOME": temp_dir},
            clear=False,
        ):
            generated_dir = resolve_generated_meeting_output_dir()

            self.assertEqual(generated_dir, Path(temp_dir) / "skills" / "generated")

    def test_distribution_bundle_includes_default_reference_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "bundle.zip"

            payload = build_distribution_bundle(output_path=output_path, include_notes=False)

            self.assertTrue(Path(payload["bundle_path"]).exists())
            with ZipFile(output_path) as archive:
                names = set(archive.namelist())
            self.assertIn("skills/meeting-output-default/SKILL.md", names)
            self.assertNotIn("skills/meeting-output-decision-focused/SKILL.md", names)
            self.assertNotIn("skills/meeting-output-summary-action-first/SKILL.md", names)

    def test_suggest_whisper_cpp_assets_prefers_prepared_runtime_asset_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {"LOCALAPPDATA": temp_dir},
            clear=False,
        ):
            asset_root = Path(temp_dir) / "local-meeting-ai-runtime" / "whisper.cpp"
            cli_path = asset_root / "bin" / ("whisper-cli.exe" if os.name == "nt" else "whisper-cli")
            model_path = asset_root / "models" / f"ggml-{DEFAULT_WHISPER_CPP_MODEL_NAME}.bin"
            cli_path.parent.mkdir(parents=True, exist_ok=True)
            model_path.parent.mkdir(parents=True, exist_ok=True)
            cli_path.write_text("placeholder", encoding="utf-8")
            model_path.write_text("placeholder", encoding="utf-8")

            self.assertEqual(suggest_whisper_cpp_command(), str(cli_path))
            self.assertEqual(suggest_whisper_cpp_model(), str(model_path))


if __name__ == "__main__":
    unittest.main()
