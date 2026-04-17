from __future__ import annotations

from dataclasses import dataclass
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from local_meeting_ai_runtime.meeting_output_skill import (
    build_interactive_meeting_output_skill_path,
    load_meeting_output_skill,
    resolve_generated_meeting_output_dir,
    resolve_meeting_output_skill_path,
    write_generated_meeting_output_skill,
)

from .config import write_config
from .paths import resolve_relative_path, resolve_workspace_path, workspace_root


COMPOSE_SANDBOX_ROOT = Path(tempfile.gettempdir()) / "zoom-meeting-bot" / "skill-compose"


@dataclass(slots=True)
class SkillAsset:
    index: int
    path: Path
    relative_path: str
    folder_name: str
    name: str
    description: str
    is_active: bool = False


_RESULT_STYLE_LABELS = {
    "executive_summary": "회의 전체 요약",
    "sections": "핵심 논의 주제",
    "decisions": "결정사항",
    "action_items": "액션 아이템",
    "open_questions": "열린 질문",
    "risk_signals": "리스크 신호",
    "memo": "메모",
    "postprocess_requests": "추가 결과물 제안",
}

_COMPOSE_REPLY_SKIP_PREFIXES = (
    "sources:",
    "source:",
    "brand cues were",
    "i couldn't run",
    "i could not run",
    "for more information",
    "to learn more",
)


def resolve_codex_command(config: dict[str, Any]) -> str:
    local_ai = dict(config.get("local_ai") or {})
    raw = str(local_ai.get("codex_command") or "codex").strip() or "codex"
    candidate = Path(raw).expanduser()
    if candidate.is_absolute() and candidate.exists():
        if sys.platform.startswith("win") and candidate.suffix.lower() == ".cmd":
            direct_candidate = candidate.with_suffix(".exe")
            if direct_candidate.exists():
                return str(direct_candidate.resolve())
        return str(candidate.resolve())
    if any(separator in raw for separator in ("/", "\\")):
        workspace_candidate = resolve_relative_path(raw)
        if workspace_candidate.exists():
            if sys.platform.startswith("win") and workspace_candidate.suffix.lower() == ".cmd":
                direct_candidate = workspace_candidate.with_suffix(".exe")
                if direct_candidate.exists():
                    return str(direct_candidate.resolve())
            return str(workspace_candidate)
    if sys.platform.startswith("win") and raw.casefold() in {"codex", "codex.cmd"}:
        direct_discovered = shutil.which("codex.exe")
        if direct_discovered:
            return str(Path(direct_discovered).resolve())
    discovered = shutil.which(raw)
    if sys.platform.startswith("win") and discovered and discovered.lower().endswith(".cmd"):
        direct_candidate = Path(discovered).with_suffix(".exe")
        if direct_candidate.exists():
            return str(direct_candidate.resolve())
    return discovered or ""


def build_interactive_skill_target_path(config: dict[str, Any], *, label: str = "") -> Path:
    skills = dict(config.get("skills") or {})
    generated_dir = str(skills.get("generated_meeting_output_dir") or "").strip()
    return build_interactive_meeting_output_skill_path(
        label=label,
        output_dir=generated_dir or resolve_generated_meeting_output_dir(None),
    )


def build_session_skill_refinement_prompt(
    *,
    config: dict[str, Any],
    session_id: str,
    user_feedback: str,
) -> str:
    session_context = _load_session_refinement_context(config, session_id)
    return (
        "다음은 이미 생성된 회의 결과물을 본 뒤, 앞으로 비슷한 결과물을 더 잘 만들기 위해 "
        "재사용 가능한 회의 결과물 skill로 자산화하려는 요청입니다.\n\n"
        "사용자의 피드백:\n"
        f"{str(user_feedback or '').strip()}\n\n"
        "참고할 완료 세션 context:\n"
        f"{session_context}\n\n"
        "위 피드백은 세션 원문을 다시 요약하라는 뜻이 아니라, 다음 결과물 생성 방식에 반영할 "
        "재사용 가능한 작성 전략을 SKILL.md에 반영하라는 뜻입니다."
    )


def prepare_skill_compose_workspace(
    *,
    base_skill_path: Path,
    final_output_path: Path,
) -> dict[str, Path]:
    sandbox_dir = _build_compose_sandbox_dir(final_output_path)
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    (sandbox_dir / "BASE_SKILL.md").write_text(base_skill_path.read_text(encoding="utf-8"), encoding="utf-8")
    (sandbox_dir / "CONVERSATION.md").write_text("# Skill Compose Conversation\n", encoding="utf-8")
    (sandbox_dir / "USER_MESSAGE.md").write_text("", encoding="utf-8")
    (sandbox_dir / "SKILL.md").write_text(
        "---\n"
        "name: \n"
        "description: \n"
        "---\n\n"
        "# Meeting Output Override\n",
        encoding="utf-8",
    )
    return {
        "sandbox_dir": sandbox_dir,
        "sandbox_skill_path": sandbox_dir / "SKILL.md",
        "sandbox_base_skill_path": sandbox_dir / "BASE_SKILL.md",
        "sandbox_conversation_path": sandbox_dir / "CONVERSATION.md",
        "sandbox_user_message_path": sandbox_dir / "USER_MESSAGE.md",
        "sandbox_reply_path": sandbox_dir / "ASSISTANT_REPLY.md",
    }


def _load_session_refinement_context(config: dict[str, Any], session_id: str) -> str:
    runtime = dict(config.get("runtime") or {})
    store_path = resolve_workspace_path(str(runtime.get("store_path") or "data/delegate_sessions.json"))
    session_key = str(session_id or "").strip()
    if not session_key:
        return "- session_id가 비어 있어 완료 세션 context를 찾지 못했습니다."
    if not store_path.exists():
        return f"- 세션 저장소를 찾지 못했습니다: {store_path}"
    try:
        payload = json.loads(store_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return f"- 세션 저장소를 읽지 못했습니다: {exc}"
    if not isinstance(payload, dict):
        return "- 세션 저장소 형식이 예상과 달라 완료 세션 context를 찾지 못했습니다."
    raw = payload.get(session_key)
    if not isinstance(raw, dict):
        return f"- session_id `{session_key}`에 해당하는 세션을 찾지 못했습니다."
    return _format_session_refinement_context(raw, store_path=store_path)


def _format_session_refinement_context(raw: dict[str, Any], *, store_path: Path) -> str:
    summary_packet = dict(raw.get("summary_packet") or {})
    briefing = dict(summary_packet.get("briefing") or {})
    exports = [dict(item or {}) for item in list(raw.get("summary_exports") or []) if isinstance(item, dict)]
    lines = [
        f"- session_id: {raw.get('session_id') or ''}",
        f"- meeting_topic: {raw.get('meeting_topic') or ''}",
        f"- status: {raw.get('status') or ''}",
    ]
    if briefing:
        lines.extend(
            [
                "",
                "## 현재 결과물 요약",
                f"- title: {briefing.get('title') or ''}",
                f"- executive_summary: {briefing.get('executive_summary') or raw.get('summary') or ''}",
            ]
        )
        for key, label in (
            ("sections", "sections"),
            ("decisions", "decisions"),
            ("action_items", "action_items"),
            ("open_questions", "open_questions"),
            ("risk_signals", "risk_signals"),
        ):
            values = list(briefing.get(key) or [])
            if not values:
                continue
            lines.append(f"- {label}:")
            for item in values[:6]:
                if isinstance(item, dict):
                    heading = str(item.get("heading") or "").strip()
                    summary = str(item.get("summary") or "").strip()
                    lines.append(f"  - {heading}: {summary}".strip())
                else:
                    lines.append(f"  - {str(item).strip()}")
    md_excerpt = _read_summary_markdown_excerpt(exports, base_dir=store_path.parent)
    if md_excerpt:
        lines.extend(["", "## 현재 Markdown 결과물 일부", md_excerpt])
    return "\n".join(lines).strip()


def _read_summary_markdown_excerpt(exports: list[dict[str, Any]], *, base_dir: Path) -> str:
    for item in exports:
        if str(item.get("format") or "").strip().lower() != "md":
            continue
        raw_path = str(item.get("path") or item.get("file_path") or "").strip()
        if not raw_path:
            continue
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="replace")
        return text.strip()[:8000]
    return ""


def write_skill_compose_user_message(*, workspace_dir: Path, text: str) -> Path:
    path = workspace_dir / "USER_MESSAGE.md"
    path.write_text(str(text or "").strip() + "\n", encoding="utf-8")
    return path


def append_skill_compose_message(*, workspace_dir: Path, role: str, text: str) -> Path:
    path = workspace_dir / "CONVERSATION.md"
    current = path.read_text(encoding="utf-8") if path.exists() else "# Skill Compose Conversation\n"
    current = current.rstrip() + "\n\n"
    current += f"## {str(role or '').strip()}\n{str(text or '').strip()}\n"
    path.write_text(current, encoding="utf-8")
    return path


def build_skill_compose_turn_prompt() -> str:
    return (
        "You are helping the user refine a reusable result-generation skill override for ZOOM_MEETING_BOT.\n"
        "Only use files in the current directory: `BASE_SKILL.md`, `CONVERSATION.md`, `USER_MESSAGE.md`, and `SKILL.md`.\n"
        "Do not inspect parent directories, repository files, code, tests, configs, docs, or any path outside the current directory.\n"
        "Treat `BASE_SKILL.md` as the stable reference and do not modify it.\n"
        "Treat `SKILL.md` as the current editable draft and update only that file when needed.\n"
        "Read `USER_MESSAGE.md` for the newest user request and `CONVERSATION.md` for the chat so far.\n"
        "Respond in Korean.\n"
        "Your user-facing reply must be Korean-only.\n"
        "Do not output English status text, validator chatter, tool diagnostics, repo notes, source lists, markdown links, or implementation commentary in the user-facing reply.\n"
        "When the request is clear, do not stay overly generic or timid; reflect the user's style direction confidently in concrete renderer metadata and body guidance.\n"
        "Prefer a well-shaped, specific override over a minimal safe placeholder when the user has already given enough direction.\n"
        "Do not invent a default style when the user has already expressed a preference.\n"
        "If the request is clear enough, update `SKILL.md` immediately and reply with `READY: ` followed by one short Korean confirmation sentence.\n"
        "If something is truly ambiguous and blocks a good draft, ask exactly one short follow-up question and format it as `QUESTION: ` followed by that one Korean question.\n"
        "Do not mention internal implementation details, repository structure, hidden files, frontmatter, metadata, slot names, or JSON field names unless the user explicitly asks.\n"
        "Talk to the user only in terms of the final result they want to see.\n"
        "The only user-facing asset from this conversation is the final `SKILL.md`; do not suggest sidecar JSON, helper Markdown, or extra persistent files.\n"
        "The skill may guide summary emphasis, section strategy, final document block ordering, and result post-processing within the existing engine boundary.\n"
        "If the user wants a block name or role to change, silently reflect that in the draft so the final result really behaves that way; do not explain the internal slot mapping unless asked.\n"
        "If the user clearly asks for final document ordering, visibility, per-block counts, section metadata display, renderer tone, brand-like visual direction, or follow-up output such as image briefs, express that in SKILL.md.\n"
        "If the user mentions a company, brand, or recognizable visual identity, you must first use web search to infer stable public-facing cues before drafting the result.\n"
        "Do not rely on prior model memory alone for brand interpretation.\n"
        "When you do that, prefer concrete renderer colors and practical Korean-friendly document font choices over vague design prose.\n"
        "Useful keys include `result_block_order`, `result_block_order_mode`, `renderer_theme_name`, `renderer_primary_color`, `renderer_accent_color`, `renderer_neutral_color`, `renderer_title_font`, `renderer_heading_font`, `renderer_body_font`, `renderer_cover_align`, `renderer_surface_tint_color`, `renderer_cover_kicker`, `renderer_heading1_color`, `renderer_heading2_color`, `renderer_heading3_color`, `renderer_body_text_color`, `renderer_muted_text_color`, `renderer_title_divider_color`, `renderer_section_border_color`, `renderer_table_header_fill_color`, `renderer_table_label_fill_color`, `renderer_cover_fill_color`, `renderer_kicker_fill_color`, `renderer_kicker_text_color`, `renderer_section_band_fill_color`, `renderer_section_panel_fill_color`, `renderer_section_accent_fill_color`, `renderer_overview_label_fill_color`, `renderer_overview_value_fill_color`, `renderer_overview_panel_fill_color`, `renderer_page_top_margin_inches`, `renderer_page_bottom_margin_inches`, `renderer_page_left_margin_inches`, `renderer_page_right_margin_inches`, `renderer_body_line_spacing`, `renderer_list_line_spacing`, `renderer_heading2_space_before_pt`, `renderer_heading2_space_after_pt`, `renderer_heading3_space_before_pt`, `renderer_heading3_space_after_pt`, `renderer_title_space_after_pt`, `renderer_title_divider_size`, `renderer_title_divider_space`, `postprocess_image_width_inches`, `show_title`, `show_overview`, `show_executive_summary`, `show_sections`, `show_decisions`, `show_action_items`, `show_open_questions`, `show_risk_signals`, `show_postprocess_requests`, `show_memo`, `show_overview_datetime`, `show_overview_author`, `show_overview_session_id`, `show_overview_participants`, `max_display_sections`, `max_decisions`, `max_action_items`, `max_open_questions`, `max_risk_signals`, `max_postprocess_requests`, `section_numbering`, and display label keys such as `overview_heading` and `executive_summary_heading`.\n"
        "Do not try to customize `제기자`, `주요 화자`, or `타임스탬프`; those trace fields are core system output.\n"
        "Do not add `max_*` keys unless the user explicitly asks for a firm upper bound.\n"
        "If the user wants images, appendix-style outputs, or renderer polish, describe that as result post-processing and concrete document-surface guidance in the draft rather than changing the engine itself.\n"
        "If the user mentions a company, brand, or visual mood, capture that in the draft as renderer theme guidance and, when appropriate, color direction.\n"
        "Prefer soft guidance over fixed page counts, sentence quotas, or rigid per-block caps unless the user explicitly insists on a hard constraint.\n"
        "Your user-facing reply should be at most two short Korean sentences total.\n"
    )


def run_skill_compose_turn(
    *,
    codex_command: str,
    workspace_dir: Path,
) -> dict[str, str | int]:
    workspace_dir.mkdir(parents=True, exist_ok=True)
    reply_path = workspace_dir / "ASSISTANT_REPLY.md"
    if reply_path.exists():
        reply_path.unlink()
    command = [
        codex_command,
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
        "-s",
        "workspace-write",
        "-C",
        str(workspace_dir),
        "-o",
        str(reply_path),
        build_skill_compose_turn_prompt(),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    reply_text = ""
    if reply_path.exists():
        reply_text = reply_path.read_text(encoding="utf-8").strip()
    if not reply_text:
        reply_text = _pick_compose_reply_fallback(completed.stdout, completed.stderr)
    return {
        "exit_code": int(completed.returncode),
        "assistant_reply": reply_text.strip(),
        "stdout": str(completed.stdout or ""),
        "stderr": str(completed.stderr or ""),
    }


def finalize_composed_skill(
    *,
    sandbox_skill_path: Path,
    final_output_path: Path,
) -> Path | None:
    if not sandbox_skill_path.exists():
        return None
    loaded = load_meeting_output_skill(sandbox_skill_path)
    body = str(loaded.get("body") or "").strip()
    if not body or body == "# Meeting Output Override":
        return None
    name = str(loaded.get("name") or "").strip() or final_output_path.parent.name
    description = str(loaded.get("description") or "").strip() or "Generated meeting output override."
    return write_generated_meeting_output_skill(
        output_path=final_output_path,
        name=name,
        description=description,
        metadata=dict(loaded.get("metadata") or {}),
        body=body,
    )


def interpret_skill_compose_reply(reply_text: str) -> dict[str, str]:
    text = str(reply_text or "").strip()
    kind = "ready"
    if text.upper().startswith("QUESTION:"):
        kind = "question"
        text = text.split(":", 1)[1].strip()
    elif text.upper().startswith("READY:"):
        text = text.split(":", 1)[1].strip()
    text = _sanitize_skill_compose_reply(text)
    if _looks_like_unfriendly_compose_reply(text):
        text = ""
    if not text:
        text = (
            "추가로 꼭 확인해야 할 점이 있으면 한 줄로 말씀해 주세요."
            if kind == "question"
            else "요청을 반영한 결과물 스타일 초안을 업데이트했습니다."
        )
    if kind != "question" and text.endswith("?") and len(text) <= 120:
        kind = "question"
    return {
        "kind": kind,
        "text": text,
    }


def summarize_composed_skill_for_user(skill_path: Path) -> str:
    loaded = load_meeting_output_skill(skill_path)
    metadata = dict(loaded.get("metadata") or {})
    name = str(loaded.get("name") or "").strip() or skill_path.parent.name

    lines = [
        "결과물 스타일 초안을 저장했습니다.",
        f"- 스타일 이름: {name}",
    ]

    theme_name = str(metadata.get("renderer_theme_name") or "").strip()
    fonts = [
        str(metadata.get("renderer_title_font") or "").strip(),
        str(metadata.get("renderer_heading_font") or "").strip(),
        str(metadata.get("renderer_body_font") or "").strip(),
    ]
    fonts = [font for font in fonts if font]
    colors = [
        str(metadata.get("renderer_primary_color") or "").strip(),
        str(metadata.get("renderer_accent_color") or "").strip(),
        str(metadata.get("renderer_neutral_color") or "").strip(),
    ]
    colors = [color for color in colors if color]

    if theme_name:
        lines.append(f"- 분위기 방향: {theme_name}")
    if colors:
        lines.append(f"- 색감 단서: {', '.join(colors[:3])}")
    if fonts:
        unique_fonts = list(dict.fromkeys(fonts))
        lines.append(f"- 폰트 방향: {', '.join(unique_fonts)}")

    block_order = [
        _RESULT_STYLE_LABELS.get(item.strip(), item.strip())
        for item in str(metadata.get("result_block_order") or "").split(",")
        if item.strip()
    ]
    if block_order:
        lines.append(f"- 주요 구성 순서: {', '.join(block_order[:4])}")

    hidden_blocks = []
    for key, label in (
        ("show_risk_signals", "리스크 신호"),
        ("show_memo", "메모"),
        ("show_postprocess_requests", "이미지/추가 결과물 제안"),
    ):
        if str(metadata.get(key) or "").strip().lower() == "never":
            hidden_blocks.append(label)
    if hidden_blocks:
        lines.append(f"- 결과물에서 숨김: {', '.join(hidden_blocks)}")

    description = str(loaded.get("description") or "").strip()
    if description:
        lines.append(f"- 한 줄 설명: {description}")

    return "\n".join(lines)


def activate_meeting_output_override(
    *,
    config: dict[str, Any],
    config_path: Path,
    skill_path: Path,
    clear_customization: bool = True,
) -> dict[str, Any]:
    updated = dict(config)
    skills = dict(updated.get("skills") or {})
    skills["meeting_output_override_path"] = _to_workspace_relative(skill_path)
    if clear_customization:
        skills["meeting_output_customization"] = ""
    updated["skills"] = skills
    write_config(config_path, updated)
    return updated


def clear_meeting_output_override(
    *,
    config: dict[str, Any],
    config_path: Path,
    clear_customization: bool = False,
) -> dict[str, Any]:
    updated = dict(config)
    skills = dict(updated.get("skills") or {})
    skills["meeting_output_override_path"] = ""
    if clear_customization:
        skills["meeting_output_customization"] = ""
    updated["skills"] = skills
    write_config(config_path, updated)
    return updated


def describe_skill_state(config: dict[str, Any]) -> dict[str, str]:
    skills = dict(config.get("skills") or {})
    base_path = resolve_meeting_output_skill_path(str(skills.get("meeting_output_path") or "").strip())
    override_raw = str(skills.get("meeting_output_override_path") or "").strip()
    override_path = resolve_workspace_path(override_raw) if override_raw else None
    generated_dir = resolve_generated_meeting_output_dir(str(skills.get("generated_meeting_output_dir") or "").strip())
    return {
        "base_skill_path": str(base_path),
        "override_skill_path": str(override_path) if override_path else "",
        "generated_skill_dir": str(generated_dir),
        "customization_request": str(skills.get("meeting_output_customization") or "").strip(),
    }


def list_generated_skill_assets(config: dict[str, Any]) -> list[SkillAsset]:
    state = describe_skill_state(config)
    generated_dir = Path(state["generated_skill_dir"])
    override_path = Path(state["override_skill_path"]).resolve() if state["override_skill_path"] else None
    if not generated_dir.exists():
        return []
    skill_paths = sorted(
        generated_dir.rglob("SKILL.md"),
        key=lambda candidate: candidate.stat().st_mtime,
        reverse=True,
    )
    assets: list[SkillAsset] = []
    for index, skill_path in enumerate(skill_paths, start=1):
        loaded = load_meeting_output_skill(skill_path)
        resolved = Path(str(loaded.get("resolved_path") or skill_path)).resolve()
        assets.append(
            SkillAsset(
                index=index,
                path=resolved,
                relative_path=_to_workspace_relative(resolved),
                folder_name=resolved.parent.name,
                name=str(loaded.get("name") or "").strip() or resolved.parent.name,
                description=str(loaded.get("description") or "").strip(),
                is_active=bool(override_path and resolved == override_path),
            )
        )
    return assets


def resolve_skill_asset_selection(assets: list[SkillAsset], selector: str) -> SkillAsset | None:
    raw = str(selector or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        wanted = int(raw)
        for asset in assets:
            if asset.index == wanted:
                return asset
    normalized = raw.casefold()
    exact_matches = [
        asset
        for asset in assets
        if normalized
        in {
            asset.name.casefold(),
            asset.folder_name.casefold(),
            asset.relative_path.casefold(),
            str(asset.path).casefold(),
        }
    ]
    if exact_matches:
        return exact_matches[0]
    partial_matches = [
        asset
        for asset in assets
        if normalized in asset.name.casefold()
        or normalized in asset.folder_name.casefold()
        or normalized in asset.relative_path.casefold()
    ]
    if partial_matches:
        return partial_matches[0]
    return None


def _build_compose_sandbox_dir(final_output_path: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return COMPOSE_SANDBOX_ROOT / f"{final_output_path.parent.name}-{stamp}"


def _pick_compose_reply_fallback(stdout: str, stderr: str) -> str:
    for source in (stdout, stderr):
        text = str(source or "").strip()
        if text:
            lines = [line.strip() for line in text.splitlines() if line.strip()]
            if lines:
                return lines[-1]
    return ""


def _sanitize_skill_compose_reply(text: str) -> str:
    cleaned_lines: list[str] = []
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        lower = line.casefold()
        if any(lower.startswith(prefix) for prefix in _COMPOSE_REPLY_SKIP_PREFIXES):
            continue
        if "validator" in lower or "sanity check" in lower:
            continue
        if line.startswith("[") and "](" in line:
            continue
        line = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", line)
        cleaned_lines.append(line)
    if not cleaned_lines:
        return ""
    merged = " ".join(cleaned_lines)
    return re.sub(r"\s+", " ", merged).strip()


def _looks_like_unfriendly_compose_reply(text: str) -> bool:
    candidate = str(text or "").strip()
    if not candidate:
        return False
    if re.search(r"https?://|www\.", candidate, re.IGNORECASE):
        return True
    has_hangul = bool(re.search(r"[가-힣]", candidate))
    has_english_words = bool(re.search(r"[A-Za-z]{4,}", candidate))
    return has_english_words and not has_hangul


def _to_workspace_relative(path: Path) -> str:
    resolved = path.resolve()
    root = workspace_root().resolve()
    try:
        return str(resolved.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(resolved)
