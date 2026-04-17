from __future__ import annotations

import hashlib
from datetime import datetime
import re
from pathlib import Path

from zoom_meeting_bot_cli.paths import package_root, resolve_package_path, resolve_workspace_path

DEFAULT_MEETING_OUTPUT_SKILL_PATH = package_root() / "skills" / "meeting-output-default" / "SKILL.md"
DEFAULT_RESULT_BLOCK_ORDER = [
    "overview",
    "executive_summary",
    "sections",
    "decisions",
    "action_items",
    "open_questions",
    "postprocess_requests",
    "memo",
]
RESULT_LIMIT_KEYS = (
    "max_display_sections",
    "max_decisions",
    "max_action_items",
    "max_open_questions",
    "max_risk_signals",
    "max_postprocess_requests",
)
VALID_RESULT_BLOCKS = {
    "overview",
    "executive_summary",
    "sections",
    "decisions",
    "action_items",
    "open_questions",
    "risk_signals",
    "postprocess_requests",
    "memo",
}
VALID_VISIBILITY_MODES = {"always", "auto", "never"}
VALID_RESULT_BLOCK_ORDER_MODES = {"append_missing", "exact"}
VALID_SECTION_NUMBERING_MODES = {"numbered", "plain"}
RENDERER_TOKEN_KEYS = (
    "renderer_cover_align",
    "renderer_cover_layout",
    "renderer_cover_background_style",
    "renderer_panel_style",
    "renderer_heading_style",
    "renderer_overview_layout",
    "renderer_section_style",
    "renderer_list_style",
)
RENDERER_TEXT_KEYS = (
    "renderer_theme_name",
    "renderer_title_font",
    "renderer_heading_font",
    "renderer_body_font",
    "renderer_cover_kicker",
    "renderer_custom_css",
)
RENDERER_COLOR_KEYS = (
    "renderer_primary_color",
    "renderer_accent_color",
    "renderer_neutral_color",
    "renderer_surface_tint_color",
    "renderer_heading1_color",
    "renderer_heading2_color",
    "renderer_heading3_color",
    "renderer_body_text_color",
    "renderer_muted_text_color",
    "renderer_title_divider_color",
    "renderer_section_border_color",
    "renderer_table_header_fill_color",
    "renderer_table_label_fill_color",
    "renderer_cover_fill_color",
    "renderer_kicker_fill_color",
    "renderer_kicker_text_color",
    "renderer_section_band_fill_color",
    "renderer_section_panel_fill_color",
    "renderer_section_accent_fill_color",
    "renderer_overview_label_fill_color",
    "renderer_overview_value_fill_color",
    "renderer_overview_panel_fill_color",
)
RENDERER_NUMERIC_KEYS = (
    "postprocess_image_width_inches",
    "renderer_page_top_margin_inches",
    "renderer_page_bottom_margin_inches",
    "renderer_page_left_margin_inches",
    "renderer_page_right_margin_inches",
    "renderer_body_line_spacing",
    "renderer_list_line_spacing",
    "renderer_heading2_space_before_pt",
    "renderer_heading2_space_after_pt",
    "renderer_heading3_space_before_pt",
    "renderer_heading3_space_after_pt",
    "renderer_title_space_after_pt",
    "renderer_title_divider_size",
    "renderer_title_divider_space",
    "renderer_block_gap_pt",
    "renderer_panel_radius_pt",
    "renderer_cover_radius_pt",
    "renderer_heading_chip_radius_pt",
    "renderer_overview_radius_pt",
)
SYSTEM_TRACE_POLICY = {
    "show_section_raised_by": "always",
    "show_section_speakers": "always",
    "show_section_timestamps": "always",
    "max_section_timestamp_refs": 4,
    "section_raised_by_label": "제기자",
    "section_speakers_label": "주요 화자",
    "section_timestamps_label": "타임스탬프",
}
DEFAULT_BLOCK_VISIBILITY = {
    "show_title": "always",
    "show_overview": "always",
    "show_executive_summary": "always",
    "show_sections": "always",
    "show_decisions": "always",
    "show_action_items": "always",
    "show_open_questions": "always",
    "show_risk_signals": "never",
    "show_postprocess_requests": "never",
    "show_memo": "always",
    "show_overview_datetime": "always",
    "show_overview_author": "always",
    "show_overview_session_id": "always",
    "show_overview_participants": "always",
}
DEFAULT_RENDERING_POLICY = {
    "renderer_theme_name": "",
    "renderer_primary_color": "",
    "renderer_accent_color": "",
    "renderer_neutral_color": "",
    "renderer_title_font": "",
    "renderer_heading_font": "",
    "renderer_body_font": "",
    "renderer_cover_align": "",
    "renderer_cover_layout": "minimal",
    "renderer_cover_background_style": "minimal",
    "renderer_panel_style": "minimal",
    "renderer_heading_style": "underline",
    "renderer_overview_layout": "stack",
    "renderer_section_style": "minimal",
    "renderer_list_style": "minimal",
    "renderer_surface_tint_color": "",
    "renderer_cover_kicker": "",
    "renderer_heading1_color": "",
    "renderer_heading2_color": "",
    "renderer_heading3_color": "",
    "renderer_body_text_color": "",
    "renderer_muted_text_color": "",
    "renderer_title_divider_color": "",
    "renderer_section_border_color": "",
    "renderer_table_header_fill_color": "",
    "renderer_table_label_fill_color": "",
    "renderer_cover_fill_color": "",
    "renderer_kicker_fill_color": "",
    "renderer_kicker_text_color": "",
    "renderer_section_band_fill_color": "",
    "renderer_section_panel_fill_color": "",
    "renderer_section_accent_fill_color": "",
    "renderer_overview_label_fill_color": "",
    "renderer_overview_value_fill_color": "",
    "renderer_overview_panel_fill_color": "",
    "postprocess_image_width_inches": "5.9",
    "renderer_page_top_margin_inches": "",
    "renderer_page_bottom_margin_inches": "",
    "renderer_page_left_margin_inches": "",
    "renderer_page_right_margin_inches": "",
    "renderer_body_line_spacing": "",
    "renderer_list_line_spacing": "",
    "renderer_heading2_space_before_pt": "",
    "renderer_heading2_space_after_pt": "",
    "renderer_heading3_space_before_pt": "",
    "renderer_heading3_space_after_pt": "",
    "renderer_title_space_after_pt": "",
    "renderer_title_divider_size": "",
    "renderer_title_divider_space": "",
    "renderer_block_gap_pt": "8",
    "renderer_panel_radius_pt": "",
    "renderer_cover_radius_pt": "",
    "renderer_heading_chip_radius_pt": "",
    "renderer_overview_radius_pt": "",
    "renderer_custom_css": "",
}
DEFAULT_LAYOUT_POLICY = {
    "result_block_order_mode": "append_missing",
    "section_numbering": "numbered",
}
DEFAULT_RESULT_LABELS = {
    "overview_heading": "회의 개요",
    "overview_datetime_label": "회의 일시",
    "overview_author_label": "작성 주체",
    "overview_session_id_label": "세션 ID",
    "overview_participants_label": "참석자",
    "executive_summary_heading": "회의 전체 요약",
    "sections_heading": "핵심 논의 주제",
    "decisions_heading": "결정사항",
    "action_items_heading": "액션 아이템",
    "open_questions_heading": "열린 질문",
    "risk_signals_heading": "리스크 신호",
    "postprocess_requests_heading": "추가 결과물 제안",
    "memo_heading": "메모",
}
DEFAULT_EMPTY_MESSAGES = {
    "empty_executive_summary_message": "회의 전체 요약이 아직 생성되지 않았습니다.",
    "empty_sections_message": "핵심 논의 주제가 아직 정리되지 않았습니다.",
    "empty_decisions_message": "아직 확정된 결정사항이 없습니다.",
    "empty_action_items_message": "추출된 액션 아이템이 없습니다.",
    "empty_open_questions_message": "현재 남은 열린 질문이 없습니다.",
    "empty_risk_signals_message": "현재 강조할 리스크 신호가 없습니다.",
    "empty_postprocess_requests_message": "현재 추가 결과물 제안이 없습니다.",
    "empty_participants_message": "미확인",
    "empty_section_summary_message": "요약 내용이 없습니다.",
    "empty_postprocess_item_title": "후속 처리",
    "empty_postprocess_item_instruction": "추가 후속 처리 요청",
    "memo_text": "세부 음성 전사와 채팅 원문은 별도 export 파일에서 확인할 수 있습니다.",
}


def resolve_meeting_output_skill_path(configured_path: str | Path | None = None) -> Path:
    text = str(configured_path or "").strip()
    if text:
        return resolve_package_path(text)
    return DEFAULT_MEETING_OUTPUT_SKILL_PATH.resolve()


def load_meeting_output_skill(configured_path: str | Path | None = None) -> dict[str, object]:
    requested_path = resolve_meeting_output_skill_path(configured_path)
    path = requested_path
    if not path.exists() and path != DEFAULT_MEETING_OUTPUT_SKILL_PATH.resolve():
        default_path = DEFAULT_MEETING_OUTPUT_SKILL_PATH.resolve()
        if default_path.exists():
            path = default_path
    if not path.exists():
        return {
            "path": str(requested_path),
            "resolved_path": str(path),
            "name": "",
            "description": "",
            "metadata": {},
            "body": "",
        }
    text = path.read_text(encoding="utf-8")
    metadata, body = _split_frontmatter(text)
    return {
        "path": str(requested_path),
        "resolved_path": str(path),
        "name": str(metadata.get("name") or "").strip(),
        "description": str(metadata.get("description") or "").strip(),
        "metadata": dict(metadata),
        "body": body,
    }


def resolve_generated_meeting_output_dir(configured_path: str | Path | None = None) -> Path:
    text = str(configured_path or "").strip()
    if text:
        return resolve_workspace_path(text)
    return resolve_workspace_path("skills/generated")


def build_generated_meeting_output_skill_path(
    customization_request: str,
    *,
    output_dir: str | Path | None = None,
    base_signature: str = "",
) -> Path:
    directory = resolve_generated_meeting_output_dir(output_dir)
    normalized_request = str(customization_request or "").strip()
    slug = _slugify(normalized_request) or "customized-meeting-output"
    digest_source = f"{str(base_signature or '').strip()}::{normalized_request}"
    digest = hashlib.sha1(digest_source.encode("utf-8")).hexdigest()[:10]
    return directory / f"{slug}-{digest}" / "SKILL.md"


def build_interactive_meeting_output_skill_path(
    *,
    label: str = "",
    output_dir: str | Path | None = None,
    timestamp: datetime | None = None,
) -> Path:
    directory = resolve_generated_meeting_output_dir(output_dir)
    instant = timestamp or datetime.now()
    stamp = instant.strftime("%Y%m%d-%H%M%S")
    slug = _slugify(label) or "interactive-meeting-output"
    return directory / f"{slug}-{stamp}" / "SKILL.md"


def write_generated_meeting_output_skill(
    *,
    output_path: str | Path,
    name: str,
    description: str,
    body: str,
    metadata: dict[str, str] | None = None,
) -> Path:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    extra_metadata = {
        str(key).strip(): str(value).strip()
        for key, value in dict(metadata or {}).items()
        if str(key).strip() and str(value).strip() and str(key).strip() not in {"name", "description"}
    }
    metadata_lines = [
        f"  {key}: {_frontmatter_scalar(value)}\n"
        for key, value in extra_metadata.items()
    ]
    text = (
        "---\n"
        f"name: {_frontmatter_scalar(str(name or '').strip())}\n"
        f"description: {_frontmatter_scalar(str(description or '').strip())}\n"
        + ("metadata:\n" + "".join(metadata_lines) if metadata_lines else "")
        + "---\n\n"
        f"{str(body or '')}\n"
    )
    path.write_text(text, encoding="utf-8")
    return path.resolve()


def resolve_result_generation_policy(*skills: dict[str, object] | None) -> dict[str, object]:
    policy: dict[str, object] = {
        "result_block_order": list(DEFAULT_RESULT_BLOCK_ORDER),
        "show_open_questions": "always",
        "show_risk_signals": "never",
    }
    policy.update(DEFAULT_BLOCK_VISIBILITY)
    policy.update(DEFAULT_RENDERING_POLICY)
    policy.update(DEFAULT_LAYOUT_POLICY)
    policy.update(DEFAULT_RESULT_LABELS)
    policy.update(DEFAULT_EMPTY_MESSAGES)
    for skill in skills:
        metadata = dict((skill or {}).get("metadata") or {})
        block_order = _parse_result_block_order(str(metadata.get("result_block_order") or "").strip())
        if block_order:
            policy["result_block_order"] = block_order
        open_questions_visibility = _normalize_visibility_mode(str(metadata.get("show_open_questions") or "").strip())
        if open_questions_visibility:
            policy["show_open_questions"] = open_questions_visibility
        risk_signals_visibility = _normalize_visibility_mode(str(metadata.get("show_risk_signals") or "").strip())
        if risk_signals_visibility:
            policy["show_risk_signals"] = risk_signals_visibility
        result_block_order_mode = _normalize_result_block_order_mode(
            str(metadata.get("result_block_order_mode") or "").strip()
        )
        if result_block_order_mode:
            policy["result_block_order_mode"] = result_block_order_mode
        section_numbering = _normalize_section_numbering(
            str(metadata.get("section_numbering") or "").strip()
        )
        if section_numbering:
            policy["section_numbering"] = section_numbering
        for key in DEFAULT_BLOCK_VISIBILITY:
            visibility = _normalize_visibility_mode(str(metadata.get(key) or "").strip())
            if visibility:
                policy[key] = visibility
        for key in RENDERER_TOKEN_KEYS:
            value = str(metadata.get(key) or "").strip()
            if value:
                policy[key] = value
        for key in RENDERER_TEXT_KEYS:
            value = str(metadata.get(key) or "").strip()
            if value:
                policy[key] = value
        for key in RENDERER_COLOR_KEYS:
            value = _normalize_color_hex(str(metadata.get(key) or "").strip())
            if value:
                policy[key] = value
        for key in RENDERER_NUMERIC_KEYS:
            value = str(metadata.get(key) or "").strip()
            if value:
                policy[key] = value
        for key in RESULT_LIMIT_KEYS:
            parsed = _parse_positive_int(str(metadata.get(key) or "").strip())
            if parsed is not None:
                policy[key] = parsed
        for key in DEFAULT_RESULT_LABELS:
            value = str(metadata.get(key) or "").strip()
            if value:
                policy[key] = value
        for key in DEFAULT_EMPTY_MESSAGES:
            value = str(metadata.get(key) or "").strip()
            if value:
                policy[key] = value
        for key, raw_value in metadata.items():
            key_text = str(key or "").strip()
            if not key_text.startswith("renderer_"):
                continue
            if key_text in DEFAULT_RENDERING_POLICY:
                continue
            value = str(raw_value or "").strip()
            if value:
                policy[key_text] = value
        custom_css = _extract_renderer_custom_css(str((skill or {}).get("body") or ""))
        if custom_css:
            existing = str(policy.get("renderer_custom_css") or "").strip()
            policy["renderer_custom_css"] = "\n\n".join(part for part in (existing, custom_css) if part).strip()
    policy.update(SYSTEM_TRACE_POLICY)
    return policy


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    normalized = str(text or "").lstrip("\ufeff")
    lines = normalized.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, normalized.strip()

    metadata: dict[str, str] = {}
    body_start_index = 0
    nested_metadata = False
    for index in range(1, len(lines)):
        line = lines[index]
        if line.strip() == "---":
            body_start_index = index + 1
            break
        if nested_metadata and (line.startswith(" ") or line.startswith("\t")) and ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = _strip_frontmatter_scalar(value)
            continue
        nested_metadata = False
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key == "metadata" and not value.strip():
            nested_metadata = True
            continue
        metadata[key] = _strip_frontmatter_scalar(value)
    else:
        return {}, normalized.strip()

    body = "\n".join(lines[body_start_index:])
    return metadata, body


def _frontmatter_scalar(value: str) -> str:
    text = str(value or "").strip()
    escaped = text.replace("\\", "\\\\").replace("\"", "\\\"")
    return f"\"{escaped}\""


def _strip_frontmatter_scalar(value: str) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"\"", "'"}:
        text = text[1:-1]
    return text.replace("\\\"", "\"").replace("\\\\", "\\")


def _slugify(text: str) -> str:
    normalized = re.sub(r"\s+", "-", str(text or "").strip().lower())
    normalized = re.sub(r"[^a-z0-9가-힣-]+", "-", normalized)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized[:48]


def _parse_result_block_order(value: str) -> list[str]:
    items = [item.strip().lower() for item in str(value or "").split(",")]
    order: list[str] = []
    for item in items:
        if item in VALID_RESULT_BLOCKS and item not in order:
            order.append(item)
    return order


def _normalize_visibility_mode(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in VALID_VISIBILITY_MODES:
        return text
    return ""


def _normalize_result_block_order_mode(value: str) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if text in {"exact_only", "only"}:
        text = "exact"
    if text in VALID_RESULT_BLOCK_ORDER_MODES:
        return text
    return ""


def _normalize_section_numbering(value: str) -> str:
    text = str(value or "").strip().lower()
    if text in {"none", "unnumbered"}:
        text = "plain"
    if text in VALID_SECTION_NUMBERING_MODES:
        return text
    return ""


def _normalize_color_hex(value: str) -> str:
    text = str(value or "").strip().lstrip("#").upper()
    if len(text) == 3 and all(ch in "0123456789ABCDEF" for ch in text):
        text = "".join(ch * 2 for ch in text)
    if len(text) == 6 and all(ch in "0123456789ABCDEF" for ch in text):
        return text
    return ""


def _parse_positive_int(value: str) -> int | None:
    text = str(value or "").strip()
    if not text.isdigit():
        return None
    number = int(text)
    if number <= 0:
        return None
    return min(number, 50)


def _extract_renderer_custom_css(body: str) -> str:
    text = str(body or "")
    if not text:
        return ""
    blocks = re.findall(r"```css\s*(.*?)```", text, flags=re.IGNORECASE | re.DOTALL)
    cleaned = [block.strip() for block in blocks if str(block).strip()]
    return "\n\n".join(cleaned)
