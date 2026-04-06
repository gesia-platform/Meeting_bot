"""Command-line entrypoint for the local meeting AI runtime."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from typing import Any

import uvicorn

from .assets import prepare_whisper_cpp_assets, whisper_cpp_asset_status
from .app import create_service


def _default_whisper_cpp_model() -> str:
    return os.getenv("DELEGATE_LOCAL_WHISPER_MODEL", "base").strip() or "base"


def _default_finalizer_poll_seconds() -> float:
    try:
        value = float(os.getenv("DELEGATE_FINALIZER_POLL_SECONDS", "5").strip() or "5")
    except ValueError:
        value = 5.0
    return max(value, 0.1)


def _doctor_install_commands(profile: str) -> list[str]:
    if profile == "integrated":
        return [
            "python -m pip install -e .",
            'python -m pip install -e ".[meeting-full]"',
            "python -m local_meeting_ai_runtime assets prepare --model <model>",
            "$env:DELEGATE_SESSION_COMPLETION_MODE='queued'",
            "python -m local_meeting_ai_runtime doctor --profile integrated",
            "python -m lush_local_ai_launcher start",
        ]
    return [
        "python -m pip install -e .",
        'python -m pip install -e ".[meeting-full]"',
        "python -m local_meeting_ai_runtime assets prepare --model <model>",
        "python -m local_meeting_ai_runtime doctor --profile runtime",
    ]


def _unique_lines(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        normalized = str(item or "").strip()
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(normalized)
    return ordered


def _doctor_payload(*, profile: str, model_name: str, asset_root: str | None = None) -> dict[str, Any]:
    service = create_service()
    overview = service.runtime_overview()
    recording_strategy = dict(overview.get("recording_transcription_strategy") or {})
    quality_readiness = dict(overview.get("quality_readiness") or {})
    artifact_export_readiness = dict(overview.get("artifact_export_readiness") or {})
    whisper_assets = whisper_cpp_asset_status(model_name=model_name, asset_root=asset_root)

    blocking_reasons: list[str] = []
    quality_notes: list[str] = []

    if not bool(recording_strategy.get("can_transcribe")):
        blocking_reasons.extend(recording_strategy.get("blocking_reasons") or [])
    blocking_reasons.extend(quality_readiness.get("blocking_reasons") or [])
    blocking_reasons.extend(artifact_export_readiness.get("blocking_reasons") or [])
    quality_notes.extend(artifact_export_readiness.get("quality_notes") or [])

    if not bool(whisper_assets.get("external_asset_root_ready")):
        quality_notes.append(
            "whisper.cpp assets are not fully prepared in the external runtime asset root yet, so lightweight packaging is incomplete."
        )

    configured_completion_mode = str(os.getenv("DELEGATE_SESSION_COMPLETION_MODE", "inline") or "").strip().lower() or "inline"
    finalizer_profile = {
        "configured_completion_mode": configured_completion_mode,
        "queued_recommended": profile == "integrated",
        "queued_ready": configured_completion_mode == "queued",
    }
    if profile == "integrated" and configured_completion_mode != "queued":
        quality_notes.append(
            "Integrated profile is lighter in practice when DELEGATE_SESSION_COMPLETION_MODE=queued so the finisher can drain heavy post-meeting work separately."
        )

    return {
        "ok": True,
        "profile": profile,
        "ready": not bool(_unique_lines(blocking_reasons)),
        "model_name": model_name,
        "install_commands": _doctor_install_commands(profile),
        "whisper_cpp_assets": whisper_assets,
        "recording_transcription_strategy": recording_strategy,
        "quality_readiness": quality_readiness,
        "artifact_export_readiness": artifact_export_readiness,
        "finalizer_profile": finalizer_profile,
        "blocking_reasons": _unique_lines(blocking_reasons),
        "quality_notes": _unique_lines(quality_notes),
    }


async def _run_finalizer_loop(
    service,
    *,
    limit: int,
    runner_id: str,
    poll_seconds: float,
    max_idle_cycles: int = 0,
) -> None:
    idle_cycles = 0
    while True:
        payload = await service.process_finalization_queue(
            limit=max(limit, 1),
            runner_id=runner_id,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if int(payload.get("processed_count", 0) or 0) > 0:
            idle_cycles = 0
        else:
            idle_cycles += 1
        if max_idle_cycles > 0 and idle_cycles >= max_idle_cycles:
            return
        await asyncio.sleep(max(poll_seconds, 0.1))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="local-meeting-ai-runtime")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("serve", help="Run the local meeting AI runtime server.")

    assets_parser = subparsers.add_parser("assets", help="Inspect or prepare external whisper.cpp assets.")
    assets_subparsers = assets_parser.add_subparsers(dest="assets_command")

    status_parser = assets_subparsers.add_parser("status", help="Show current whisper.cpp asset readiness.")
    status_parser.add_argument("--model", default=_default_whisper_cpp_model(), help="whisper.cpp model name.")
    status_parser.add_argument("--asset-root", default="", help="Override runtime asset root.")

    prepare_parser = assets_subparsers.add_parser("prepare", help="Copy whisper.cpp assets into the runtime asset root.")
    prepare_parser.add_argument("--model", default=_default_whisper_cpp_model(), help="whisper.cpp model name.")
    prepare_parser.add_argument("--asset-root", default="", help="Override runtime asset root.")
    prepare_parser.add_argument("--source-root", default="", help="Source whisper.cpp root to copy from.")
    prepare_parser.add_argument("--overwrite", action="store_true", help="Overwrite existing files in the asset root.")
    prepare_parser.add_argument("--skip-vad", action="store_true", help="Skip copying the VAD model.")

    doctor_parser = subparsers.add_parser("doctor", help="Inspect local readiness without starting the runtime server.")
    doctor_parser.add_argument(
        "--profile",
        choices=("runtime", "integrated"),
        default="runtime",
        help="Inspect the standalone runtime profile or the launcher-integrated profile.",
    )
    doctor_parser.add_argument("--model", default=_default_whisper_cpp_model(), help="whisper.cpp model name.")
    doctor_parser.add_argument("--asset-root", default="", help="Override runtime asset root.")

    finalizer_parser = subparsers.add_parser("finalizer", help="Run or invoke the post-meeting finisher path.")
    finalizer_subparsers = finalizer_parser.add_subparsers(dest="finalizer_command")

    run_once_parser = finalizer_subparsers.add_parser("run-once", help="Process queued finalization jobs once.")
    run_once_parser.add_argument("--limit", type=int, default=1, help="Maximum number of queued jobs to process.")
    run_once_parser.add_argument(
        "--runner-id",
        default="local-meeting-finisher",
        help="Lease owner id recorded in the finalization queue.",
    )

    run_loop_parser = finalizer_subparsers.add_parser(
        "run-loop",
        help="Keep polling the queued finalization jobs until interrupted or idle limit is reached.",
    )
    run_loop_parser.add_argument("--limit", type=int, default=1, help="Maximum number of queued jobs to process.")
    run_loop_parser.add_argument(
        "--runner-id",
        default="local-meeting-finisher",
        help="Lease owner id recorded in the finalization queue.",
    )
    run_loop_parser.add_argument(
        "--poll-seconds",
        type=float,
        default=_default_finalizer_poll_seconds(),
        help="Seconds to sleep between queue polls.",
    )
    run_loop_parser.add_argument(
        "--max-idle-cycles",
        type=int,
        default=0,
        help="Exit after this many empty polls. Use 0 to run until interrupted.",
    )

    complete_parser = finalizer_subparsers.add_parser("complete-session", help="Finalize one session immediately.")
    complete_parser.add_argument("--session-id", required=True, help="Session id to finalize.")

    args = parser.parse_args(argv)

    if args.command == "assets":
        if args.assets_command == "prepare":
            payload = prepare_whisper_cpp_assets(
                model_name=str(args.model or "").strip() or _default_whisper_cpp_model(),
                asset_root=str(args.asset_root or "").strip() or None,
                source_root=str(args.source_root or "").strip() or None,
                include_vad=not bool(args.skip_vad),
                overwrite=bool(args.overwrite),
            )
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return
        payload = whisper_cpp_asset_status(
            model_name=str(getattr(args, "model", "") or "").strip() or _default_whisper_cpp_model(),
            asset_root=str(getattr(args, "asset_root", "") or "").strip() or None,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.command == "doctor":
        payload = _doctor_payload(
            profile=str(getattr(args, "profile", "runtime") or "runtime").strip() or "runtime",
            model_name=str(getattr(args, "model", "") or "").strip() or _default_whisper_cpp_model(),
            asset_root=str(getattr(args, "asset_root", "") or "").strip() or None,
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    if args.command == "finalizer":
        service = create_service()
        if args.finalizer_command == "complete-session":
            session, completion = asyncio.run(
                service.request_session_completion(
                    str(args.session_id or "").strip(),
                    mode="inline",
                    requested_by="cli_finalizer",
                )
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "session": session.to_dict(),
                        "completion": completion,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if args.finalizer_command == "run-loop":
            asyncio.run(
                _run_finalizer_loop(
                    service,
                    limit=max(int(getattr(args, "limit", 1) or 1), 1),
                    runner_id=str(getattr(args, "runner_id", "") or "local-meeting-finisher").strip()
                    or "local-meeting-finisher",
                    poll_seconds=float(getattr(args, "poll_seconds", _default_finalizer_poll_seconds()) or 5.0),
                    max_idle_cycles=max(int(getattr(args, "max_idle_cycles", 0) or 0), 0),
                )
            )
            return
        payload = asyncio.run(
            service.process_finalization_queue(
                limit=max(int(getattr(args, "limit", 1) or 1), 1),
                runner_id=str(getattr(args, "runner_id", "") or "local-meeting-finisher").strip()
                or "local-meeting-finisher",
            )
        )
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return

    uvicorn.run(
        "local_meeting_ai_runtime.app:app",
        host=os.getenv("DELEGATE_HOST", "127.0.0.1"),
        port=int(os.getenv("DELEGATE_PORT", "9010")),
        reload=False,
    )


if __name__ == "__main__":
    main()
