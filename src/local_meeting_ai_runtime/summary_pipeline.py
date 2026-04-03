"""Session summarization helpers for meeting delegate sessions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

from .models import DelegateSession

KST = timezone(timedelta(hours=9), name="KST")


class DelegateSummaryPipeline:
    def build(self, session: DelegateSession) -> dict[str, Any]:
        transcript_participants: set[str] = set()
        unresolved_local_speaker = False
        unresolved_remote_speaker = False
        action_candidates: list[str] = []
        decision_candidates: list[str] = []
        open_questions: list[str] = []
        risk_signals: list[str] = []
        spoken_preview: list[str] = []
        chat_preview: list[str] = []
        interaction_timeline: list[str] = []
        source_breakdown: dict[str, int] = {}

        for chunk in session.transcript:
            raw_speaker = self._normalize(getattr(chunk, "speaker", "") or "")
            speaker = self._speaker_display_name(
                chunk.speaker,
                getattr(chunk, "metadata", {}),
                session=session,
                created_at=getattr(chunk, "created_at", None),
            )
            text = self._normalize(chunk.text)
            if not text:
                continue
            if self._is_local_placeholder_label(raw_speaker):
                unresolved_local_speaker = True
            if self._is_remote_placeholder_label(raw_speaker):
                unresolved_remote_speaker = True
            if self._is_local_placeholder_label(speaker):
                unresolved_local_speaker = True
            if self._is_remote_placeholder_label(speaker):
                unresolved_remote_speaker = True
            if not self._is_placeholder_participant_label(speaker):
                transcript_participants.add(speaker)
            source = self._classify_source(chunk.source)
            source_breakdown[source] = source_breakdown.get(source, 0) + 1
            line = f"{self._time_label_from_chunk(chunk)}{speaker}: {text}"
            interaction_timeline.append(f"[spoken] {line}")
            if source == "spoken_transcript":
                spoken_preview.append(line)
            else:
                chat_preview.append(line)
            if self._should_collect_transcript_intelligence(session, chunk, text):
                self._collect_meeting_intelligence(
                    text=text,
                    action_candidates=action_candidates,
                    decision_candidates=decision_candidates,
                    open_questions=open_questions,
                    risk_signals=risk_signals,
                )

        for turn in session.chat_history:
            speaker = self._speaker_display_name(
                turn.speaker or turn.role or "participant",
                session=session,
                created_at=getattr(turn, "created_at", None),
            )
            text = self._normalize(turn.text)
            if not text:
                continue
            if not self._is_placeholder_participant_label(speaker):
                transcript_participants.add(speaker)
            line = f"{self._time_label(turn.created_at)}{speaker}: {text}"
            interaction_timeline.append(f"[chat] {line}")
            if line not in chat_preview:
                chat_preview.append(line)
            if turn.role != "bot" and len(text) <= 280:
                self._collect_meeting_intelligence(
                    text=text,
                    action_candidates=action_candidates,
                    decision_candidates=decision_candidates,
                    open_questions=open_questions,
                    risk_signals=risk_signals,
                )

        participants = self._session_participants(
            session,
            transcript_participants=transcript_participants,
            unresolved_local_speaker=unresolved_local_speaker,
            unresolved_remote_speaker=unresolved_remote_speaker,
        )
        packet = {
            "meeting": {
                "session_id": session.session_id,
                "meeting_id": session.meeting_id,
                "meeting_uuid": session.meeting_uuid,
                "meeting_number": session.meeting_number,
                "meeting_topic": session.meeting_topic,
                "delegate_mode": session.delegate_mode,
                "status": session.status,
            },
            "participants": participants,
            "counts": {
                "input_events": len(session.input_timeline),
                "transcript_lines": len(session.transcript),
                "chat_turns": len(session.chat_history),
                "workspace_event_count": len(session.workspace_events),
            },
            "source_breakdown": source_breakdown,
            "meeting_intelligence": {
                "decisions": decision_candidates[:10],
                "open_questions": open_questions[:10],
                "risk_signals": risk_signals[:10],
            },
            "spoken_transcript_preview": spoken_preview[-10:],
            "chat_preview": chat_preview[-10:],
            "interaction_timeline_preview": interaction_timeline[-14:],
            "action_candidates": action_candidates[:10],
        }
        packet["briefing"] = self.build_briefing(session, packet=packet)
        return packet

    def build_briefing(
        self,
        session: DelegateSession,
        *,
        packet: dict[str, Any] | None = None,
        ai_result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        packet = dict(packet or {})
        ai_result = dict(ai_result or {})
        intelligence = dict(packet.get("meeting_intelligence") or {})
        participants = self._clean_list(packet.get("participants"))
        action_items = self._clean_list(session.action_items or ai_result.get("action_items") or packet.get("action_candidates"))
        decisions = self._clean_list(ai_result.get("decisions") or intelligence.get("decisions"))
        open_questions = self._clean_list(ai_result.get("open_questions") or intelligence.get("open_questions"))
        risk_signals = self._clean_list(ai_result.get("risk_signals") or intelligence.get("risk_signals"))
        executive_summary = self._normalize(str(ai_result.get("summary") or "")) or self._normalize(str(session.summary or "")) or self._fallback_summary(session)
        sections = self._sections_from_ai(session, ai_result)
        if not sections:
            sections = self._fallback_sections(
                session,
                executive_summary=executive_summary,
                decisions=decisions,
                action_items=action_items,
                open_questions=open_questions,
                risk_signals=risk_signals,
            )
        return {
            "title": self._resolved_title(session, ai_result=ai_result, sections=sections, executive_summary=executive_summary),
            "meeting_datetime_label": self._meeting_datetime_label(session),
            "executive_summary": executive_summary or "회의 전체 요약이 아직 생성되지 않았습니다.",
            "sections": sections[:6],
            "decisions": decisions[:10],
            "action_items": action_items[:10],
            "open_questions": open_questions[:10],
            "participants": participants[:20],
            "risk_signals": risk_signals[:10],
        }

    def render_summary_markdown(self, session: DelegateSession) -> str:
        packet = dict(session.summary_packet or {})
        fresh_packet: dict[str, Any] | None = None
        if not packet:
            packet = self.build(session)
        elif self._participants_need_refresh(packet):
            fresh_packet = self.build(session)
            packet["participants"] = list(fresh_packet.get("participants") or [])
        briefing = dict(packet.get("briefing") or self.build_briefing(session, packet=packet))
        if self._participants_need_refresh({"participants": briefing.get("participants")}):
            if fresh_packet is None:
                fresh_packet = self.build(session)
            briefing["participants"] = list((fresh_packet or {}).get("participants") or [])
        title = self._display_title(session, briefing)
        meeting_datetime = str(briefing.get("meeting_datetime_label") or self._meeting_datetime_label(session)).strip()
        executive_summary = str(briefing.get("executive_summary") or session.summary or "").strip()
        sections = self._enrich_sections_for_display(session, list(briefing.get("sections") or []))
        decisions = self._clean_list(briefing.get("decisions"))
        action_items = self._clean_list(briefing.get("action_items"))
        open_questions = self._clean_list(briefing.get("open_questions"))
        participants = self._clean_list(briefing.get("participants"))
        participant_text = ", ".join(participants) if participants else "미확인"

        lines = [
            f"# {title}",
            "",
            "## 회의 개요",
            "",
            f"**회의 일시**: {meeting_datetime or '미확인'}",
            "",
            f"**작성 주체**: {session.bot_display_name}",
            "",
            f"**세션 ID**: {session.session_id}",
            "",
            f"**참석자**: {participant_text}",
            "",
            "## 회의 전체 요약",
            "",
            executive_summary or "회의 전체 요약이 아직 생성되지 않았습니다.",
            "",
            "## 핵심 논의 주제",
            "",
        ]
        if sections:
            for idx, section in enumerate(sections, start=1):
                heading = self._normalize(str(section.get("heading") or f"주제 {idx}")) or f"주제 {idx}"
                summary = self._normalize(str(section.get("summary") or "")) or "요약 내용이 없습니다."
                timestamp_refs = self._clean_list(section.get("timestamp_refs"))
                raised_by = self._normalize(str(section.get("raised_by") or ""))
                speakers = self._clean_list(section.get("speakers"))
                lines.extend([f"### {idx}. {heading}", "", summary, ""])
                if raised_by:
                    lines.append(f"- 제기자: {raised_by}")
                if len(speakers) >= 2:
                    lines.append(f"- 주요 화자: {', '.join(speakers[:3])}")
                if timestamp_refs:
                    formatted_refs = ", ".join(f"`{item}`" for item in timestamp_refs[:4])
                    lines.append(f"- 타임스탬프: {formatted_refs}")
                lines.append("")
        else:
            lines.extend(["- 핵심 논의 주제가 아직 정리되지 않았습니다.", ""])

        lines.extend(["## 결정사항", ""])
        if decisions:
            lines.extend(f"- {item}" for item in decisions)
        else:
            lines.append("- 아직 확정된 결정사항이 없습니다.")

        lines.extend(["", "## 액션 아이템", ""])
        if action_items:
            lines.extend(f"- {item}" for item in action_items)
        else:
            lines.append("- 추출된 액션 아이템이 없습니다.")

        lines.extend(["", "## 열린 질문", ""])
        if open_questions:
            lines.extend(f"- {item}" for item in open_questions)
        else:
            lines.append("- 현재 남은 열린 질문이 없습니다.")

        lines.extend(["", "## 메모", "", "세부 음성 전사와 채팅 원문은 별도 export 파일에서 확인할 수 있습니다."])
        return "\n".join(lines).strip() + "\n"

    def _enrich_sections_for_display(self, session: DelegateSession, sections: list[Any]) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for item in sections:
            if not isinstance(item, dict):
                continue
            section = dict(item)
            refs = self._clean_list(section.get("timestamp_refs"))
            if refs:
                speaker_rankings = self._speaker_rankings_from_timestamp_refs(session, refs)
                speaker_candidates = [speaker for speaker, _ in speaker_rankings]
                if speaker_candidates:
                    if len(speaker_rankings) == 1 or speaker_rankings[0][1] >= (speaker_rankings[1][1] * 1.35):
                        section["raised_by"] = speaker_candidates[0]
                    else:
                        section.pop("raised_by", None)
                    if len(speaker_candidates) >= 2:
                        section["speakers"] = speaker_candidates[:3]
                    else:
                        section.pop("speakers", None)
                else:
                    if self._is_placeholder_participant_label(self._normalize(str(section.get("raised_by") or ""))):
                        section.pop("raised_by", None)
                    section.pop("speakers", None)
            enriched.append(section)
        return enriched

    def render_transcript_markdown(self, session: DelegateSession) -> str:
        briefing = dict((session.summary_packet or {}).get("briefing") or {})
        title = self._display_title(session, briefing) if briefing else self._resolved_title(session)
        lines = [
            f"# {title} - 전사 원문",
            "",
            f"- 세션 ID: `{session.session_id}`",
            f"- 회의 번호: {session.meeting_id or session.meeting_number or '미확인'}",
            f"- 모드: {session.delegate_mode}",
            f"- 입력 이벤트 수: {len(session.input_timeline)}",
            f"- 전사 라인 수: {len(session.transcript)}",
            f"- 채팅 수: {len(session.chat_history)}",
            "",
            "## 음성 전사",
            "",
        ]
        if not session.transcript:
            lines.append("_아직 수집된 음성 전사가 없습니다._")
        else:
            for idx, chunk in enumerate(session.transcript, start=1):
                speaker = self._speaker_display_name(
                    chunk.speaker,
                    getattr(chunk, "metadata", {}),
                    session=session,
                    created_at=getattr(chunk, "created_at", None),
                )
                text = self._normalize(chunk.text)
                source = self._source_label(chunk)
                lines.append(f"{idx}. **{speaker}** [{source}]")
                lines.append(f"   {text}")

        lines.extend(["", "## 회의 채팅", ""])
        if not session.chat_history:
            lines.append("_아직 수집된 회의 채팅이 없습니다._")
        else:
            for idx, turn in enumerate(session.chat_history, start=1):
                speaker = self._speaker_display_name(
                    turn.speaker or turn.role or "participant",
                    session=session,
                    created_at=getattr(turn, "created_at", None),
                )
                text = self._normalize(turn.text)
                source = self._normalize(turn.source or "meeting_chat") or "meeting_chat"
                lines.append(f"{idx}. **{speaker}** [{source}]")
                lines.append(f"   {text}")
        return "\n".join(lines).strip() + "\n"

    def _participants_need_refresh(self, packet: dict[str, Any]) -> bool:
        participants = [self._normalize(str(item)) for item in list(packet.get("participants") or []) if self._normalize(str(item))]
        if not participants:
            return False
        if any(self._is_internal_placeholder_participant_label(item) for item in participants):
            return True
        placeholder_only = [item for item in participants if self._is_placeholder_participant_label(item)]
        return bool(len(placeholder_only) == len(participants))

    def _display_title(self, session: DelegateSession, briefing: dict[str, Any]) -> str:
        title = self._normalize(str(briefing.get("title") or ""))
        if title and not self._looks_like_broken_title(title):
            return title
        section_title = self._section_title_fallback(list(briefing.get("sections") or []))
        if section_title:
            return section_title
        summary_head = self._first_sentence(self._normalize(str(briefing.get("executive_summary") or "")))
        if summary_head and not self._looks_like_broken_title(summary_head):
            return summary_head[:80]
        return self._resolved_title(session)

    def _sections_from_ai(self, session: DelegateSession, ai_result: dict[str, Any]) -> list[dict[str, Any]]:
        raw_sections = ai_result.get("sections")
        if not isinstance(raw_sections, list):
            return []
        sections: list[dict[str, Any]] = []
        for item in raw_sections:
            if not isinstance(item, dict):
                continue
            heading = self._normalize(str(item.get("heading") or ""))
            summary = self._normalize(str(item.get("summary") or ""))
            refs = [self._normalize(str(value)) for value in list(item.get("timestamp_refs") or []) if self._normalize(str(value))]
            if not heading or not summary:
                continue
            section = {"heading": heading, "summary": summary, "timestamp_refs": refs[:4]}
            raised_by = self._speaker_from_timestamp_refs(session, refs)
            if raised_by:
                section["raised_by"] = raised_by
            sections.append(section)
        return sections

    def _fallback_sections(self, session: DelegateSession, *, executive_summary: str, decisions: list[str], action_items: list[str], open_questions: list[str], risk_signals: list[str]) -> list[dict[str, Any]]:
        records = self._interaction_records(session)
        sections: list[dict[str, Any]] = []
        definitions = [
            ("회의 흐름 요약", executive_summary, records[:3]),
            ("결정과 후속 작업", self._join_sentences(decisions[:3] + action_items[:3]), records[2:6]),
            ("남은 질문과 리스크", self._join_sentences(open_questions[:3] + risk_signals[:3]), records[-4:]),
        ]
        for heading, summary, slice_records in definitions:
            if not summary:
                continue
            section = {"heading": heading, "summary": summary, "timestamp_refs": self._timeline_timestamp_refs(slice_records)}
            raised_by = self._speaker_from_records(session, slice_records)
            if raised_by:
                section["raised_by"] = raised_by
            sections.append(section)
        if not sections:
            records = records[:4]
            section = {"heading": "회의 메모", "summary": self._fallback_summary(session), "timestamp_refs": self._timeline_timestamp_refs(records)}
            raised_by = self._speaker_from_records(session, records)
            if raised_by:
                section["raised_by"] = raised_by
            sections.append(section)
        return sections[:6]

    def _interaction_records(self, session: DelegateSession) -> list[dict[str, str]]:
        records: list[dict[str, str]] = []
        for chunk in session.transcript:
            speaker = self._speaker_display_name(
                chunk.speaker,
                getattr(chunk, "metadata", {}),
                session=session,
                created_at=getattr(chunk, "created_at", None),
            )
            text = self._normalize(chunk.text)
            if text:
                records.append({"timestamp_ref": self._time_ref_from_chunk(chunk), "speaker": speaker, "text": text})
        for turn in session.chat_history:
            speaker = self._speaker_display_name(
                turn.speaker or turn.role or "participant",
                session=session,
                created_at=getattr(turn, "created_at", None),
            )
            text = self._normalize(turn.text)
            if text:
                records.append({"timestamp_ref": self._time_ref(turn.created_at), "speaker": speaker, "text": text})
        return records

    def _timeline_timestamp_refs(self, records: list[dict[str, str]]) -> list[str]:
        refs: list[str] = []
        for item in records:
            ref = self._normalize(item.get("timestamp_ref") or "")
            if ref and ref not in refs:
                refs.append(ref)
        return refs[:4]

    def _speaker_from_timestamp_refs(self, session: DelegateSession, refs: list[str]) -> str:
        candidates = self._speaker_candidates_from_timestamp_refs(session, refs)
        return candidates[0] if candidates else ""

    def _speaker_candidates_from_timestamp_refs(self, session: DelegateSession, refs: list[str]) -> list[str]:
        return [speaker for speaker, _ in self._speaker_rankings_from_timestamp_refs(session, refs)]

    def _speaker_rankings_from_timestamp_refs(self, session: DelegateSession, refs: list[str]) -> list[tuple[str, float]]:
        cleaned_refs = self._clean_list(refs)
        if not cleaned_refs:
            return []
        records = [
            item
            for item in self._interaction_records(session)
            if self._normalize(item.get("speaker") or "")
            and self._normalize(item.get("speaker") or "") != session.bot_display_name
        ]
        if not records:
            preferred = self._preferred_local_speaker_name(session)
            return [(preferred, 1.0)] if preferred else []

        exact_votes: list[str] = []
        proximity_votes: list[tuple[str, float]] = []
        for ref in cleaned_refs:
            for item in records:
                if item.get("timestamp_ref") == ref:
                    speaker = self._normalize(item.get("speaker") or "")
                    if speaker:
                        exact_votes.append(speaker)
            target_seconds = self._time_reference_seconds(ref)
            if target_seconds is None:
                continue
            nearest_by_speaker: dict[str, float] = {}
            for item in records:
                item_seconds = self._time_reference_seconds(item.get("timestamp_ref") or "")
                if item_seconds is None:
                    continue
                delta = abs(item_seconds - target_seconds)
                if delta > 5.0:
                    continue
                speaker = self._normalize(item.get("speaker") or "")
                if not speaker:
                    continue
                previous = nearest_by_speaker.get(speaker)
                if previous is None or delta < previous:
                    nearest_by_speaker[speaker] = delta
            for speaker, delta in nearest_by_speaker.items():
                proximity_votes.append((speaker, delta))

        all_votes = list(exact_votes) + [speaker for speaker, _ in proximity_votes]
        named_votes = [speaker for speaker in all_votes if not self._is_placeholder_participant_label(speaker)]
        if named_votes:
            exact_votes = [speaker for speaker in exact_votes if not self._is_placeholder_participant_label(speaker)]
            proximity_votes = [(speaker, delta) for speaker, delta in proximity_votes if not self._is_placeholder_participant_label(speaker)]
        elif all_votes and all(self._is_placeholder_participant_label(speaker) for speaker in all_votes):
            preferred = self._preferred_local_speaker_name(session)
            return [(preferred, 1.0)] if preferred else []

        scores: dict[str, float] = {}
        first_seen: dict[str, int] = {}
        for idx, speaker in enumerate(exact_votes):
            scores[speaker] = scores.get(speaker, 0.0) + 3.0
            first_seen.setdefault(speaker, idx)
        start_index = len(first_seen)
        for idx, (speaker, delta) in enumerate(proximity_votes):
            weight = max(0.25, 1.5 - (delta / 5.0))
            scores[speaker] = scores.get(speaker, 0.0) + weight
            first_seen.setdefault(speaker, start_index + idx)

        if scores:
            ranked = sorted(scores.items(), key=lambda item: (-item[1], first_seen.get(item[0], 10_000), item[0]))
            return [(speaker, score) for speaker, score in ranked[:3]]

        preferred = self._preferred_local_speaker_name(session)
        if preferred:
            return [(preferred, 1.0)]
        return []

    def _speaker_from_records(self, session: DelegateSession, records: list[dict[str, str]]) -> str:
        for item in records:
            speaker = self._normalize(item.get("speaker") or "")
            if speaker and speaker != session.bot_display_name:
                return speaker
        return ""

    def _session_participants(
        self,
        session: DelegateSession,
        *,
        transcript_participants: set[str],
        unresolved_local_speaker: bool,
        unresolved_remote_speaker: bool,
    ) -> list[str]:
        participants: list[str] = []
        if session.bot_display_name:
            participants.append(session.bot_display_name)
        for label in self._participant_state_labels(session):
            self._append_unique(participants, label)
        for turn in session.chat_history:
            if turn.role == "bot":
                continue
            speaker = self._speaker_display_name(
                turn.speaker or turn.role or "participant",
                session=session,
                created_at=getattr(turn, "created_at", None),
            )
            if not self._is_placeholder_participant_label(speaker):
                self._append_unique(participants, speaker)

        for speaker in sorted(transcript_participants):
            self._append_unique(participants, speaker)

        named_humans = [item for item in participants if item != session.bot_display_name]
        if unresolved_local_speaker and not named_humans:
            self._append_unique(participants, "로컬 발화자")
        if unresolved_remote_speaker:
            self._append_unique(participants, "원격 참가자(이름 미확인)")
        return participants[:20]

    def _participant_state_labels(self, session: DelegateSession) -> list[str]:
        labels: list[str] = []
        entries = list(session.input_timeline) + list(session.workspace_events)
        for entry in entries:
            entry_type = self._normalize(getattr(entry, "input_type", "") or getattr(entry, "event_type", ""))
            if "participant_state" not in entry_type:
                continue
            metadata = dict(getattr(entry, "metadata", {}) or {})
            raw_value = metadata.get("raw")
            raw = dict(raw_value) if isinstance(raw_value, dict) else {}
            candidate = (
                self._normalize(getattr(entry, "speaker", "") or "")
                or self._normalize(str(raw.get("displayName") or raw.get("participantName") or raw.get("userName") or ""))
                or self._normalize(str(metadata.get("participant") or ""))
            )
            if not candidate or self._is_placeholder_participant_label(candidate):
                continue
            self._append_unique(labels, candidate)
        return labels

    def _resolved_title(self, session: DelegateSession, *, ai_result: dict[str, Any] | None = None, sections: list[dict[str, Any]] | None = None, executive_summary: str | None = None) -> str:
        ai_result = dict(ai_result or {})
        sections = list(sections or [])
        for candidate in (self._normalize(str(ai_result.get("title") or "")), self._normalize(str(session.meeting_topic or ""))):
            if candidate and not self._looks_like_broken_title(candidate):
                return candidate
        section_title = self._section_title_fallback(sections)
        if section_title:
            return section_title
        summary_head = self._first_sentence(self._normalize(str(executive_summary or "")))
        if summary_head and not self._looks_like_broken_title(summary_head):
            return summary_head[:80]
        if session.meeting_number:
            return f"Zoom 회의 {session.meeting_number}"
        if session.meeting_id:
            return f"회의 {session.meeting_id}"
        return "회의 요약"

    def _looks_like_broken_title(self, value: str) -> bool:
        text = self._normalize(value)
        if not text:
            return True
        if "�" in text or text.count("?") >= 3:
            return True
        lowered = text.lower()
        if lowered in {"zoom", "zoom meeting", "zoom 회의", "meeting", "회의"}:
            return True
        return lowered.startswith("zoom") and re.fullmatch(r"zoom[\s\-\:_?？!./]*", lowered) is not None

    def _is_generic_title_candidate(self, value: str) -> bool:
        lowered = self._normalize(value).lower()
        return lowered in {
            "회의 흐름 요약",
            "결정과 후속 작업",
            "남은 질문과 리스크",
            "회의 메모",
            "회의 전체 요약",
            "결정사항",
            "액션 아이템",
            "열린 질문",
        }

    def _section_title_fallback(self, sections: list[dict[str, Any]] | list[Any]) -> str:
        headings: list[str] = []
        for item in sections:
            if not isinstance(item, dict):
                continue
            heading = self._normalize(str(item.get("heading") or ""))
            if not heading or self._looks_like_broken_title(heading) or self._is_generic_title_candidate(heading):
                continue
            if heading not in headings:
                headings.append(heading)
        if not headings:
            return ""
        if len(headings) == 1:
            return headings[0]
        combined = f"{headings[0]} · {headings[1]}"
        return combined[:80]

    def _fallback_summary(self, session: DelegateSession) -> str:
        recent_lines = self._interaction_lines(session)[-4:]
        return self._join_sentences(recent_lines) if recent_lines else "회의 핵심 내용이 아직 정리되지 않았습니다."

    def _interaction_lines(self, session: DelegateSession) -> list[str]:
        lines: list[str] = []
        for item in self._interaction_records(session):
            ref = self._normalize(item.get("timestamp_ref") or "")
            speaker = self._normalize(item.get("speaker") or "")
            text = self._normalize(item.get("text") or "")
            prefix = f"[{ref}] " if ref else ""
            if speaker and text:
                lines.append(f"{prefix}{speaker}: {text}".strip())
        return lines

    def _meeting_datetime_label(self, session: DelegateSession) -> str:
        raw = str(session.updated_at or session.created_at or "").strip()
        if not raw:
            return ""
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return raw
        return parsed.astimezone(KST).strftime("%Y-%m-%d %H:%M KST")

    def _join_sentences(self, items: list[str]) -> str:
        cleaned = [self._normalize(item) for item in items if self._normalize(item)]
        return " ".join(cleaned[:6]) if cleaned else ""

    def _clean_list(self, values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        result: list[str] = []
        for item in values:
            self._append_unique(result, self._normalize(str(item)))
        return result

    def _append_unique(self, values: list[str], item: str) -> None:
        cleaned = self._normalize(item)
        if cleaned and cleaned not in values:
            values.append(cleaned)

    def _should_collect_transcript_intelligence(self, session: DelegateSession, chunk: Any, text: str) -> bool:
        if len(text) > 280:
            return False
        speaker = self._normalize(getattr(chunk, "speaker", "") or "")
        if speaker and speaker == self._normalize(session.bot_display_name):
            return False
        source = self._normalize(getattr(chunk, "source", "") or "")
        return "bot" not in source

    def _speaker_display_name(
        self,
        speaker: str,
        metadata: dict[str, Any] | None = None,
        *,
        session: DelegateSession | None = None,
        created_at: str | None = None,
        resolve_local_alias: bool = True,
    ) -> str:
        meta = dict(metadata or {})
        for key in ("speaker_name", "speaker_display_name", "participant_name", "participantName", "display_name", "displayName", "userName", "user_name", "name"):
            candidate = self._normalize(str(meta.get(key) or ""))
            if candidate and not self._is_placeholder_participant_label(candidate):
                return candidate
        normalized = self._normalize(speaker or "unknown") or "unknown"
        lowered = normalized.lower()
        zoom_name = self._zoom_active_speaker_name(session, metadata=meta, created_at=created_at)
        if zoom_name:
            return zoom_name
        preferred_local_name = self._preferred_local_speaker_name(session) if resolve_local_alias else None
        if preferred_local_name and self._is_local_placeholder_label(normalized):
            return preferred_local_name
        if lowered not in {"participant", "meeting_audio", "unknown"} and not self._is_known_internal_speaker(lowered):
            return normalized
        audio_source = self._normalize(str(meta.get("audio_source") or meta.get("capture_mode") or ""))
        if self._is_local_placeholder_label(normalized) or audio_source == "microphone":
            return "로컬 발화자"
        if self._is_remote_placeholder_label(normalized) or audio_source == "system":
            return "원격 참가자(이름 미확인)"
        if lowered == "local_system_audio":
            return "회의 출력 음성"
        return audio_source or normalized

    def _zoom_active_speaker_name(
        self,
        session: DelegateSession | None,
        *,
        metadata: dict[str, Any],
        created_at: str | None,
    ) -> str | None:
        if session is None:
            return None
        events = self._zoom_active_speaker_events(session)
        if not events:
            return None
        target_offset = self._session_offset_seconds(metadata)
        if target_offset is not None:
            best_name: str | None = None
            best_delta: float | None = None
            for event in events:
                offset = event.get("offset_seconds")
                if offset is None:
                    continue
                delta = abs(offset - target_offset)
                if delta > 3.0:
                    continue
                if best_delta is None or delta < best_delta:
                    best_delta = delta
                    best_name = self._normalize(str(event.get("name") or ""))
            if best_name:
                return best_name
        target = self._event_datetime(created_at) or self._event_datetime(str(metadata.get("captured_at") or ""))
        if target is None:
            return None
        best_name: str | None = None
        best_delta: float | None = None
        for event in events:
            name = self._normalize(str(event.get("name") or ""))
            when = event.get("when")
            if not name or when is None:
                continue
            delta = abs((when - target).total_seconds())
            if delta > 8.0:
                continue
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_name = name
        return best_name

    def _zoom_active_speaker_events(self, session: DelegateSession) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for entry in session.input_timeline:
            if entry.input_type != "participant_state":
                continue
            metadata = dict(entry.metadata or {})
            if self._normalize(str(metadata.get("event") or "")).lower() != "active-speaker":
                continue
            raw_value = metadata.get("raw")
            raw = dict(raw_value) if isinstance(raw_value, dict) else {}
            name = (
                self._normalize(str(entry.speaker or ""))
                or self._normalize(str(raw.get("displayName") or raw.get("userName") or raw.get("participantName") or ""))
            )
            if not name or name == session.bot_display_name:
                continue
            when = self._event_datetime(str(entry.created_at or "")) or self._event_datetime(str(metadata.get("created_at") or ""))
            events.append(
                {
                    "name": name,
                    "user_id": self._normalize(str(raw.get("userId") or metadata.get("userId") or "")),
                    "when": when,
                    "offset_seconds": self._session_offset_seconds(metadata),
                }
            )
        return events

    def _session_offset_seconds(self, metadata: dict[str, Any] | None) -> float | None:
        meta = dict(metadata or {})
        for key in ("session_offset_seconds", "session_start_offset_seconds"):
            value = meta.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _event_datetime(self, value: str | None) -> datetime | None:
        text = self._normalize(str(value or ""))
        if not text:
            return None
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    def _preferred_local_speaker_name(self, session: DelegateSession | None) -> str | None:
        if session is None:
            return None
        names = [item for item in self._named_human_participants(session) if item != session.bot_display_name]
        return names[0] if len(names) == 1 else None

    def _named_human_participants(self, session: DelegateSession) -> list[str]:
        names: list[str] = []
        for label in self._participant_state_labels(session):
            clean = self._normalize(label)
            if clean and clean != session.bot_display_name and not self._is_placeholder_participant_label(clean):
                self._append_unique(names, clean)
        for turn in session.chat_history:
            if turn.role == "bot":
                continue
            speaker = self._speaker_display_name(
                turn.speaker or turn.role or "participant",
                session=session,
                created_at=getattr(turn, "created_at", None),
                resolve_local_alias=False,
            )
            if speaker and speaker != session.bot_display_name and not self._is_placeholder_participant_label(speaker):
                self._append_unique(names, speaker)
        return names

    def _is_known_internal_speaker(self, lowered: str) -> bool:
        return lowered in {"participant", "meeting_audio", "unknown", "local_user", "remote_participant", "local_microphone", "local_system_audio", "meeting_output"} or lowered.startswith("local_user_") or lowered.startswith("remote_participant_") or lowered.startswith("local_microphone_")

    def _is_placeholder_participant_label(self, value: str) -> bool:
        text = self._normalize(value)
        return (not text) or self._is_local_placeholder_label(text) or self._is_remote_placeholder_label(text) or text.lower() in {"participant", "meeting_audio", "unknown", "local_system_audio", "meeting_output"}

    def _is_internal_placeholder_participant_label(self, value: str) -> bool:
        lowered = self._normalize(value).lower()
        return lowered in {
            "participant",
            "meeting_audio",
            "unknown",
            "local_user",
            "remote_participant",
            "local_microphone",
            "local_system_audio",
            "meeting_output",
        } or lowered.startswith("local_user_") or lowered.startswith("remote_participant_") or lowered.startswith("local_microphone_")

    def _is_local_placeholder_label(self, value: str) -> bool:
        lowered = self._normalize(value).lower()
        return lowered in {"local_user", "local_microphone", "로컬 발화자"} or lowered.startswith("local_user_") or lowered.startswith("local_microphone_") or lowered.startswith("로컬 발화자 ")

    def _is_remote_placeholder_label(self, value: str) -> bool:
        lowered = self._normalize(value).lower()
        return lowered in {"remote_participant", "회의 출력 음성", "원격 참가자", "원격 참가자(이름 미확인)"} or lowered.startswith("remote_participant_") or lowered.startswith("원격 참가자 ")

    def _source_label(self, chunk: Any) -> str:
        source = self._normalize(getattr(chunk, "source", "") or "manual") or "manual"
        label = self._time_label_from_chunk(chunk).strip()
        return source if not label else f"{source} | {label.strip('[] ')}"

    def _time_ref_from_chunk(self, chunk: Any) -> str:
        metadata = dict(getattr(chunk, "metadata", {}) or {})
        start_offset = metadata.get("session_start_offset_seconds")
        if start_offset is not None:
            return self._clock_label(start_offset)
        start_offset = metadata.get("start_offset_seconds")
        if start_offset is not None:
            return self._clock_label(start_offset)
        return self._normalize(str(getattr(chunk, "created_at", None) or ""))

    def _time_label_from_chunk(self, chunk: Any) -> str:
        return self._time_label(self._time_ref_from_chunk(chunk))

    def _time_ref(self, value: Any) -> str:
        return self._normalize(str(value or ""))

    def _time_reference_seconds(self, value: Any) -> float | None:
        text = self._time_ref(value)
        if not text:
            return None
        if re.fullmatch(r"\d{2}:\d{2}\.\d{2}", text):
            minutes_text, seconds_text = text.split(":", 1)
            try:
                return (int(minutes_text) * 60) + float(seconds_text)
            except ValueError:
                return None
        if re.fullmatch(r"\d{2}:\d{2}:\d{2}\.\d{2}", text):
            hours_text, minutes_text, seconds_text = text.split(":", 2)
            try:
                return (int(hours_text) * 3600) + (int(minutes_text) * 60) + float(seconds_text)
            except ValueError:
                return None
        return None

    def _time_label(self, value: Any) -> str:
        text = self._time_ref(value)
        return f"[{text}] " if text else ""

    def _clock_label(self, seconds: Any) -> str:
        try:
            total = max(float(seconds), 0.0)
        except (TypeError, ValueError):
            return ""
        minutes = int(total // 60)
        remainder = total - (minutes * 60)
        if minutes >= 60:
            hours = minutes // 60
            minutes = minutes % 60
            return f"{hours:02d}:{minutes:02d}:{remainder:05.2f}"
        return f"{minutes:02d}:{remainder:05.2f}"

    def _collect_meeting_intelligence(self, *, text: str, action_candidates: list[str], decision_candidates: list[str], open_questions: list[str], risk_signals: list[str]) -> None:
        action = self._action_candidate(text)
        if action and action not in action_candidates:
            action_candidates.append(action)
        decision = self._decision_candidate(text)
        if decision and decision not in decision_candidates:
            decision_candidates.append(decision)
        question = self._question_candidate(text)
        if question and question not in open_questions:
            open_questions.append(question)
        risk = self._risk_candidate(text)
        if risk and risk not in risk_signals:
            risk_signals.append(risk)

    def _classify_source(self, source: str) -> str:
        normalized = self._normalize(source).lower()
        if normalized.startswith("local_") or normalized.startswith("platform_audio") or normalized == "manual" or "transcript" in normalized or "audio" in normalized:
            return "spoken_transcript"
        if "chat" in normalized:
            return "workspace_chat"
        return "other"

    def _action_candidate(self, text: str) -> str | None:
        return text if any(token in text.lower() for token in ("action", "todo", "follow up", "next step", "need to", "should", "해야", "후속", "액션")) else None

    def _decision_candidate(self, text: str) -> str | None:
        return text if any(token in text.lower() for token in ("decided", "we will", "we'll", "confirmed", "approved", "정했다", "결정", "확정", "확인")) else None

    def _question_candidate(self, text: str) -> str | None:
        if "?" in text:
            return text
        return text if any(token in text.lower() for token in ("question", "need to know", "unclear", "whether", "what if", "어떻게", "무엇", "언제", "가능한가")) else None

    def _risk_candidate(self, text: str) -> str | None:
        return text if any(token in text.lower() for token in ("risk", "blocker", "issue", "problem", "delay", "concern", "리스크", "문제", "이슈", "지연", "막자")) else None

    def _first_sentence(self, text: str) -> str:
        normalized = self._normalize(text)
        return re.split(r"(?<=[.!?])\s+", normalized, maxsplit=1)[0].strip() if normalized else ""

    def _normalize(self, value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()
