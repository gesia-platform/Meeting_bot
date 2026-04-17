from __future__ import annotations

import argparse
from datetime import datetime
import httpx
import importlib.metadata
import importlib.util
import json
import platform
import re
import shutil
import sys
import time
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from local_meeting_ai_runtime.assets import find_whisper_cpp_cli, find_whisper_cpp_model

from .config import (
    DEFAULT_WHISPER_CPP_MODEL_NAME,
    build_default_config,
    build_preset_config,
    default_config_path,
    load_config,
    merge_config,
    PRESET_CHOICES,
    sanitize_text_input,
    suggest_workspace_name,
    write_config,
)
from .cuda_support import detect_cuda_gpu, inspect_torch_runtime
from .launcher_manager import (
    read_launcher_status as read_full_launcher_status,
    start_launcher as start_full_launcher,
    stop_launcher as stop_full_launcher,
)
from .runtime_manager import (
    create_runtime_session,
    get_runtime_session,
    list_runtime_sessions,
    read_runtime_status,
    start_runtime,
    stop_runtime,
)
from .skill_manager import (
    activate_meeting_output_override,
    append_skill_compose_message,
    build_interactive_skill_target_path,
    build_session_skill_refinement_prompt,
    clear_meeting_output_override,
    describe_skill_state,
    finalize_composed_skill,
    interpret_skill_compose_reply,
    list_generated_skill_assets,
    prepare_skill_compose_workspace,
    resolve_codex_command,
    resolve_skill_asset_selection,
    run_skill_compose_turn,
    summarize_composed_skill_for_user,
    write_skill_compose_user_message,
)
from local_meeting_ai_runtime.meeting_output_skill import (
    resolve_generated_meeting_output_dir,
    resolve_meeting_output_skill_path,
)

from .paths import package_root, resolve_relative_path, resolve_workspace_path, workspace_root
from .runtime_env import runtime_host, runtime_port, runtime_state_path
from .model_manager import prepare_models
from .package_manager import build_distribution_bundle
from .platform_support import MACOS, command_candidates_exist, current_platform_id, tool_install_plans
from .setup_manager import run_setup

EXECUTION_MODE_CHOICES: list[tuple[str, str, str]] = [
    (
        "runtime_only",
        "runtime_only",
        "Zoom 회의 엔진만 실행합니다. PDF 생성까지만 먼저 확인하고 싶은 경우에 적합합니다.",
    ),
    (
        "launcher",
        "launcher",
        "Zoom 런타임과 artifact 전달 계층을 함께 실행합니다. Telegram 자동 전달까지 고려할 때 적합합니다.",
    ),
]

COMPLETION_MODE_CHOICES: list[tuple[str, str, str]] = [
    (
        "inline",
        "inline",
        "회의 종료 뒤 무거운 후처리를 지금 런타임 안에서 바로 끝냅니다. 기존 동작에 가장 가깝습니다.",
    ),
    (
        "queued",
        "queued",
        "회의 종료 뒤 무거운 후처리를 별도 finisher가 이어받습니다. 흐름과 결과물은 유지하면서 메모리 부담을 낮추는 쪽입니다.",
    ),
]

RUNNER_BACKEND_CHOICES: list[tuple[str, str, str]] = [
    ("none", "none", "Telegram 대화 runner는 켜지지 않습니다. Zoom 런타임과 PDF 전달만 사용합니다."),
    (
        "metheus_cli",
        "metheus_cli",
        "메테우스 CLI가 이미 준비된 PC에서 Telegram 대화 runner를 함께 사용합니다.",
    ),
]

ROUTE_MODE_CHOICES: list[tuple[str, str, str]] = [
    ("metheus_project", "메테우스 프로젝트", "project_id와 destination_label 기준으로 보냅니다."),
    ("personal_dm", "개인 1:1 DM", "특정 Telegram 개인 chat_id로 보냅니다."),
    ("telegram_chat", "일반 그룹/채널", "메테우스 없이 일반 Telegram chat_id로 보냅니다."),
    ("none", "사용 안 함", "이 route는 비활성화합니다."),
]


def main(argv: list[str] | None = None) -> int:
    try:
        _configure_utf8_stdio()
        parsed_argv = list(sys.argv[1:] if argv is None else argv)
        if not parsed_argv:
            return handle_menu(argparse.Namespace(config=default_config_path()))
        parser = build_parser()
        args = parser.parse_args(parsed_argv)
        return args.func(args)
    except FileNotFoundError as exc:
        print(str(exc))
        return 1
    except Exception as exc:
        print(f"Command failed: {exc}")
        return 1


def _configure_utf8_stdio() -> None:
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            continue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zoom-meeting-bot",
        description="Generic CLI kit for local Zoom meeting bot packaging.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=default_config_path(),
        help="Path to the bot config file. Defaults to ./zoom-meeting-bot.config.json",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    menu_parser = subparsers.add_parser(
        "menu",
        help="Open the Korean menu UI for everyday operation.",
    )
    menu_parser.set_defaults(func=handle_menu)

    init_parser = subparsers.add_parser("init", help="Create a new bot config file.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite the config file if it already exists.")
    init_parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Write the default template without prompting for values.",
    )
    init_parser.add_argument(
        "--preset",
        choices=PRESET_CHOICES,
        default="runtime_only",
        help="Choose a starting preset for the first config file.",
    )
    init_parser.set_defaults(func=handle_init)

    configure_parser = subparsers.add_parser("configure", help="Edit the bot config interactively.")
    configure_parser.set_defaults(func=handle_configure)

    show_config_parser = subparsers.add_parser("show-config", help="Show the effective config with secrets masked.")
    show_config_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the sanitized effective config as JSON.",
    )
    show_config_parser.set_defaults(func=handle_show_config)

    support_bundle_parser = subparsers.add_parser(
        "support-bundle",
        help="Write a sanitized support bundle for troubleshooting on another PC.",
    )
    support_bundle_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional output path. Defaults to ./.tmp/zoom-meeting-bot/support-bundle-YYYYMMDD-HHMMSS.json",
    )
    support_bundle_parser.set_defaults(func=handle_support_bundle)

    doctor_parser = subparsers.add_parser("doctor", help="Validate local prerequisites and config completeness.")
    doctor_parser.add_argument(
        "--mode",
        choices=("current", "runtime_only", "launcher"),
        default="current",
        help="Check readiness for the current config mode, or force runtime_only / launcher readiness.",
    )
    doctor_parser.set_defaults(func=handle_doctor)

    setup_parser = subparsers.add_parser("setup", help="Install local prerequisites and create base directories.")
    setup_parser.add_argument("--yes", action="store_true", help="Accept recommended installation prompts automatically.")
    setup_parser.add_argument("--skip-python", action="store_true", help="Skip Python dependency installation.")
    setup_parser.add_argument("--skip-tools", action="store_true", help="Skip external tool installation.")
    setup_parser.add_argument("--skip-directories", action="store_true", help="Skip workspace directory creation.")
    setup_parser.add_argument("--skip-models", action="store_true", help="Skip transcribe/diarization model preparation.")
    setup_parser.set_defaults(func=handle_setup)

    quickstart_parser = subparsers.add_parser(
        "quickstart",
        help="Run the first-time setup flow in one command: init/configure/setup/doctor/start.",
    )
    quickstart_parser.add_argument(
        "--preset",
        choices=PRESET_CHOICES,
        default="launcher_dm",
        help="Choose the starting preset when creating a new config. Default: launcher_dm",
    )
    quickstart_parser.add_argument(
        "--force-init",
        action="store_true",
        help="Recreate the config from the preset before configuring it.",
    )
    quickstart_parser.add_argument(
        "--yes",
        action="store_true",
        help="Accept recommended setup/model preparation prompts automatically.",
    )
    quickstart_parser.add_argument(
        "--skip-setup",
        action="store_true",
        help="Skip the installation/setup step and only run configure/doctor/start.",
    )
    quickstart_parser.add_argument(
        "--skip-start",
        action="store_true",
        help="Stop after doctor instead of starting the runtime or launcher.",
    )
    quickstart_parser.set_defaults(func=handle_quickstart)

    prepare_models_parser = subparsers.add_parser(
        "prepare-models",
        help="Pre-download local transcription and diarization models.",
    )
    prepare_models_parser.add_argument("--yes", action="store_true", help="Accept model preparation prompts automatically.")
    prepare_models_parser.add_argument(
        "--skip-transcribe",
        action="store_true",
        help="Skip faster-whisper transcription model preparation.",
    )
    prepare_models_parser.add_argument(
        "--skip-diarization",
        action="store_true",
        help="Skip pyannote diarization model preparation.",
    )
    prepare_models_parser.set_defaults(func=handle_prepare_models)

    package_parser = subparsers.add_parser(
        "package",
        help="Build a pilot distribution bundle from the current workspace.",
    )
    package_parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional zip output path. Defaults to ./.dist/zoom-meeting-bot-alpha-YYYYMMDD-HHMMSS.zip",
    )
    package_parser.add_argument(
        "--exclude-notes",
        action="store_true",
        help="Exclude notes/ from the bundle if you want a slimmer distribution zip.",
    )
    package_parser.set_defaults(func=handle_package)

    start_parser = subparsers.add_parser("start", help="Start the copied Zoom meeting runtime with injected config env.")
    start_parser.set_defaults(func=handle_start)

    status_parser = subparsers.add_parser("status", help="Show copied runtime status and health.")
    status_parser.set_defaults(func=handle_status)

    stop_parser = subparsers.add_parser("stop", help="Stop the copied runtime process.")
    stop_parser.set_defaults(func=handle_stop)

    create_session_parser = subparsers.add_parser(
        "create-session",
        help="Create a meeting session from a Zoom link and passcode.",
    )
    create_session_parser.add_argument("join_url", help="Zoom meeting join URL.")
    create_session_parser.add_argument("--passcode", required=True, help="Zoom meeting passcode.")
    create_session_parser.add_argument("--meeting-number", default="", help="Zoom meeting number if already known.")
    create_session_parser.add_argument("--meeting-topic", default="", help="Optional meeting topic/title.")
    create_session_parser.add_argument("--requested-by", default="", help="Operator or requester name.")
    create_session_parser.add_argument("--instructions", default="", help="Optional meeting instructions for the bot.")
    create_session_parser.add_argument(
        "--delegate-mode",
        default="answer_on_ask",
        help="Delegate mode to pass through to the runtime. Default: answer_on_ask",
    )
    create_session_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw runtime response as JSON instead of the friendly summary.",
    )
    create_session_parser.add_argument(
        "--open",
        action="store_true",
        help="Open the session join/control URL immediately after creation.",
    )
    create_session_parser.add_argument(
        "--open-target",
        choices=("browser_auto", "browser", "desktop", "config"),
        default="browser_auto",
        help="Which URL to open when --open is used. Default: browser_auto",
    )
    create_session_parser.add_argument(
        "--no-start",
        action="store_true",
        help="Do not auto-start the runtime when it is not reachable.",
    )
    create_session_parser.add_argument(
        "--startup-wait-seconds",
        type=float,
        default=20.0,
        help="How long to wait for the runtime API after auto-start. Default: 20",
    )
    create_session_parser.set_defaults(func=handle_create_session)

    list_sessions_parser = subparsers.add_parser(
        "list-sessions",
        help="List delegate sessions currently known to the runtime.",
    )
    list_sessions_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw runtime response as JSON instead of the friendly summary.",
    )
    list_sessions_parser.set_defaults(func=handle_list_sessions)

    show_session_parser = subparsers.add_parser(
        "show-session",
        help="Show one delegate session in detail.",
    )
    show_session_parser.add_argument("session_id", help="Delegate session ID.")
    show_session_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw runtime response as JSON instead of the friendly summary.",
    )
    show_session_parser.set_defaults(func=handle_show_session)

    open_session_parser = subparsers.add_parser(
        "open-session",
        help="Open the join page or control page for one delegate session.",
    )
    open_session_parser.add_argument("session_id", help="Delegate session ID.")
    open_session_parser.add_argument(
        "--target",
        choices=("browser_auto", "browser", "desktop", "config"),
        default="browser_auto",
        help="Which URL to open. Default: browser_auto",
    )
    open_session_parser.add_argument(
        "--print-only",
        action="store_true",
        help="Resolve the URL and print it without opening a browser.",
    )
    open_session_parser.set_defaults(func=handle_open_session)

    skill_parser = subparsers.add_parser(
        "skill",
        help="Compose or manage reusable meeting-output skills.",
    )
    skill_subparsers = skill_parser.add_subparsers(dest="skill_command", required=True)

    skill_compose_parser = skill_subparsers.add_parser(
        "compose",
        help="Open a conversational skill-authoring flow for meeting-output customization.",
    )
    skill_compose_parser.add_argument(
        "--name",
        default="",
        help="Optional label used in the generated skill folder name.",
    )
    skill_compose_parser.add_argument(
        "--prompt",
        default="",
        help="Optional first request to seed the skill-authoring conversation.",
    )
    skill_compose_parser.add_argument(
        "--fallback-only",
        action="store_true",
        help="Do not launch Codex even if it is installed; save the request as a deferred customization instead.",
    )
    skill_compose_parser.add_argument(
        "--no-activate",
        action="store_true",
        help="Create the generated skill file but do not connect it to the active config automatically.",
    )
    skill_compose_parser.set_defaults(func=handle_skill_compose)

    skill_refine_parser = skill_subparsers.add_parser(
        "refine",
        help="Turn feedback on a completed meeting result into a reusable meeting-output skill.",
    )
    skill_refine_parser.add_argument(
        "--session-id",
        required=True,
        help="Completed delegate session ID to use as result context.",
    )
    skill_refine_parser.add_argument(
        "--prompt",
        default="",
        help="Feedback such as '이번 결과물은 너무 딱딱했어. 다음엔 액션 아이템을 맨 위에 둬줘.'",
    )
    skill_refine_parser.add_argument(
        "--name",
        default="",
        help="Optional label used in the generated skill folder name.",
    )
    skill_refine_parser.add_argument(
        "--fallback-only",
        action="store_true",
        help="Do not launch Codex even if it is installed; save the refinement request as a deferred customization instead.",
    )
    skill_refine_parser.add_argument(
        "--no-activate",
        action="store_true",
        help="Create the refined skill file but do not connect it to the active config automatically.",
    )
    skill_refine_parser.set_defaults(func=handle_skill_refine)

    skill_status_parser = skill_subparsers.add_parser(
        "status",
        help="Show the current base skill, override skill, and deferred customization state.",
    )
    skill_status_parser.set_defaults(func=handle_skill_status)

    skill_list_parser = skill_subparsers.add_parser(
        "list",
        help="List generated skill assets that can be activated.",
    )
    skill_list_parser.set_defaults(func=handle_skill_list)

    skill_activate_parser = skill_subparsers.add_parser(
        "activate",
        help="Activate one saved generated skill without editing config keys manually.",
    )
    skill_activate_parser.add_argument(
        "selector",
        nargs="?",
        default="",
        help="Optional skill index, skill name, folder name, or relative path from `skill list`.",
    )
    skill_activate_parser.add_argument(
        "--latest",
        action="store_true",
        help="Activate the most recently generated skill.",
    )
    skill_activate_parser.set_defaults(func=handle_skill_activate)

    skill_clear_parser = skill_subparsers.add_parser(
        "clear",
        help="Clear the active override skill from the current config.",
    )
    skill_clear_parser.add_argument(
        "--all",
        action="store_true",
        help="Also clear any deferred natural-language customization request.",
    )
    skill_clear_parser.set_defaults(func=handle_skill_clear)

    return parser


def handle_menu(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    while True:
        print()
        print("ZOOM_MEETING_BOT")
        print("----------------")
        if config_path.exists():
            print(f"설정 파일: {config_path.resolve()}")
        else:
            print(f"설정 파일이 아직 없습니다: {config_path.resolve()}")
            print("처음 사용이라면 [1] 처음 설정하기를 먼저 진행해 주세요.")
        print()
        print("[1] 처음 설정하기")
        print("[2] 런처 시작")
        print("[3] 런처 중지")
        print("[4] 회의 참가")
        print("[5] 현재 상태 보기")
        print("[6] 결과물 스타일 관리")
        print("[0] 종료")
        choice = input("\n번호를 선택해 주세요: ").strip().lower()

        if choice in {"0", "q", "quit", "exit"}:
            print("종료합니다.")
            return 0
        if choice == "1":
            result = handle_quickstart(
                argparse.Namespace(
                    config=config_path,
                    preset="launcher_dm",
                    force_init=False,
                    yes=True,
                    skip_setup=False,
                    skip_start=False,
                )
            )
            _pause_menu()
            continue
        if choice == "2":
            if not _menu_require_config(config_path):
                _pause_menu()
                continue
            result = handle_start(argparse.Namespace(config=config_path))
            _pause_menu()
            continue
        if choice == "3":
            if not _menu_require_config(config_path):
                _pause_menu()
                continue
            result = handle_stop(argparse.Namespace(config=config_path))
            _pause_menu()
            continue
        if choice == "4":
            result = _handle_menu_create_session(config_path)
            _pause_menu()
            continue
        if choice == "5":
            result = _handle_menu_show_status(config_path)
            _pause_menu()
            continue
        if choice == "6":
            result = _handle_menu_skill_library(config_path)
            _pause_menu()
            continue

        print("올바른 번호를 선택해 주세요.")


def _handle_menu_show_status(config_path: Path) -> int:
    if not _menu_require_config(config_path):
        return 1
    config = _load_effective_config(config_path)
    profile = dict(config.get("profile") or {})
    telegram = dict(config.get("telegram") or {})
    startup_blockers = _collect_startup_blockers(config)
    execution_mode = _execution_mode(config)
    mode_summary = _mode_summary(config)
    state = describe_skill_state(config)
    active_skill_label = _menu_active_skill_label(state)
    launcher_status: dict[str, Any] | None = None
    runtime_status: dict[str, Any] | None = None

    try:
        if execution_mode == "launcher":
            launcher_status = read_full_launcher_status(config, config_path=config_path)
            runtime_status = dict(launcher_status.get("zoom_runtime") or {})
        else:
            runtime_status = read_runtime_status(config, config_path=config_path)
    except Exception as exc:
        print("현재 상태를 읽는 중 문제가 생겼습니다.")
        print(f"- {exc}")
        return 1

    launcher_running = str((launcher_status or {}).get("status") or "").strip().lower() == "running"
    runtime_alive = bool(runtime_status and runtime_status.get("alive"))
    finalizer_alive = bool(dict((launcher_status or {}).get("finalizer") or {}).get("alive"))
    finalizer_enabled = bool(dict((launcher_status or {}).get("finalizer") or {}).get("enabled"))
    readiness_label = "준비됨" if not startup_blockers else "점검 필요"

    print("현재 상태")
    print("---------")
    print(f"- 봇 이름: {profile.get('bot_name') or '(미설정)'}")
    print(f"- 작업 공간: {profile.get('workspace_name') or '(미설정)'}")
    print(f"- 실행 방식: {mode_summary}")
    print(f"- 설정 상태: {'완료' if config_path.exists() else '미설정'}")
    print(f"- 회의 참가 준비: {readiness_label}")
    if startup_blockers:
        print(f"  점검 항목 {len(startup_blockers)}개가 있습니다.")
    print(f"- 런처 상태: {'실행 중' if launcher_running else '중지됨' if execution_mode == 'launcher' else '사용 안 함'}")
    print(f"- Zoom 엔진 상태: {'실행 중' if runtime_alive else '중지됨'}")
    if execution_mode == "launcher":
        if finalizer_enabled:
            print(f"- 결과물 마무리 엔진: {'실행 중' if finalizer_alive else '중지됨'}")
        else:
            print("- 결과물 마무리 엔진: 사용 안 함")
    print(f"- Telegram 연동: {'사용' if bool(telegram.get('enabled')) else '사용 안 함'}")
    if bool(telegram.get("enabled")):
        print(f"- PDF 전달 방식: {_describe_route(dict(telegram.get('artifact_route') or {}))}")
    print(f"- 현재 결과물 스타일: {active_skill_label}")

    if not _prompt_bool("상세 기술 정보도 볼까요", False):
        return 0

    print()
    show_result = handle_show_config(
        argparse.Namespace(
            config=config_path,
            json=False,
        )
    )
    print()
    print("런타임 상태")
    print("-----------")
    status_result = handle_status(argparse.Namespace(config=config_path))
    print()
    print("결과물 스타일 상태")
    print("------------------")
    skill_result = handle_skill_status(argparse.Namespace(config=config_path))
    return max(show_result, status_result, skill_result)


def _handle_menu_create_session(config_path: Path) -> int:
    if not _menu_require_config(config_path):
        return 1
    args = _build_menu_create_session_args(config_path)
    if args is None:
        print("회의 참가를 취소했습니다.")
        return 0
    return handle_create_session(args)


def _handle_menu_skill_compose(config_path: Path) -> int:
    if not _menu_require_config(config_path):
        return 1
    config = _load_effective_config(config_path)
    skill_state = describe_skill_state(config)
    codex_command = resolve_codex_command(config)

    print()
    print("새 결과물 스타일 만들기")
    print("-----------------------")
    print("원하는 회의록 분위기와 요구사항을 편하게 말씀해 주세요.")
    print("예: 회사 분위기, 색감, 폰트 느낌, 중요하게 다루고 싶은 파트, 이미지 필요 여부")
    initial_request = _prompt_required("요구사항", "")
    if initial_request.strip() == "/cancel":
        print("취소했습니다.")
        return 0

    if codex_command:
        target_path = build_interactive_skill_target_path(config, label="")
        return _run_skill_compose_flow(
            config=config,
            config_path=config_path,
            codex_command=codex_command,
            base_skill_path=Path(skill_state["base_skill_path"]),
            target_path=target_path,
            initial_request=initial_request,
            no_activate=False,
        )

    print("Codex를 찾지 못해서 지금은 새 결과물 스타일을 만들 수 없습니다.")
    print("먼저 Codex 명령어 경로를 확인해 주세요.")
    return 1


def _handle_menu_skill_library(config_path: Path) -> int:
    if not _menu_require_config(config_path):
        return 1
    while True:
        print()
        print("결과물 스타일 관리")
        print("----------------")
        print("[1] 현재 적용된 스타일 보기")
        print("[2] 저장된 스타일 목록 보기")
        print("[3] 다른 스타일로 바꾸기")
        print("[4] 새 결과물 스타일 만들기")
        print("[5] 기본 스타일로 되돌리기")
        print("[0] 이전 메뉴로 돌아가기")
        choice = input("\n번호를 선택해 주세요: ").strip().lower()

        if choice in {"0", "b", "back"}:
            return 0
        if choice == "1":
            _show_menu_skill_current(config_path)
            continue
        if choice == "2":
            _show_menu_skill_assets(config_path)
            continue
        if choice == "3":
            _handle_menu_skill_activate(config_path)
            continue
        if choice == "4":
            _handle_menu_skill_compose(config_path)
            continue
        if choice == "5":
            _handle_menu_skill_reset(config_path)
            continue

        print("올바른 번호를 선택해 주세요.")


def _show_menu_skill_current(config_path: Path) -> int:
    config = _load_effective_config(config_path)
    state = describe_skill_state(config)
    active_label = _menu_active_skill_label(state)

    print()
    print("현재 적용된 결과물 스타일")
    print("------------------------")
    print(f"- 현재 스타일: {active_label}")
    print(f"- 기본 스타일: {state['base_skill_path']}")
    if state["override_skill_path"]:
        print(f"- 적용 중인 스타일: {state['override_skill_path']}")
    else:
        print("- 적용 중인 스타일: 없음 (기본 스타일 사용 중)")
    return 0


def _show_menu_skill_assets(config_path: Path) -> int:
    config = _load_effective_config(config_path)
    state = describe_skill_state(config)
    assets = list_generated_skill_assets(config)

    print()
    print("저장된 결과물 스타일 목록")
    print("------------------------")
    print(f"- 생성된 스타일 폴더: {state['generated_skill_dir']}")
    if not assets:
        print("- 아직 생성된 결과물 스타일이 없습니다.")
        print("- [4] 새 결과물 스타일 만들기로 먼저 스타일을 만들 수 있습니다.")
        return 0

    for asset in assets:
        active_marker = " [현재 적용 중]" if asset.is_active else ""
        print(f"{asset.index}. {asset.name}{active_marker}")
        if asset.description:
            print(f"   - 설명: {asset.description}")
        print(f"   - 위치: {asset.relative_path}")
    return 0


def _handle_menu_skill_activate(config_path: Path) -> int:
    config = _load_effective_config(config_path)
    assets = list_generated_skill_assets(config)

    print()
    print("다른 결과물 스타일로 바꾸기")
    print("--------------------------")
    if not assets:
        print("- 적용할 결과물 스타일이 아직 없습니다.")
        print("- [4] 새 결과물 스타일 만들기로 먼저 만들어 주세요.")
        return 0

    for asset in assets:
        active_marker = " [현재 적용 중]" if asset.is_active else ""
        print(f"[{asset.index}] {asset.name}{active_marker}")
    print("취소하려면 /cancel 을 입력해 주세요.")

    selector = _prompt_menu_text("바꿀 스타일 번호", required=True)
    if selector is None:
        print("스타일 변경을 취소했습니다.")
        return 0

    selected = resolve_skill_asset_selection(assets, selector)
    if selected is None:
        print("올바른 번호를 고르지 못했습니다.")
        return 1

    activate_meeting_output_override(
        config=config,
        config_path=config_path,
        skill_path=selected.path,
        clear_customization=True,
    )
    print(f"현재 적용 스타일을 바꿨습니다: {selected.name}")
    return 0


def _handle_menu_skill_reset(config_path: Path) -> int:
    config = _load_effective_config(config_path)
    state = describe_skill_state(config)
    print()
    if not state["override_skill_path"]:
        print("이미 기본 결과물 스타일을 사용 중입니다.")
        return 0

    clear_meeting_output_override(
        config=config,
        config_path=config_path,
        clear_customization=False,
    )
    print("기본 결과물 스타일로 되돌렸습니다.")
    return 0


def _menu_require_config(config_path: Path) -> bool:
    if config_path.exists():
        return True
    print("아직 설정 파일이 없습니다.")
    print("먼저 [1] 처음 설정하기를 진행해 주세요.")
    return False


def _pause_menu() -> None:
    print()
    input("Enter를 누르면 메뉴로 돌아갑니다...")


def _menu_active_skill_label(state: dict[str, str]) -> str:
    override_path = str(state.get("override_skill_path") or "").strip()
    if override_path:
        return Path(override_path).parent.name
    base_path = str(state.get("base_skill_path") or "").strip()
    if base_path:
        return f"기본 스타일 ({Path(base_path).parent.name})"
    return "기본 스타일"


def _build_menu_create_session_args(config_path: Path) -> argparse.Namespace | None:
    print()
    print("회의 참가")
    print("---------")
    print("회의 링크와 암호를 입력해 주세요.")
    print("중간에 취소하려면 /cancel 을 입력하면 됩니다.")

    join_url = _prompt_menu_text("회의 링크", required=True)
    if join_url is None:
        return None
    passcode = _prompt_menu_text("암호", required=True)
    if passcode is None:
        return None

    return argparse.Namespace(
        config=config_path,
        join_url=join_url,
        passcode=passcode,
        meeting_number="",
        meeting_topic="",
        requested_by="",
        instructions="",
        delegate_mode="answer_on_ask",
        json=False,
        open=True,
        open_target="browser_auto",
        no_start=False,
        startup_wait_seconds=20.0,
    )


def _prompt_menu_text(label: str, *, required: bool = False, help_text: str = "") -> str | None:
    while True:
        if help_text:
            print(f"   {help_text}")
        value = input(f"{label}: ")
        cleaned = sanitize_text_input(value).strip()
        if cleaned == "/cancel":
            return None
        if cleaned:
            return cleaned
        if not required:
            return ""
        print("   빈 값으로 둘 수 없습니다. 취소하려면 /cancel 을 입력해 주세요.")


def handle_init(args: argparse.Namespace) -> int:
    config_path: Path = args.config
    if config_path.exists() and not args.force:
        print(f"Config already exists: {config_path}")
        print(f"Use --force to overwrite it, or run `{_script_cli_command('configure')}`.")
        return 1

    config = build_preset_config(args.preset)
    if not args.non_interactive:
        config = _collect_interactive_config(config)

    write_config(config_path, config)
    print(f"Created config: {config_path}")
    return 0


def handle_configure(args: argparse.Namespace) -> int:
    config_path: Path = args.config
    if config_path.exists():
        config = merge_config(build_default_config(), load_config(config_path))
    else:
        config = build_default_config()

    updated = _collect_interactive_config(config)
    write_config(config_path, updated)
    print(f"Updated config: {config_path}")
    return 0


def handle_skill_compose(args: argparse.Namespace) -> int:
    config_path: Path = args.config
    config = _load_effective_config(config_path)
    skill_state = describe_skill_state(config)
    codex_command = "" if bool(args.fallback_only) else resolve_codex_command(config)
    initial_request = str(args.prompt or "").strip()

    print("새 결과물 스타일 만들기")
    print("원하는 회의 결과물 생성 방식을 자연어로 말씀해 주세요.")
    print("빈 줄 또는 /done 으로 마치고, /cancel 이면 저장 없이 종료합니다.")

    if not initial_request:
        initial_request = _prompt_required("첫 요청", "")
    if initial_request.strip() == "/cancel":
        print("취소했습니다.")
        return 0

    if codex_command:
        target_path = build_interactive_skill_target_path(config, label=str(args.name or "").strip())
        compose_workspace = prepare_skill_compose_workspace(
            base_skill_path=Path(skill_state["base_skill_path"]),
            final_output_path=target_path,
        )
        pending_request = initial_request
        exit_code = 0
        while True:
            write_skill_compose_user_message(
                workspace_dir=Path(compose_workspace["sandbox_dir"]),
                text=pending_request,
            )
            append_skill_compose_message(
                workspace_dir=Path(compose_workspace["sandbox_dir"]),
                role="User",
                text=pending_request,
            )
            print()
            print("요청을 바탕으로 결과물 스타일 초안을 만들고 있습니다...")
            turn_result = run_skill_compose_turn(
                codex_command=codex_command,
                workspace_dir=Path(compose_workspace["sandbox_dir"]),
            )
            exit_code = int(turn_result["exit_code"])
            raw_assistant_reply = str(turn_result["assistant_reply"] or "").strip()
            parsed_reply = interpret_skill_compose_reply(raw_assistant_reply)
            assistant_reply = str(parsed_reply["text"] or "").strip()
            if assistant_reply:
                append_skill_compose_message(
                    workspace_dir=Path(compose_workspace["sandbox_dir"]),
                    role="Assistant",
                    text=assistant_reply,
                )
                print()
                print(f"도우미: {assistant_reply}")
            if exit_code != 0:
                print()
                print("결과물 스타일 초안을 정상적으로 마무리하지 못했습니다.")
                return 1
            prompt_label = (
                "\n답변 또는 추가 요청(없으면 Enter): "
                if parsed_reply["kind"] == "question"
                else "\n추가 요청이 없으면 Enter를 눌러 저장합니다: "
            )
            next_request = input(prompt_label).strip()
            if not next_request or next_request == "/done":
                break
            if next_request == "/cancel":
                print("취소했습니다. 새 skill은 저장하지 않았습니다.")
                return 0
            pending_request = next_request
        finalized_skill_path = finalize_composed_skill(
            sandbox_skill_path=Path(compose_workspace["sandbox_skill_path"]),
            final_output_path=target_path,
        )
        if not finalized_skill_path:
            print("완성된 skill 초안을 찾지 못해서 저장하지 않았습니다.")
            return 1 if exit_code != 0 else 0
        print()
        print(f"새 skill을 저장했습니다: {finalized_skill_path}")
        print(summarize_composed_skill_for_user(Path(finalized_skill_path)))
        if bool(args.no_activate):
            print("활성화는 건너뛰었습니다.")
            return 0
        should_activate = _prompt_bool("이 skill을 앞으로 회의 결과물에 바로 적용할까요", True)
        if should_activate:
            activate_meeting_output_override(
                config=config,
                config_path=config_path,
                skill_path=Path(finalized_skill_path),
                clear_customization=True,
            )
            print("새 skill을 활성화했습니다.")
        else:
            print("skill 파일은 남겨두고, 설정은 바꾸지 않았습니다.")
        return 0

    print("Codex를 찾지 못해서 자연어 요청만 저장해 두겠습니다.")
    updated = dict(config)
    skills = dict(updated.get("skills") or {})
    skills["meeting_output_customization"] = initial_request
    skills["meeting_output_override_path"] = ""
    updated["skills"] = skills
    write_config(config_path, updated)
    print("나중에 재사용할 수 있도록 요청을 저장했습니다.")
    return 0


def _run_skill_compose_flow(
    *,
    config: dict[str, Any],
    config_path: Path,
    codex_command: str,
    base_skill_path: Path,
    target_path: Path,
    initial_request: str,
    no_activate: bool,
) -> int:
    compose_workspace = prepare_skill_compose_workspace(
        base_skill_path=base_skill_path,
        final_output_path=target_path,
    )
    pending_request = initial_request
    exit_code = 0
    while True:
        write_skill_compose_user_message(
            workspace_dir=Path(compose_workspace["sandbox_dir"]),
            text=pending_request,
        )
        append_skill_compose_message(
            workspace_dir=Path(compose_workspace["sandbox_dir"]),
            role="User",
            text=pending_request,
        )
        print()
        print("요청을 바탕으로 결과물 스타일 초안을 만들고 있습니다...")
        turn_result = run_skill_compose_turn(
            codex_command=codex_command,
            workspace_dir=Path(compose_workspace["sandbox_dir"]),
        )
        exit_code = int(turn_result["exit_code"])
        raw_assistant_reply = str(turn_result["assistant_reply"] or "").strip()
        parsed_reply = interpret_skill_compose_reply(raw_assistant_reply)
        assistant_reply = str(parsed_reply["text"] or "").strip()
        if assistant_reply:
            append_skill_compose_message(
                workspace_dir=Path(compose_workspace["sandbox_dir"]),
                role="Assistant",
                text=assistant_reply,
            )
            print()
            print(f"도우미: {assistant_reply}")
        if exit_code != 0:
            print()
            print("결과물 스타일 초안을 정상적으로 마무리하지 못했습니다.")
            return 1
        prompt_label = (
            "\n답변 또는 추가 요청(없으면 Enter): "
            if parsed_reply["kind"] == "question"
            else "\n추가 요청이 없으면 Enter를 눌러 저장합니다: "
        )
        next_request = input(prompt_label).strip()
        if not next_request or next_request == "/done":
            break
        if next_request == "/cancel":
            print("취소했습니다. 새 skill은 저장하지 않았습니다.")
            return 0
        pending_request = next_request
    finalized_skill_path = finalize_composed_skill(
        sandbox_skill_path=Path(compose_workspace["sandbox_skill_path"]),
        final_output_path=target_path,
    )
    if not finalized_skill_path:
        print("완성된 skill 초안을 찾지 못해서 저장하지 않았습니다.")
        return 1 if exit_code != 0 else 0
    print()
    print(f"새 skill을 저장했습니다: {finalized_skill_path}")
    print(summarize_composed_skill_for_user(Path(finalized_skill_path)))
    if no_activate:
        print("활성화는 건너뛰었습니다.")
        return 0
    should_activate = _prompt_bool("이 skill을 앞으로 회의 결과물에 바로 적용할까요", True)
    if should_activate:
        activate_meeting_output_override(
            config=config,
            config_path=config_path,
            skill_path=Path(finalized_skill_path),
            clear_customization=True,
        )
        print("새 skill을 활성화했습니다.")
    else:
        print("skill 파일은 남겨두고, 설정은 바꾸지 않았습니다.")
    return 0


def handle_skill_refine(args: argparse.Namespace) -> int:
    config_path: Path = args.config
    config = _load_effective_config(config_path)
    skill_state = describe_skill_state(config)
    codex_command = "" if bool(args.fallback_only) else resolve_codex_command(config)
    feedback = str(args.prompt or "").strip()

    print("결과물 스타일 다듬기")
    print("이미 나온 회의 결과물을 본 뒤, 다음 결과물 생성 방식으로 쌓을 피드백을 말씀해 주세요.")
    if not feedback:
        feedback = _prompt_required("피드백", "")
    if feedback.strip() == "/cancel":
        print("취소했습니다.")
        return 0

    initial_request = build_session_skill_refinement_prompt(
        config=config,
        session_id=str(args.session_id or "").strip(),
        user_feedback=feedback,
    )
    base_skill_path = (
        Path(skill_state["override_skill_path"])
        if str(skill_state["override_skill_path"] or "").strip()
        else Path(skill_state["base_skill_path"])
    )

    if codex_command:
        target_path = build_interactive_skill_target_path(
            config,
            label=str(args.name or "").strip() or f"refine-{str(args.session_id or '').strip()}",
        )
        return _run_skill_compose_flow(
            config=config,
            config_path=config_path,
            codex_command=codex_command,
            base_skill_path=base_skill_path,
            target_path=target_path,
            initial_request=initial_request,
            no_activate=bool(args.no_activate),
        )

    print("Codex를 찾지 못해서 refinement 요청만 저장해 두겠습니다.")
    updated = dict(config)
    skills = dict(updated.get("skills") or {})
    skills["meeting_output_customization"] = initial_request
    skills["meeting_output_override_path"] = ""
    updated["skills"] = skills
    write_config(config_path, updated)
    print("나중에 재사용할 수 있도록 요청을 저장했습니다.")
    return 0


def handle_skill_status(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config)
    state = describe_skill_state(config)
    codex_command = resolve_codex_command(config)
    print("Meeting-output skill status")
    print("--------------------------")
    print(f"- base skill: {state['base_skill_path']}")
    print(f"- active override: {state['override_skill_path'] or '(none)'}")
    print(f"- generated skill dir: {state['generated_skill_dir']}")
    print(f"- deferred customization: {state['customization_request'] or '(none)'}")
    print(f"- codex command: {codex_command or '(not found)'}")
    return 0


def handle_skill_list(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config)
    state = describe_skill_state(config)
    assets = list_generated_skill_assets(config)
    print("Generated skill assets")
    print("----------------------")
    print(f"- generated skill dir: {state['generated_skill_dir']}")
    print(f"- active override: {state['override_skill_path'] or '(none)'}")
    if not assets:
        print("- 아직 생성된 skill 자산이 없습니다.")
        return 0
    for asset in assets:
        active_marker = " [active]" if asset.is_active else ""
        print(f"{asset.index}. {asset.name}{active_marker}")
        print(f"   - folder: {asset.folder_name}")
        if asset.description:
            print(f"   - description: {asset.description}")
        print(f"   - path: {asset.relative_path}")
    return 0


def handle_skill_activate(args: argparse.Namespace) -> int:
    config_path: Path = args.config
    config = _load_effective_config(config_path)
    assets = list_generated_skill_assets(config)
    if not assets:
        print("활성화할 generated skill 자산이 아직 없습니다.")
        return 1

    selected = None
    if bool(args.latest):
        selected = assets[0]
    elif str(args.selector or "").strip():
        selected = resolve_skill_asset_selection(assets, str(args.selector or "").strip())
        if selected is None:
            print("해당 selector로는 skill을 찾지 못했습니다.")
            print("`skill list`로 번호와 이름을 먼저 확인해 주세요.")
            return 1
    else:
        print("활성화할 skill을 골라 주세요.")
        for asset in assets:
            active_marker = " [active]" if asset.is_active else ""
            print(f"  [{asset.index}] {asset.name}{active_marker}")
        choice = _prompt_required("번호", "")
        selected = resolve_skill_asset_selection(assets, choice)
        if selected is None:
            print("올바른 번호를 고르지 못해서 종료했습니다.")
            return 1

    activate_meeting_output_override(
        config=config,
        config_path=config_path,
        skill_path=selected.path,
        clear_customization=True,
    )
    print(f"활성화했습니다: {selected.name}")
    print(f"- path: {selected.relative_path}")
    return 0


def handle_skill_clear(args: argparse.Namespace) -> int:
    config_path: Path = args.config
    config = _load_effective_config(config_path)
    clear_meeting_output_override(
        config=config,
        config_path=config_path,
        clear_customization=bool(args.all),
    )
    if bool(args.all):
        print("Cleared the active override skill and deferred customization request.")
    else:
        print("Cleared the active override skill.")
    return 0


def handle_quickstart(args: argparse.Namespace) -> int:
    config_path: Path = args.config
    print("ZOOM_MEETING_BOT quickstart")
    print(f"- config path: {config_path}")
    print(f"- preset: {args.preset}")

    if not config_path.exists() or bool(args.force_init):
        init_result = handle_init(
            argparse.Namespace(
                config=config_path,
                force=True,
                non_interactive=False,
                preset=args.preset,
            )
        )
        if init_result != 0:
            return init_result
    else:
        print(f"- existing config found, so init was skipped: {config_path}")

    configure_result = handle_configure(argparse.Namespace(config=config_path))
    if configure_result != 0:
        return configure_result

    config = _load_effective_config(config_path)
    if not bool(args.skip_setup):
        payload = run_setup(
            config=config,
            yes=bool(args.yes),
            install_python=True,
            install_tools=True,
            create_directories=True,
            prepare_local_models=True,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if _setup_requires_reboot(payload):
            print()
            print("Quickstart installed a macOS audio driver that requires one reboot before meeting-output capture becomes available.")
            print("Reboot macOS once, then run the same quickstart command again.")
            return 0

    doctor_result = handle_doctor(
        argparse.Namespace(
            config=config_path,
            mode="current",
        )
    )
    if doctor_result != 0:
        return doctor_result

    if bool(args.skip_start):
        print("Quickstart finished after doctor. Start was skipped by request.")
        return 0

    return handle_start(argparse.Namespace(config=config_path))


def _setup_requires_reboot(payload: dict[str, Any]) -> bool:
    for raw in list(payload.get("steps") or []):
        step = dict(raw or {})
        if bool(step.get("reboot_required")):
            return True
    return False


def handle_show_config(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config)
    sanitized = _sanitize_config_for_display(config)
    decorated = {
        "config_path": str(args.config.resolve()),
        "workspace_dir": str(workspace_root()),
        "package_dir": str(package_root()),
        "execution_mode": _execution_mode(config),
        "execution_mode_label": _choice_label(_execution_mode(config), EXECUTION_MODE_CHOICES),
        "completion_mode": _completion_mode(config),
        "mode_summary": _mode_summary(config),
        "resolved_paths": _resolved_paths(config),
        "config": sanitized,
    }
    if args.json:
        print(json.dumps(decorated, ensure_ascii=False, indent=2))
        return 0

    profile = dict(config.get("profile") or {})
    skills = dict(config.get("skills") or {})
    telegram = dict(config.get("telegram") or {})
    print("설정 요약")
    print("---------")
    print(f"- config: {args.config.resolve()}")
    print(f"- 작업 공간: {workspace_root()}")
    print(f"- 패키지 위치: {package_root()}")
    print(f"- bot 이름: {profile.get('bot_name') or '(미설정)'}")
    print(f"- workspace 이름: {profile.get('workspace_name') or '(미설정)'}")
    print(f"- 실행 모드: {_mode_summary(config)}")
    print(f"- runtime 주소: http://{runtime_host(config)}:{runtime_port(config)}")
    print(f"- Zoom Client ID: {_mask_visible_tail(str(dict(config.get('zoom') or {}).get('client_id') or '').strip())}")
    print(f"- Hugging Face token: {_mask_presence(str(dict(config.get('local_ai') or {}).get('huggingface_token') or '').strip())}")
    print(f"- Telegram 연동: {'사용' if bool(telegram.get('enabled')) else '사용 안 함'}")
    if bool(telegram.get("enabled")):
        print(f"- 대화 route: {_describe_route(dict(telegram.get('conversation_route') or {}))}")
        print(f"- PDF route: {_describe_route(dict(telegram.get('artifact_route') or {}))}")
        print(f"- 대화 route 지원: {_describe_supported_route_modes(_supported_conversation_route_modes(config))}")
        print(f"- PDF route 지원: {_describe_supported_route_modes(_supported_artifact_route_modes(config))}")

    print()
    print("경로")
    print("----")
    for key, value in _resolved_paths(config).items():
        print(f"- {key}: {value}")
    return 0


def handle_support_bundle(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config)
    output_path = args.output or _default_support_bundle_path()
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    execution_mode = _execution_mode(config)
    bundle = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "workspace_dir": str(workspace_root()),
        "package_dir": str(package_root()),
        "config_path": str(args.config.resolve()),
        "execution_mode": execution_mode,
        "mode_summary": _mode_summary(config),
        "resolved_paths": _resolved_paths(config),
        "system": {
            "platform": platform.platform(),
            "python_version": platform.python_version(),
        },
        "commands": {
            "codex": shutil.which(str(dict(config.get("local_ai") or {}).get("codex_command") or "").strip() or "codex"),
            "pandoc": shutil.which(str(dict(config.get("local_ai") or {}).get("pandoc_command") or "").strip() or "pandoc"),
            "libreoffice": shutil.which(str(dict(config.get("local_ai") or {}).get("libreoffice_command") or "").strip() or "soffice"),
            "brew": shutil.which("brew"),
            "winget": shutil.which("winget"),
            "metheus_governance_mcp_cli": shutil.which("metheus-governance-mcp-cli")
            or shutil.which("metheus-governance-mcp-cli.cmd"),
        },
        "config": _sanitize_config_for_display(config),
        "runtime_status": _safe_runtime_status(config, args.config),
        "launcher_status": _safe_launcher_status(config, args.config) if execution_mode == "launcher" else None,
    }
    output_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"지원 번들을 저장했습니다: {output_path}")
    return 0


def handle_doctor(args: argparse.Namespace) -> int:
    config_path: Path = args.config
    base_problems: list[str] = []
    base_warnings: list[str] = []
    telegram_problems: list[str] = []
    telegram_warnings: list[str] = []
    launcher_problems: list[str] = []
    launcher_warnings: list[str] = []

    print("ZOOM_MEETING_BOT doctor")
    print(f"- Python: {platform.python_version()}")
    print(f"- Platform: {platform.system()} {platform.release()}")
    print(f"- Config path: {config_path}")

    if not config_path.exists():
        problems = [f"Config file does not exist. Run `{_script_cli_command('init')}` first."]
        _report("config", "missing", "Run init to create the first config file.")
        _print_next_steps(
            problems,
            [],
            [
                _script_cli_command("quickstart --preset launcher_dm"),
                _script_cli_command("init"),
                _script_cli_command("configure"),
                _script_cli_command("doctor"),
            ],
        )
        return 1

    try:
        config = merge_config(build_default_config(), load_config(config_path))
        _report("config", "ok", "Config file loaded successfully.")
    except json.JSONDecodeError as exc:
        problems = [f"Config file is not valid JSON: {exc}"]
        _report("config", "error", "Config JSON is invalid.")
        _print_next_steps(
            problems,
            [],
            [
                "Fix the JSON syntax in the config file.",
                _script_cli_command("doctor"),
            ],
        )
        return 1

    _check_required(config, ("profile", "bot_name"), "profile.bot_name", base_problems)
    _check_required(config, ("zoom", "client_id"), "zoom.client_id", base_problems)
    _check_required(config, ("zoom", "client_secret"), "zoom.client_secret", base_problems)
    _check_zoom_credentials(config, base_problems, base_warnings)
    _check_required(config, ("local_ai", "meeting_output_device"), "local_ai.meeting_output_device", base_problems)
    _check_required(
        config,
        ("local_ai", "huggingface_token"),
        "local_ai.huggingface_token",
        base_warnings,
        severity="warning",
    )

    _check_command(config["local_ai"]["codex_command"], "codex command", base_problems)
    _check_command(config["local_ai"]["pandoc_command"], "pandoc", base_problems)
    _check_command(config["local_ai"]["libreoffice_command"], "libreoffice/soffice", base_problems)
    _check_local_quality_dependencies(config, base_problems, base_warnings)
    _check_telegram_config(config, telegram_problems, telegram_warnings)
    _check_launcher_config(config, launcher_problems, launcher_warnings)

    requested_mode = _doctor_mode(args, config)
    _check_route_support(
        config,
        execution_mode=requested_mode,
        problems=telegram_problems,
        warnings=telegram_warnings,
    )
    blocking_problems = list(base_problems)
    warnings = list(base_warnings)
    if requested_mode == "launcher":
        blocking_problems.extend(launcher_problems)
        blocking_problems.extend(telegram_problems)
        warnings.extend(launcher_warnings)
        warnings.extend(telegram_warnings)
    else:
        warnings.extend(launcher_warnings)
        warnings.extend(telegram_warnings)
        warnings.extend([f"[non-blocking in runtime_only] {item}" for item in launcher_problems])
        warnings.extend([f"[non-blocking in runtime_only] {item}" for item in telegram_problems])

    _print_readiness_summary(
        config,
        requested_mode=requested_mode,
        runtime_ready=not base_problems,
        launcher_ready=not (base_problems or launcher_problems or telegram_problems),
    )
    _print_next_steps(
        blocking_problems,
        warnings,
        _recommended_next_steps(
            config,
            requested_mode=requested_mode,
            blocking_problems=blocking_problems,
        ),
    )
    return _finish_report(blocking_problems, warnings)


def handle_setup(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config) if args.config.exists() else None
    payload = run_setup(
        config=config,
        yes=bool(args.yes),
        install_python=not bool(args.skip_python),
        install_tools=not bool(args.skip_tools),
        create_directories=not bool(args.skip_directories),
        prepare_local_models=not bool(args.skip_models),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def handle_prepare_models(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config)
    payload = prepare_models(
        config,
        yes=bool(args.yes),
        prepare_transcribe=not bool(args.skip_transcribe),
        prepare_diarization=not bool(args.skip_diarization),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def handle_package(args: argparse.Namespace) -> int:
    payload = build_distribution_bundle(
        output_path=args.output,
        include_notes=not bool(args.exclude_notes),
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def handle_start(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config)
    startup_blockers = _collect_startup_blockers(config)
    if startup_blockers:
        print("Start blocked due to unsupported or incomplete launcher/runtime settings.")
        for item in startup_blockers:
            print(f"- {item}")
        print(f"Run `{_script_cli_command('doctor')}` and `{_script_cli_command('configure')}` first.")
        return 1
    if _execution_mode(config) == "launcher":
        status = start_full_launcher(config, config_path=args.config)
    else:
        status = start_runtime(config, config_path=args.config)
    print(json.dumps(_decorate_status_payload(config, args.config, status), ensure_ascii=False, indent=2))
    return 0


def handle_status(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config)
    if _execution_mode(config) == "launcher":
        status = read_full_launcher_status(config, config_path=args.config)
    else:
        status = read_runtime_status(config, config_path=args.config)
    print(json.dumps(_decorate_status_payload(config, args.config, status), ensure_ascii=False, indent=2))
    return 0


def handle_stop(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config)
    if _execution_mode(config) == "launcher":
        status = stop_full_launcher(config, config_path=args.config)
    else:
        status = stop_runtime(config, config_path=args.config)
    print(json.dumps(_decorate_status_payload(config, args.config, status), ensure_ascii=False, indent=2))
    return 0


def handle_create_session(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config)
    if not _ensure_runtime_api_ready(
        config,
        args.config,
        no_start=bool(args.no_start),
        wait_seconds=float(args.startup_wait_seconds or 20.0),
    ):
        return 1

    join_url = str(args.join_url or "").strip()
    if not _looks_like_zoom_join_url(join_url):
        print("Zoom join URL looks invalid. Use a full https://... Zoom meeting link.")
        return 1

    supplied_meeting_number = str(args.meeting_number or "").strip()
    inferred_meeting_number = supplied_meeting_number or _extract_zoom_meeting_number(join_url)

    payload = create_runtime_session(
        config,
        join_url=join_url,
        passcode=args.passcode,
        meeting_number=inferred_meeting_number,
        meeting_topic=args.meeting_topic,
        requested_by=args.requested_by,
        instructions=args.instructions,
        delegate_mode=args.delegate_mode,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if inferred_meeting_number and not supplied_meeting_number:
        print(f"회의 링크에서 meeting number를 추출했습니다: {inferred_meeting_number}")
        print()

    _print_created_session_summary(payload)
    if bool(args.open):
        session = dict(payload.get("session") or {})
        resolved_url = _resolve_session_target_url(session, args.open_target)
        if not resolved_url:
            print(f"`--open-target {args.open_target}`에 해당하는 URL이 이 세션에 없습니다.")
            return 1
        print()
        print(f"자동으로 여는 중: {resolved_url}")
        if not webbrowser.open(resolved_url):
            print("브라우저를 자동으로 열지 못했습니다. 위 URL을 직접 여세요.")
            return 1
    return 0


def handle_list_sessions(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config)
    if not _runtime_api_ready(config):
        print(f"Runtime API is not reachable. Start it first with `{_script_cli_command('start')}`.")
        return 1

    payload = list_runtime_sessions(config)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    sessions = list(payload.get("sessions") or [])
    print("세션 목록")
    print("---------")
    if not sessions:
        print("- 현재 저장된 세션이 없습니다.")
        return 0

    for raw in sessions:
        session = dict(raw or {})
        meeting_label = (
            str(session.get("meeting_topic") or "").strip()
            or str(session.get("meeting_number") or "").strip()
            or str(session.get("meeting_id") or "").strip()
            or "미확인 회의"
        )
        print(
            f"- {session.get('session_id') or '(unknown)'}"
            f" | {session.get('status') or 'unknown'}"
            f" | {meeting_label}"
        )
    print()
    print("상세 확인 예시")
    print("-------------")
    print(f"- {_script_cli_command('show-session <session_id>')}")
    return 0


def handle_show_session(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config)
    if not _runtime_api_ready(config):
        print(f"Runtime API is not reachable. Start it first with `{_script_cli_command('start')}`.")
        return 1

    payload = get_runtime_session(config, args.session_id)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    session = dict(payload.get("session") or {})
    _print_session_detail_summary(session)
    return 0


def handle_open_session(args: argparse.Namespace) -> int:
    config = _load_effective_config(args.config)
    if not _runtime_api_ready(config):
        print(f"Runtime API is not reachable. Start it first with `{_script_cli_command('start')}`.")
        return 1

    payload = get_runtime_session(config, args.session_id)
    session = dict(payload.get("session") or {})
    resolved_url = _resolve_session_target_url(session, args.target)
    if not resolved_url:
        print(f"No URL is available for target `{args.target}` in this session.")
        return 1

    print(f"Resolved URL: {resolved_url}")
    if args.print_only:
        return 0

    opened = webbrowser.open(resolved_url)
    if not opened:
        print("브라우저를 자동으로 열지 못했습니다. 위 URL을 직접 여세요.")
        return 1
    print("브라우저를 열었습니다.")
    return 0


def _resolve_session_target_url(session: dict[str, Any], target: str) -> str:
    join_ticket = dict(session.get("join_ticket") or {})
    url_map = {
        "browser_auto": str(join_ticket.get("browser_auto_join_url") or "").strip(),
        "browser": str(join_ticket.get("browser_join_url") or "").strip(),
        "desktop": str(join_ticket.get("desktop_auto_launch_control_url") or "").strip(),
        "config": str(join_ticket.get("meeting_sdk_config_url") or "").strip(),
    }
    return url_map.get(target, "")


def _ensure_runtime_api_ready(
    config: dict[str, Any],
    config_path: Path,
    *,
    no_start: bool,
    wait_seconds: float,
) -> bool:
    if _runtime_api_ready(config):
        return True
    if no_start:
        print(f"Runtime API is not reachable. Start it first with `{_script_cli_command('start')}`.")
        return False

    startup_blockers = _collect_startup_blockers(config)
    if startup_blockers:
        print("Runtime API is not reachable, and auto-start was blocked by config issues.")
        for item in startup_blockers:
            print(f"- {item}")
        print(
            f"Run `{_script_cli_command('quickstart --preset launcher_dm --yes')}` "
            f"or `{_script_cli_command('doctor')}` first."
        )
        return False

    print("Runtime API is not reachable, so the CLI is starting it automatically...")
    if _execution_mode(config) == "launcher":
        status = start_full_launcher(config, config_path=config_path)
    else:
        status = start_runtime(config, config_path=config_path)

    deadline = time.time() + max(wait_seconds, 1.0)
    while time.time() < deadline:
        if _runtime_api_ready(config):
            return True
        time.sleep(0.5)

    print("Start was requested, but the runtime API is still not reachable.")
    print(json.dumps(_decorate_status_payload(config, config_path, status), ensure_ascii=False, indent=2))
    return False


def _load_effective_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file does not exist: {path}")
    return merge_config(build_default_config(), load_config(path))


def _default_support_bundle_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return resolve_workspace_path(f".tmp/zoom-meeting-bot/support-bundle-{timestamp}.json")


def _safe_runtime_status(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    try:
        return _decorate_status_payload(config, config_path, read_runtime_status(config, config_path=config_path))
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _safe_launcher_status(config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    try:
        return _decorate_status_payload(config, config_path, read_full_launcher_status(config, config_path=config_path))
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _runtime_api_ready(config: dict[str, Any]) -> bool:
    try:
        with httpx.Client(timeout=3.0) as client:
            response = client.get(f"http://{runtime_host(config)}:{runtime_port(config)}/health")
            response.raise_for_status()
            payload = response.json()
            return bool(isinstance(payload, dict) and payload.get("ok"))
    except Exception:
        return False


def _sanitize_config_for_display(config: dict[str, Any]) -> dict[str, Any]:
    sanitized = json.loads(json.dumps(config))
    zoom = dict(sanitized.get("zoom") or {})
    local_ai = dict(sanitized.get("local_ai") or {})
    telegram = dict(sanitized.get("telegram") or {})

    zoom["client_secret"] = _mask_secret(str(zoom.get("client_secret") or "").strip())
    local_ai["huggingface_token"] = _mask_secret(str(local_ai.get("huggingface_token") or "").strip())
    telegram["bot_token"] = _mask_secret(str(telegram.get("bot_token") or "").strip())

    sanitized["zoom"] = zoom
    sanitized["local_ai"] = local_ai
    sanitized["telegram"] = telegram
    return sanitized


def _resolved_paths(config: dict[str, Any]) -> dict[str, str]:
    runtime = dict(config.get("runtime") or {})
    launcher = dict(config.get("launcher") or {})
    local_ai = dict(config.get("local_ai") or {})
    skills = dict(config.get("skills") or {})
    whisper_cpp_command = str(local_ai.get("whisper_cpp_command") or "").strip()
    whisper_cpp_model = str(local_ai.get("whisper_cpp_model") or "").strip()
    meeting_output_skill = str(skills.get("meeting_output_path") or "").strip()
    meeting_output_override = str(skills.get("meeting_output_override_path") or "").strip()
    generated_skill_dir = str(skills.get("generated_meeting_output_dir") or "").strip()
    return {
        "store_path": str(resolve_workspace_path(str(runtime.get("store_path") or "data/delegate_sessions.json"))),
        "exports_dir": str(resolve_workspace_path(str(runtime.get("exports_dir") or "data/exports"))),
        "audio_archive_dir": str(resolve_workspace_path(str(runtime.get("audio_archive_dir") or "data/audio"))),
        "meeting_output_skill_path": str(resolve_meeting_output_skill_path(meeting_output_skill or None)),
        "meeting_output_override_path": str(resolve_workspace_path(meeting_output_override))
        if meeting_output_override
        else "",
        "generated_meeting_output_dir": str(resolve_generated_meeting_output_dir(generated_skill_dir or None)),
        "runtime_state_path": str(runtime_state_path(config)),
        "launcher_state_path": str(
            resolve_workspace_path(str(launcher.get("state_path") or ".tmp/zoom-meeting-bot/launcher-state.json"))
        ),
        "whisper_cpp_command": _display_optional_path(whisper_cpp_command),
        "whisper_cpp_model": _display_optional_path(whisper_cpp_model),
    }


def _mask_secret(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= 8:
        return "*" * len(text)
    return text[:4] + ("*" * (len(text) - 8)) + text[-4:]


def _mask_visible_tail(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "(미설정)"
    if len(text) <= 6:
        return text[:1] + ("*" * max(len(text) - 1, 0))
    return text[:3] + ("*" * (len(text) - 6)) + text[-3:]


def _mask_presence(value: str) -> str:
    return "configured" if str(value or "").strip() else "(미설정)"


def _looks_like_zoom_join_url(join_url: str) -> bool:
    if not join_url:
        return False
    parsed = urlsplit(join_url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _extract_zoom_meeting_number(join_url: str) -> str:
    if not join_url:
        return ""
    parsed = urlsplit(join_url)
    patterns = (
        r"/j/(\d+)",
        r"/wc/join/(\d+)",
        r"/join/(\d+)",
    )
    for pattern in patterns:
        match = re.search(pattern, parsed.path)
        if match:
            return match.group(1)
    return ""


def _print_created_session_summary(payload: dict[str, Any]) -> None:
    session = dict(payload.get("session") or {})
    join_ticket = dict(session.get("join_ticket") or {})
    preflight = dict(session.get("preflight") or {})

    session_id = str(session.get("session_id") or "").strip() or "(unknown)"
    meeting_label = (
        str(session.get("meeting_topic") or "").strip()
        or str(session.get("meeting_number") or "").strip()
        or str(session.get("meeting_id") or "").strip()
        or "미확인 회의"
    )
    readiness = str(preflight.get("readiness") or "").strip() or "unknown"
    browser_ready = bool(join_ticket.get("can_join_in_browser"))
    desktop_ready = bool(join_ticket.get("can_join_in_zoom_workplace"))
    browser_join_url = str(join_ticket.get("browser_join_url") or "").strip()
    browser_auto_join_url = str(join_ticket.get("browser_auto_join_url") or "").strip()
    desktop_control_url = str(join_ticket.get("desktop_auto_launch_control_url") or "").strip()
    config_url = str(join_ticket.get("meeting_sdk_config_url") or "").strip()

    print("세션 생성 완료")
    print("---------------")
    print(f"- session_id: {session_id}")
    print(f"- meeting: {meeting_label}")
    print(f"- status: {session.get('status') or 'planned'}")
    print(f"- preflight: {readiness}")
    print(f"- browser join ready: {'yes' if browser_ready else 'no'}")
    print(f"- Zoom Workplace ready: {'yes' if desktop_ready else 'no'}")

    if browser_auto_join_url:
        print(f"- browser auto join: {browser_auto_join_url}")
    elif browser_join_url:
        print(f"- browser join page: {browser_join_url}")

    if desktop_control_url:
        print(f"- desktop control page: {desktop_control_url}")

    if config_url:
        print(f"- meeting sdk config: {config_url}")

    print()
    print("다음 단계")
    print("---------")
    if browser_auto_join_url:
        print("- 브라우저 기반으로 들어가려면 위 browser auto join 주소를 여세요.")
    elif browser_join_url:
        print("- join page를 열고, 거기서 브라우저 또는 Zoom Workplace 입장을 진행하세요.")
    elif desktop_control_url:
        print("- desktop control page를 열고 Zoom Workplace 입장을 진행하세요.")
    else:
        print(f"- `{_script_cli_command('status')}`로 런타임 상태를 확인한 뒤 join ticket을 다시 확인하세요.")

    if readiness == "blocked":
        print(f"- preflight가 blocked 상태이므로 `{_script_cli_command('doctor')}`와 설정값을 먼저 다시 확인하세요.")
    else:
        print(f"- 상세 내용은 `{_script_cli_command(f'show-session {session_id}')}`로 다시 확인할 수 있습니다.")


def _print_session_detail_summary(session: dict[str, Any]) -> None:
    join_ticket = dict(session.get("join_ticket") or {})
    preflight = dict(session.get("preflight") or {})
    summary_exports = list(session.get("summary_exports") or [])
    transcript_exports = list(session.get("transcript_exports") or [])

    meeting_label = (
        str(session.get("meeting_topic") or "").strip()
        or str(session.get("meeting_number") or "").strip()
        or str(session.get("meeting_id") or "").strip()
        or "미확인 회의"
    )

    print("세션 상세")
    print("---------")
    print(f"- session_id: {session.get('session_id') or '(unknown)'}")
    print(f"- meeting: {meeting_label}")
    print(f"- status: {session.get('status') or 'unknown'}")
    print(f"- delegate_mode: {session.get('delegate_mode') or 'unknown'}")
    print(f"- requested_by: {session.get('requested_by') or '-'}")
    print(f"- preflight: {preflight.get('readiness') or 'unknown'}")
    print(f"- browser join ready: {'yes' if bool(join_ticket.get('can_join_in_browser')) else 'no'}")
    print(f"- Zoom Workplace ready: {'yes' if bool(join_ticket.get('can_join_in_zoom_workplace')) else 'no'}")

    browser_auto_join_url = str(join_ticket.get("browser_auto_join_url") or "").strip()
    browser_join_url = str(join_ticket.get("browser_join_url") or "").strip()
    desktop_control_url = str(join_ticket.get("desktop_auto_launch_control_url") or "").strip()
    if browser_auto_join_url:
        print(f"- browser auto join: {browser_auto_join_url}")
    elif browser_join_url:
        print(f"- browser join page: {browser_join_url}")
    if desktop_control_url:
        print(f"- desktop control page: {desktop_control_url}")

    if summary_exports:
        print("- summary exports:")
        for item in summary_exports:
            export = dict(item or {})
            print(f"  - {export.get('path') or export.get('file_path') or '(path missing)'}")
    if transcript_exports:
        print("- transcript exports:")
        for item in transcript_exports:
            export = dict(item or {})
            print(f"  - {export.get('path') or export.get('file_path') or '(path missing)'}")


def _collect_interactive_config(config: dict[str, Any]) -> dict[str, Any]:
    updated = build_default_config()
    updated = merge_config(updated, config)

    print()
    print("ZOOM_MEETING_BOT 설정을 시작합니다.")
    print("Enter를 누르면 현재 값이 유지됩니다.")

    _print_section("1. 기본 프로필")
    updated["profile"]["bot_name"] = _prompt_required("봇 이름", updated["profile"]["bot_name"])
    suggested_workspace = updated["profile"]["workspace_name"] or suggest_workspace_name(updated["profile"]["bot_name"])
    updated["profile"]["workspace_name"] = _prompt(
        "작업 공간 이름",
        suggested_workspace,
        help_text="예: team-alpha, lush-korea, my-bot",
    )

    _print_section("2. Zoom 앱 정보")
    print("General App > Meeting SDK > programmatic join use case 기준 값을 입력합니다.")
    updated["zoom"]["client_id"] = _prompt_required(
        "Meeting SDK Client ID",
        updated["zoom"]["client_id"],
    )
    updated["zoom"]["client_secret"] = _prompt_secret(
        "Meeting SDK Client Secret",
        updated["zoom"]["client_secret"],
        help_text="Zoom App 화면 왼쪽 상단 App Credentials의 Client Secret 값을 한 번만 그대로 붙여넣으세요.",
    )

    _print_section("3. 로컬 AI 본체")
    updated["local_ai"]["huggingface_token"] = _prompt_secret(
        "Hugging Face token",
        updated["local_ai"]["huggingface_token"],
        help_text="pyannote diarization 모델 접근용입니다.",
    )
    updated["local_ai"]["meeting_output_device"] = _prompt_required(
        "회의 출력 장치 이름",
        updated["local_ai"]["meeting_output_device"],
        help_text="예: Realtek HD Audio 2nd output(Realtek(R) Audio)",
    )
    updated["local_ai"]["codex_command"] = _prompt("Codex 명령어", updated["local_ai"]["codex_command"])
    updated["local_ai"]["pandoc_command"] = _prompt(
        "pandoc 명령어 또는 절대 경로",
        updated["local_ai"]["pandoc_command"],
    )
    updated["local_ai"]["libreoffice_command"] = _prompt(
        "LibreOffice(soffice) 명령어 또는 절대 경로",
        updated["local_ai"]["libreoffice_command"],
    )
    updated["local_ai"]["whisper_cpp_command"] = _prompt(
        "whisper.cpp CLI 명령어 또는 절대 경로",
        str(updated["local_ai"].get("whisper_cpp_command") or ""),
        help_text="원본 품질 fallback을 맞추려면 whisper-cli.exe 경로를 넣는 편이 좋습니다.",
    )
    updated["local_ai"]["whisper_cpp_model"] = _prompt(
        "whisper.cpp 모델(.bin) 경로",
        str(updated["local_ai"].get("whisper_cpp_model") or ""),
        help_text="예: ggml-large-v3-turbo-q5_0.bin",
    )

    _print_section("4. 런타임")
    updated["runtime"]["execution_mode"] = _prompt_described_choice(
        "실행 모드",
        str(updated["runtime"]["execution_mode"]),
        EXECUTION_MODE_CHOICES,
    )
    updated["runtime"]["host"] = _prompt("런타임 host", str(updated["runtime"]["host"]))
    updated["runtime"]["port"] = _prompt_int("런타임 port", int(updated["runtime"]["port"]))
    updated["runtime"]["audio_mode"] = _prompt_choice(
        "오디오 모드",
        str(updated["runtime"]["audio_mode"]),
        ("conversation", "mixed", "microphone", "system"),
    )
    updated["runtime"]["completion_mode"] = _prompt_described_choice(
        "session completion mode",
        str(updated["runtime"].get("completion_mode") or "inline"),
        COMPLETION_MODE_CHOICES,
    )
    if str(updated["runtime"]["execution_mode"]) == "launcher":
        _print_section("5. Launcher")
        print("launcher 모드는 Zoom 런타임과 artifact 전달 계층을 함께 실행합니다.")
        updated["launcher"]["telegram_runner_backend"] = _prompt_described_choice(
            "Telegram runner backend",
            str(updated["launcher"]["telegram_runner_backend"]),
            RUNNER_BACKEND_CHOICES,
        )
        if str(updated["launcher"]["telegram_runner_backend"]) == "metheus_cli":
            updated["launcher"]["metheus_route_name"] = _prompt_required(
                "Metheus route name",
                str(updated["launcher"]["metheus_route_name"]),
                help_text="예: telegram-monitor-my-bot",
            )
        else:
            updated["launcher"]["metheus_route_name"] = ""

    _print_section("6. Telegram 연동")
    telegram_enabled = _prompt_bool("Telegram 연동을 사용할까요", bool(updated["telegram"]["enabled"]))
    updated["telegram"]["enabled"] = telegram_enabled
    if telegram_enabled:
        updated["telegram"]["bot_name"] = _prompt_required("Telegram bot 이름", updated["telegram"]["bot_name"])
        updated["telegram"]["bot_token"] = _prompt_secret("Telegram bot token", updated["telegram"]["bot_token"])
        conversation_modes = _supported_conversation_route_modes(updated)
        artifact_modes = _supported_artifact_route_modes(updated)
        _print_route_capability_note(
            updated,
            conversation_modes=conversation_modes,
            artifact_modes=artifact_modes,
        )
        updated["telegram"]["conversation_route"] = _configure_route(
            "대화/멘션 응답 route",
            dict(updated["telegram"]["conversation_route"] or {}),
            allowed_modes=conversation_modes,
        )
        updated["telegram"]["artifact_route"] = _configure_route(
            "PDF artifact 전달 route",
            dict(updated["telegram"]["artifact_route"] or {}),
            allowed_modes=artifact_modes,
        )
    else:
        updated["telegram"]["conversation_route"] = {
            "mode": "none",
            "project_id": "",
            "destination_label": "",
            "chat_id": "",
        }
        updated["telegram"]["artifact_route"] = {
            "mode": "none",
            "project_id": "",
            "destination_label": "",
            "chat_id": "",
        }

    _print_config_summary(updated)
    return updated


def _prompt(label: str, current: str, *, help_text: str = "") -> str:
    if help_text:
        print(f"   {help_text}")
    suffix = f" [{current}]" if current else ""
    value = input(f"{label}{suffix}: ")
    cleaned = sanitize_text_input(value)
    if value and cleaned != value.strip():
        print("   Hidden control characters were removed from the value.")
    current_clean = sanitize_text_input(current)
    return cleaned or current_clean


def _prompt_required(label: str, current: str, *, help_text: str = "") -> str:
    while True:
        value = _prompt(label, current, help_text=help_text).strip()
        if value:
            return value
        print("   빈 값으로 둘 수 없습니다.")


def _prompt_secret(label: str, current: str, *, help_text: str = "") -> str:
    if help_text:
        print(f"   {help_text}")
    suffix = " [configured]" if current else ""
    value = input(f"{label}{suffix}: ")
    cleaned = sanitize_text_input(value)
    if value and cleaned != value.strip():
        print("   Hidden control characters were removed from the value.")
    current_clean = sanitize_text_input(current)
    return cleaned or current_clean


def _prompt_bool(label: str, current: bool) -> bool:
    current_marker = "Y/n" if current else "y/N"
    value = input(f"{label} [{current_marker}]: ").strip().lower()
    if not value:
        return current
    return value in {"y", "yes"}


def _prompt_choice(label: str, current: str, choices: tuple[str, ...]) -> str:
    joined = "/".join(choices)
    while True:
        value = input(f"{label} ({joined}) [{current}]: ").strip()
        if not value:
            return current
        if value in choices:
            return value
        print(f"Choose one of: {joined}")


def _prompt_described_choice(label: str, current: str, choices: list[tuple[str, str, str]]) -> str:
    print(f"현재 선택: {_choice_label(current, choices)}")
    for index, (value, display, description) in enumerate(choices, start=1):
        marker = " (현재)" if value == current else ""
        print(f"  [{index}] {display}{marker}")
        print(f"      {description}")
    while True:
        choice = input(f"{label} 번호를 선택하세요 [Enter={_choice_label(current, choices)}]: ").strip()
        if not choice:
            return current
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(choices):
                return choices[idx - 1][0]
        print("   올바른 번호를 선택해 주세요.")


def _prompt_int(label: str, current: int) -> int:
    while True:
        value = input(f"{label} [{current}]: ").strip()
        if not value:
            return current
        try:
            return int(value)
        except ValueError:
            print("Enter a valid integer.")


def _print_section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _configure_route(
    title: str,
    current: dict[str, Any],
    *,
    allowed_modes: tuple[str, ...] | list[str],
) -> dict[str, Any]:
    print()
    print(title)
    normalized_modes = tuple(_normalize_route_mode(item) for item in allowed_modes) or ("none",)
    current_mode = _normalize_route_mode(str(current.get("mode") or "none"))
    if current_mode not in normalized_modes:
        current_mode = normalized_modes[0]

    if normalized_modes == ("none",):
        print("   현재 설정 조합에서는 이 route를 사용할 수 없습니다.")
        return {
            "mode": "none",
            "project_id": "",
            "destination_label": "",
            "chat_id": "",
        }

    mode = _prompt_route_mode(current_mode, allowed_modes=normalized_modes)
    route = {
        "mode": mode,
        "project_id": "",
        "destination_label": "",
        "chat_id": "",
    }
    if mode == "metheus_project":
        route["project_id"] = _prompt_required("메테우스 project_id", str(current.get("project_id") or ""))
        route["destination_label"] = _prompt(
            "메테우스 destination_label",
            str(current.get("destination_label") or ""),
            help_text="예: Lush Korea CLI",
        )
    elif mode in {"personal_dm", "telegram_chat"}:
        route["chat_id"] = _prompt_required("Telegram chat_id", str(current.get("chat_id") or ""))
    return route


def _prompt_route_mode(current: str, *, allowed_modes: tuple[str, ...] | list[str]) -> str:
    current = _normalize_route_mode(current)
    route_choices = _route_choices(allowed_modes)
    allowed_values = {value for value, _label, _description in route_choices}
    if current not in allowed_values:
        current = route_choices[0][0]
    current_label = _route_label(current)
    print(f"현재 route: {current_label}")
    for index, (value, label, description) in enumerate(route_choices, start=1):
        marker = " (현재)" if value == current else ""
        print(f"  [{index}] {label}{marker}")
        print(f"      {description}")
    while True:
        choice = input(f"번호를 선택하세요 [Enter={current_label}]: ").strip()
        if not choice:
            return current
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(route_choices):
                return route_choices[idx - 1][0]
        print("   올바른 번호를 선택해 주세요.")


def _route_label(mode: str) -> str:
    normalized = _normalize_route_mode(mode)
    for value, label, _description in ROUTE_MODE_CHOICES:
        if value == normalized:
            return label
    return normalized


def _normalize_route_mode(mode: str) -> str:
    mode = str(mode or "").strip()
    if mode == "project_channel":
        return "metheus_project"
    return mode or "none"


def _route_choices(allowed_modes: tuple[str, ...] | list[str]) -> list[tuple[str, str, str]]:
    allowed = {_normalize_route_mode(item) for item in allowed_modes}
    return [choice for choice in ROUTE_MODE_CHOICES if choice[0] in allowed]


def _supported_conversation_route_modes(
    config: dict[str, Any],
    *,
    execution_mode: str | None = None,
) -> tuple[str, ...]:
    mode = execution_mode or _execution_mode(config)
    if mode != "launcher":
        return ("none",)
    launcher = dict(config.get("launcher") or {})
    backend = str(launcher.get("telegram_runner_backend") or "none").strip() or "none"
    if backend == "metheus_cli":
        return ("none", "metheus_project")
    return ("none",)


def _supported_artifact_route_modes(
    config: dict[str, Any],
    *,
    execution_mode: str | None = None,
) -> tuple[str, ...]:
    mode = execution_mode or _execution_mode(config)
    if mode != "launcher":
        return ("none",)
    return ("none", "metheus_project", "personal_dm", "telegram_chat")


def _describe_supported_route_modes(modes: tuple[str, ...] | list[str]) -> str:
    return ", ".join(_route_label(mode) for mode in modes)


def _print_route_capability_note(
    config: dict[str, Any],
    *,
    conversation_modes: tuple[str, ...] | list[str],
    artifact_modes: tuple[str, ...] | list[str],
) -> None:
    print(f"   대화 route 지원: {_describe_supported_route_modes(conversation_modes)}")
    print(f"   PDF route 지원: {_describe_supported_route_modes(artifact_modes)}")
    if _execution_mode(config) != "launcher":
        print("   runtime_only 모드에서는 Telegram 대화/전달 route가 비활성입니다. launcher 모드에서만 실제 전송이 됩니다.")
        return
    launcher = dict(config.get("launcher") or {})
    backend = str(launcher.get("telegram_runner_backend") or "none").strip() or "none"
    if backend == "none":
        print("   현재 launcher는 Telegram conversation runner를 켜지 않으므로 대화 route는 비활성이고, PDF artifact route만 사용할 수 있습니다.")
    elif backend == "metheus_cli":
        print("   현재 launcher는 Metheus Telegram runner를 사용하므로 대화 route는 메테우스 프로젝트 또는 비활성만 지원합니다.")


def _execution_mode(config: dict[str, Any]) -> str:
    runtime = dict(config.get("runtime") or {})
    mode = str(runtime.get("execution_mode") or "runtime_only").strip()
    return mode or "runtime_only"


def _completion_mode(config: dict[str, Any]) -> str:
    runtime = dict(config.get("runtime") or {})
    mode = str(runtime.get("completion_mode") or "inline").strip()
    return mode or "inline"


def _decorate_status_payload(config: dict[str, Any], config_path: Path, payload: dict[str, Any]) -> dict[str, Any]:
    execution_mode = _execution_mode(config)
    telegram = dict(config.get("telegram") or {})
    launcher = dict(config.get("launcher") or {})
    decorated = dict(payload)
    decorated["config_path"] = str(config_path.resolve())
    decorated["execution_mode"] = execution_mode
    decorated["execution_mode_label"] = _choice_label(execution_mode, EXECUTION_MODE_CHOICES)
    decorated["completion_mode"] = _completion_mode(config)
    decorated["mode_summary"] = _mode_summary(config)
    decorated["telegram"] = {
        "enabled": bool(telegram.get("enabled")),
        "bot_name": str(telegram.get("bot_name") or "").strip(),
        "conversation_route": _describe_route(dict(telegram.get("conversation_route") or {})),
        "artifact_route": _describe_route(dict(telegram.get("artifact_route") or {})),
        "supported_conversation_routes": list(_supported_conversation_route_modes(config, execution_mode=execution_mode)),
        "supported_artifact_routes": list(_supported_artifact_route_modes(config, execution_mode=execution_mode)),
    }
    if execution_mode == "launcher":
        backend = str(launcher.get("telegram_runner_backend") or "none").strip() or "none"
        decorated["launcher"] = {
            "backend": backend,
            "backend_label": _choice_label(backend, RUNNER_BACKEND_CHOICES),
            "route_name": str(launcher.get("metheus_route_name") or "").strip(),
        }
    return decorated


def _print_config_summary(config: dict[str, Any]) -> None:
    profile = dict(config.get("profile") or {})
    telegram = dict(config.get("telegram") or {})
    print()
    print("설정 요약")
    print("---------")
    print(f"- bot 이름: {profile.get('bot_name') or '(미설정)'}")
    print(f"- 작업 공간: {profile.get('workspace_name') or '(미설정)'}")
    print(f"- 실행 모드: {_mode_summary(config)}")
    print(f"- Telegram 연동: {'사용' if bool(telegram.get('enabled')) else '사용 안 함'}")
    if bool(telegram.get("enabled")):
        print(f"- 대화 route: {_describe_route(dict(telegram.get('conversation_route') or {}))}")
        print(f"- PDF route: {_describe_route(dict(telegram.get('artifact_route') or {}))}")
        print(f"- 대화 route 지원: {_describe_supported_route_modes(_supported_conversation_route_modes(config))}")
        print(f"- PDF route 지원: {_describe_supported_route_modes(_supported_artifact_route_modes(config))}")


def _mode_summary(config: dict[str, Any]) -> str:
    execution_mode = _execution_mode(config)
    if execution_mode != "launcher":
        return "runtime_only (Zoom 런타임만 실행)"
    launcher = dict(config.get("launcher") or {})
    backend = str(launcher.get("telegram_runner_backend") or "none").strip() or "none"
    if backend == "metheus_cli":
        return "launcher (Zoom 런타임 + artifact 전달 + Metheus Telegram runner)"
    return "launcher (Zoom 런타임 + artifact 전달)"


def _doctor_mode(args: argparse.Namespace, config: dict[str, Any]) -> str:
    requested = str(getattr(args, "mode", "current") or "current").strip()
    if requested == "current":
        return _execution_mode(config)
    return requested


def _print_readiness_summary(
    config: dict[str, Any],
    *,
    requested_mode: str,
    runtime_ready: bool,
    launcher_ready: bool,
) -> None:
    print()
    print("Readiness")
    print("---------")
    print(f"- current config mode: {_execution_mode(config)}")
    print(f"- requested check mode: {requested_mode}")
    print(f"- runtime_only ready: {'yes' if runtime_ready else 'no'}")
    print(f"- launcher ready: {'yes' if launcher_ready else 'no'}")


def _recommended_next_steps(
    config: dict[str, Any],
    *,
    requested_mode: str,
    blocking_problems: list[str],
) -> list[str]:
    steps: list[str] = []
    problem_text = " ".join(blocking_problems)

    if "Missing executable" in problem_text:
        steps.append(_script_cli_command("setup"))
    if "Missing required value" in problem_text:
        steps.append(_script_cli_command("configure"))
    if "launcher.metheus_route_name" in problem_text:
        steps.append(_script_cli_command("configure"))
    if "telegram." in problem_text and requested_mode == "launcher":
        steps.append(_script_cli_command("configure"))
    if "Unsupported route combination" in problem_text:
        steps.append(_script_cli_command("configure"))

    if not blocking_problems:
        if requested_mode in {"runtime_only", "launcher"}:
            steps.append(_script_cli_command('create-session "https://us06web.zoom.us/j/..." --passcode "123456"'))
        steps.append(_script_cli_command("status"))

    # remove duplicates while preserving order
    unique_steps: list[str] = []
    for step in steps:
        if step not in unique_steps:
            unique_steps.append(step)
    return unique_steps


def _print_next_steps(problems: list[str], warnings: list[str], steps: list[str]) -> None:
    if not steps:
        return
    print()
    print("Next steps")
    print("----------")
    if problems:
        print("- blocking issues exist, so start with the commands below:")
    elif warnings:
        print("- no blocking issues, but these commands are a good next checkpoint:")
    else:
        print("- this config looks usable. Recommended next commands:")
    for step in steps:
        print(f"- {step}")


def _script_cli_command(args: str) -> str:
    prefix = ".\\scripts\\zoom-meeting-bot.ps1" if current_platform_id() == "windows" else "./scripts/zoom-meeting-bot.sh"
    suffix = str(args or "").strip()
    return f"{prefix} {suffix}".strip()


def _describe_route(route: dict[str, Any]) -> str:
    mode = _normalize_route_mode(str(route.get("mode") or "none"))
    if mode == "metheus_project":
        project_id = str(route.get("project_id") or "").strip() or "(project_id 미설정)"
        label = str(route.get("destination_label") or "").strip()
        return f"메테우스 프로젝트 ({project_id}{', ' + label if label else ''})"
    if mode == "personal_dm":
        return f"개인 1:1 DM ({str(route.get('chat_id') or '').strip() or 'chat_id 미설정'})"
    if mode == "telegram_chat":
        return f"일반 Telegram chat ({str(route.get('chat_id') or '').strip() or 'chat_id 미설정'})"
    return "사용 안 함"


def _choice_label(value: str, choices: list[tuple[str, str, str]]) -> str:
    for choice_value, label, _description in choices:
        if choice_value == value:
            return label
    return value


def _report(name: str, status: str, message: str) -> None:
    print(f"- {name}: {status} | {message}")


def _check_required(
    config: dict[str, Any],
    path: tuple[str, str],
    label: str,
    bucket: list[str],
    *,
    severity: str = "problem",
) -> None:
    section, key = path
    value = str(config.get(section, {}).get(key, "")).strip()
    if value:
        _report(label, "ok", "Configured.")
    else:
        if severity == "warning":
            bucket.append(f"Missing recommended value: {label}")
            _report(label, "warning", "Value is empty.")
            return
        bucket.append(f"Missing required value: {label}")
        _report(label, "missing", "Value is empty.")


def _check_zoom_credentials(config: dict[str, Any], problems: list[str], warnings: list[str]) -> None:
    zoom = dict(config.get("zoom") or {})
    client_id = str(zoom.get("client_id") or "").strip()
    client_secret = str(zoom.get("client_secret") or "").strip()
    if client_id and client_secret:
        _report("zoom.meeting_sdk_credentials", "ok", "Meeting SDK credentials are present.")
    if not client_secret:
        return
    if len(client_secret) > 64:
        problems.append(
            "zoom.client_secret looks unusually long. Check that you pasted the Meeting SDK Client Secret only once."
        )
        _report(
            "zoom.client_secret",
            "error",
            "Value looks unusually long. This often means the secret was pasted multiple times.",
        )
        return
    repeated_unit = _detect_repeated_prefix(client_secret)
    if repeated_unit:
        problems.append(
            "zoom.client_secret appears to contain a repeated pattern. Check that you pasted the Meeting SDK Client Secret only once."
        )
        _report(
            "zoom.client_secret",
            "error",
            f"Value appears to repeat the same prefix (`{repeated_unit}`...).",
        )


def _detect_repeated_prefix(value: str) -> str:
    text = str(value or "").strip()
    if len(text) < 32:
        return ""
    for unit_length in range(16, min(65, len(text) // 2 + 1)):
        unit = text[:unit_length]
        if text.startswith(unit * 2):
            return unit
    return ""


def _check_command(command_name: str, label: str, bucket: list[str]) -> None:
    candidates = _command_candidates_for_check(command_name, label)
    found, resolved = command_candidates_exist(candidates)
    if found:
        _report(label, "ok", f"Found `{resolved}`.")
        return
    bucket.append(f"Missing executable: {command_name}")
    _report(label, "missing", f"`{command_name}` was not found.")


def _command_candidates_for_check(command_name: str, label: str) -> tuple[str, ...]:
    candidates: list[str] = []
    configured = str(command_name or "").strip()
    if configured:
        candidates.append(configured)
    step_name = ""
    if label == "pandoc":
        step_name = "pandoc"
    elif label == "libreoffice/soffice":
        step_name = "libreoffice"
    elif label == "ffmpeg":
        step_name = "ffmpeg"
    if step_name:
        for plan in tool_install_plans():
            if plan.step_name == step_name:
                candidates.extend(plan.command_candidates)
                break
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate or "").strip().casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(str(candidate).strip())
    return tuple(deduped)


def _check_local_quality_dependencies(config: dict[str, Any], problems: list[str], warnings: list[str]) -> None:
    runtime = dict(config.get("runtime") or {})
    local_ai = dict(config.get("local_ai") or {})
    audio_mode = str(runtime.get("audio_mode") or "conversation").strip() or "conversation"
    platform_id = current_platform_id()
    gpu_detected, gpu_detail = _detect_cuda_gpu()
    torch_runtime = inspect_torch_runtime()

    _check_package_version(
        "starlette",
        expected_version="0.52.1",
        bucket=problems,
        required=True,
        mismatch_message=f"The runtime engine expects Starlette 0.52.1. Re-run `{_script_cli_command('setup')}` or reinstall the kit dependencies.",
    )
    _check_package_version(
        "uvicorn",
        expected_version="0.42.0",
        bucket=problems,
        required=True,
        mismatch_message=f"The runtime engine expects Uvicorn 0.42.0. Re-run `{_script_cli_command('setup')}` or reinstall the kit dependencies.",
    )

    if gpu_detected:
        _report("cuda_gpu", "ok", f"Detected CUDA-capable GPU via {gpu_detail}.")
    else:
        if platform_id == MACOS:
            _report(
                "cuda_gpu",
                "warning",
                "CUDA-capable GPU was not detected. macOS will use the CPU final-offline transcription path when available.",
            )
        else:
            warnings.append("CUDA-capable GPU was not detected. The kit may need a fallback transcription path.")
            _report("cuda_gpu", "warning", "CUDA-capable GPU was not detected.")

    if bool(torch_runtime.get("cuda_enabled")):
        _report("torch.cuda runtime", "ok", str(torch_runtime.get("detail") or "CUDA-enabled torch runtime is ready."))
    elif gpu_detected:
        message = (
            "CUDA-capable GPU is present, but the installed torch runtime cannot use CUDA. "
            f"Re-run `{_script_cli_command('setup --yes')}` so quickstart/setup can install the CUDA torch runtime."
        )
        problems.append(message)
        _report("torch.cuda runtime", "missing", str(torch_runtime.get("detail") or message))
    elif bool(torch_runtime.get("installed")):
        _report("torch.cuda runtime", "warning", str(torch_runtime.get("detail") or "torch is installed without CUDA."))
    else:
        warnings.append("torch is not installed yet, so CUDA quality readiness could not be verified.")
        _report("torch.cuda runtime", "warning", str(torch_runtime.get("detail") or "torch is not installed."))

    if audio_mode in {"conversation", "mixed", "system"}:
        _check_python_module("soundcard", "soundcard", problems, required=True)
        _check_python_module("soundfile", "soundfile", problems, required=True)
    else:
        _check_python_module("soundcard", "soundcard", warnings, required=False)
        _check_python_module("soundfile", "soundfile", warnings, required=False)

    _check_python_module("faster_whisper", "faster-whisper", problems, required=True)
    _check_python_module("pyannote.audio", "pyannote.audio", problems, required=True)
    whisper_cpp_command = str(local_ai.get("whisper_cpp_command") or "").strip()
    whisper_cpp_model = str(local_ai.get("whisper_cpp_model") or "").strip()
    discovered_whisper_cpp_command = find_whisper_cpp_cli() if not whisper_cpp_command else None
    discovered_whisper_cpp_model = find_whisper_cpp_model(DEFAULT_WHISPER_CPP_MODEL_NAME) if not whisper_cpp_model else None
    _check_optional_path(
        whisper_cpp_command,
        "whisper.cpp command",
        warnings,
        missing_message="whisper.cpp CLI path is not configured. Short live-audio fallback quality may differ from the golden reference.",
        expect_file=True,
        discovered_path=discovered_whisper_cpp_command,
    )
    _check_optional_path(
        whisper_cpp_model,
        "whisper.cpp model",
        warnings,
        missing_message="whisper.cpp model path is not configured. Short live-audio fallback quality may differ from the golden reference.",
        expect_file=True,
        discovered_path=discovered_whisper_cpp_model,
    )
    if platform_id == MACOS and audio_mode in {"conversation", "mixed", "system"}:
        from local_meeting_ai_runtime.local_observer import LocalObserver

        observer_readiness = LocalObserver().audio_quality_readiness()
        microphone_ready = bool(observer_readiness.get("microphone_device_ready"))
        meeting_output_ready = bool(observer_readiness.get("meeting_output_device_ready"))
        if microphone_ready:
            _report("observer.microphone_device", "ok", "Configured/default microphone device is available.")
        else:
            for reason in observer_readiness.get("blocking_reasons") or []:
                if "microphone" in str(reason).lower() and reason not in problems:
                    problems.append(str(reason))
            _report("observer.microphone_device", "missing", "Configured/default microphone device is not ready.")
        if meeting_output_ready:
            _report("observer.meeting_output_device", "ok", "Configured meeting output device is available.")
        else:
            for reason in observer_readiness.get("blocking_reasons") or []:
                if "meeting output" in str(reason).lower() and reason not in problems:
                    problems.append(str(reason))
            _report("observer.meeting_output_device", "missing", "Configured meeting output device is not ready.")
        for note in observer_readiness.get("quality_notes") or []:
            note_text = str(note).strip()
            if note_text and note_text not in warnings:
                warnings.append(note_text)


def _check_python_module(module_name: str, label: str, bucket: list[str], *, required: bool) -> None:
    try:
        found = importlib.util.find_spec(module_name) is not None
    except ModuleNotFoundError:
        found = False
    except Exception:
        found = False

    if found:
        _report(label, "ok", "Python module is installed.")
        return

    message = f"Missing Python module: {label}"
    if required:
        bucket.append(message)
        _report(label, "missing", "Python module is not installed.")
    else:
        bucket.append(message)
        _report(label, "warning", "Python module is not installed.")


def _check_package_version(
    package_name: str,
    *,
    expected_version: str,
    bucket: list[str],
    required: bool,
    mismatch_message: str,
) -> None:
    try:
        version = importlib.metadata.version(package_name)
    except importlib.metadata.PackageNotFoundError:
        message = f"Missing Python package: {package_name}"
        if required:
            bucket.append(message)
            _report(package_name, "missing", "Python package is not installed.")
        else:
            bucket.append(message)
            _report(package_name, "warning", "Python package is not installed.")
        return
    except Exception:
        bucket.append(f"Could not inspect Python package version: {package_name}")
        _report(package_name, "warning", "Could not inspect installed version.")
        return

    if version == expected_version:
        _report(package_name, "ok", f"Version {version} is installed.")
        return

    bucket.append(f"{package_name} version mismatch: expected {expected_version}, found {version}. {mismatch_message}")
    _report(package_name, "error", f"Expected {expected_version}, found {version}.")


def _check_optional_path(
    configured_path: str,
    label: str,
    warnings: list[str],
    *,
    missing_message: str,
    expect_file: bool,
    discovered_path: Path | None = None,
) -> None:
    path_text = str(configured_path or "").strip()
    if not path_text:
        if discovered_path is not None and (discovered_path.is_file() if expect_file else discovered_path.exists()):
            _report(label, "ok", f"Found `{discovered_path}` automatically.")
            return
        warnings.append(missing_message)
        _report(label, "warning", missing_message)
        return
    path = _resolve_optional_path_for_check(path_text)
    exists = bool(path and (path.is_file() if expect_file else path.exists()))
    if exists:
        _report(label, "ok", f"Found `{path}`.")
        return
    warnings.append(f"{label} was not found: {path_text}")
    _report(label, "warning", f"`{path_text}` was not found.")


def _resolve_optional_path_for_check(value: str) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_absolute():
        return path.resolve()
    if "/" in text or "\\" in text:
        return resolve_relative_path(text)
    resolved = shutil.which(text)
    if resolved:
        return Path(resolved).resolve()
    return None


def _display_optional_path(value: str) -> str:
    resolved = _resolve_optional_path_for_check(value)
    if resolved is not None:
        return str(resolved)
    return str(value or "").strip()


def _detect_cuda_gpu() -> tuple[bool, str]:
    return detect_cuda_gpu()


def _check_telegram_config(config: dict[str, Any], problems: list[str], warnings: list[str]) -> None:
    telegram = dict(config.get("telegram") or {})
    if not bool(telegram.get("enabled")):
        _report("telegram", "ok", "Telegram integration is disabled.")
        return

    bot_name = str(telegram.get("bot_name") or "").strip()
    bot_token = str(telegram.get("bot_token") or "").strip()
    if bot_name:
        _report("telegram.bot_name", "ok", "Configured.")
    else:
        problems.append("Missing required value: telegram.bot_name")
        _report("telegram.bot_name", "missing", "Value is empty.")

    if bot_token:
        _report("telegram.bot_token", "ok", "Configured.")
    else:
        problems.append("Missing required value: telegram.bot_token")
        _report("telegram.bot_token", "missing", "Value is empty.")

    for route_name in ("conversation_route", "artifact_route"):
        route = dict(telegram.get(route_name) or {})
        mode = _normalize_route_mode(str(route.get("mode") or "none"))
        if mode == "none":
            _report(f"telegram.{route_name}", "ok", "Route disabled.")
            continue
        if mode == "metheus_project":
            project_id = str(route.get("project_id") or "").strip()
            destination_label = str(route.get("destination_label") or "").strip()
            if not project_id:
                problems.append(f"Missing required value: telegram.{route_name}.project_id")
                _report(f"telegram.{route_name}.project_id", "missing", "Value is empty.")
            else:
                _report(f"telegram.{route_name}.project_id", "ok", "Configured.")
            if not destination_label:
                warnings.append(f"Missing recommended value: telegram.{route_name}.destination_label")
                _report(f"telegram.{route_name}.destination_label", "warning", "Value is empty.")
            else:
                _report(f"telegram.{route_name}.destination_label", "ok", "Configured.")
            continue
        if mode in {"personal_dm", "telegram_chat"}:
            chat_id = str(route.get("chat_id") or "").strip()
            if not chat_id:
                problems.append(f"Missing required value: telegram.{route_name}.chat_id")
                _report(f"telegram.{route_name}.chat_id", "missing", "Value is empty.")
            else:
                _report(f"telegram.{route_name}.chat_id", "ok", "Configured.")


def _check_launcher_config(config: dict[str, Any], problems: list[str], warnings: list[str]) -> None:
    mode = _execution_mode(config)
    launcher = dict(config.get("launcher") or {})
    if mode != "launcher":
        _report("launcher", "ok", "Launcher mode is disabled.")
        return
    _report("launcher", "ok", "Launcher mode is enabled.")
    backend = str(launcher.get("telegram_runner_backend") or "none").strip() or "none"
    if backend == "none":
        _report("launcher.telegram_runner_backend", "ok", "Telegram runner is disabled.")
        return
    if backend == "metheus_cli":
        _report("launcher.telegram_runner_backend", "ok", "Using metheus-governance-mcp-cli runner backend.")
        route_name = str(launcher.get("metheus_route_name") or "").strip()
        if route_name:
            _report("launcher.metheus_route_name", "ok", "Configured.")
        else:
            problems.append("Missing required value: launcher.metheus_route_name")
            _report("launcher.metheus_route_name", "missing", "Value is empty.")
        if shutil.which("metheus-governance-mcp-cli") or shutil.which("metheus-governance-mcp-cli.cmd"):
            _report("metheus-governance-mcp-cli", "ok", "Found in PATH.")
        else:
            warnings.append("metheus-governance-mcp-cli was not found in PATH.")
            _report("metheus-governance-mcp-cli", "warning", "Not found in PATH.")


def _check_route_support(
    config: dict[str, Any],
    *,
    execution_mode: str,
    problems: list[str],
    warnings: list[str],
) -> None:
    telegram = dict(config.get("telegram") or {})
    enabled = bool(telegram.get("enabled"))
    conversation_route = dict(telegram.get("conversation_route") or {})
    artifact_route = dict(telegram.get("artifact_route") or {})
    conversation_mode = _normalize_route_mode(str(conversation_route.get("mode") or "none"))
    artifact_mode = _normalize_route_mode(str(artifact_route.get("mode") or "none"))
    supported_conversation = set(_supported_conversation_route_modes(config, execution_mode=execution_mode))
    supported_artifact = set(_supported_artifact_route_modes(config, execution_mode=execution_mode))

    if not enabled:
        if conversation_mode != "none":
            warnings.append("Telegram is disabled, so conversation_route is stored but inactive.")
            _report("telegram.conversation_route", "warning", "Telegram is disabled, so this route is inactive.")
        if artifact_mode != "none":
            warnings.append("Telegram is disabled, so artifact_route is stored but inactive.")
            _report("telegram.artifact_route", "warning", "Telegram is disabled, so this route is inactive.")
        return

    if conversation_mode not in supported_conversation:
        problems.append(
            "Unsupported route combination: "
            f"conversation_route.mode={conversation_mode} is not supported in {execution_mode}"
        )
        _report(
            "telegram.conversation_route",
            "error",
            f"Supported here: {_describe_supported_route_modes(sorted(supported_conversation))}",
        )
    else:
        _report("telegram.conversation_route", "ok", f"Supported in {execution_mode}.")

    if artifact_mode not in supported_artifact:
        problems.append(
            "Unsupported route combination: "
            f"artifact_route.mode={artifact_mode} is not supported in {execution_mode}"
        )
        _report(
            "telegram.artifact_route",
            "error",
            f"Supported here: {_describe_supported_route_modes(sorted(supported_artifact))}",
        )
    else:
        _report("telegram.artifact_route", "ok", f"Supported in {execution_mode}.")


def _collect_startup_blockers(config: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    warnings: list[str] = []
    requested_mode = _execution_mode(config)
    _check_required(config, ("profile", "bot_name"), "profile.bot_name", problems)
    _check_required(config, ("zoom", "client_id"), "zoom.client_id", problems)
    _check_required(config, ("zoom", "client_secret"), "zoom.client_secret", problems)
    _check_required(config, ("local_ai", "meeting_output_device"), "local_ai.meeting_output_device", problems)
    _check_zoom_credentials(config, problems, warnings)
    _check_launcher_config(config, problems, warnings)
    _check_telegram_config(config, problems, warnings)
    _check_route_support(config, execution_mode=requested_mode, problems=problems, warnings=warnings)
    return problems


def _finish_report(problems: list[str], warnings: list[str]) -> int:
    if warnings:
        print("\nWarnings")
        for item in warnings:
            print(f"- {item}")

    if problems:
        print("\nProblems")
        for item in problems:
            print(f"- {item}")
        return 1

    print("\nDoctor finished with no blocking problems.")
    return 0
