import unittest

from datetime import UTC, datetime, timedelta

from lush_local_ai_launcher.launcher import (
    _friendly_meeting_topic,
    _friendly_session_activity,
    _friendly_session_state_label,
    _session_is_stale_status_candidate,
    _select_status_board_session,
)
from local_meeting_ai_runtime.models import DelegateSession


class LauncherStatusBoardTests(unittest.TestCase):
    def test_user_progress_message_is_preferred(self) -> None:
        session = DelegateSession(
            session_id="abc123",
            delegate_mode="answer_on_ask",
            bot_display_name="WooBIN_bot",
            status="active",
        )
        session.ai_state["user_progress"] = {
            "stage": "generating_images",
            "message": "이미지를 만드는 중입니다.",
            "detail": "1/3번째 이미지를 준비하고 있습니다.",
        }

        message, detail = _friendly_session_activity(session)

        self.assertEqual(message, "이미지를 만드는 중입니다.")
        self.assertEqual(detail, "1/3번째 이미지를 준비하고 있습니다.")

    def test_active_session_uses_friendly_capture_message(self) -> None:
        session = DelegateSession(
            session_id="abc123",
            delegate_mode="answer_on_ask",
            bot_display_name="WooBIN_bot",
            status="active",
        )

        message, detail = _friendly_session_activity(session)

        self.assertEqual(message, "회의 음성을 수집하고 있습니다.")
        self.assertIn("자동으로 결과물을 준비", detail)
        self.assertEqual(_friendly_session_state_label(session), "회의 진행 중")

    def test_select_status_board_session_prefers_in_progress_session(self) -> None:
        completed = DelegateSession(
            session_id="done001",
            delegate_mode="answer_on_ask",
            bot_display_name="WooBIN_bot",
            meeting_topic="완료된 회의",
            status="completed",
            updated_at="2026-04-17T01:00:00+00:00",
        )
        active = DelegateSession(
            session_id="live001",
            delegate_mode="answer_on_ask",
            bot_display_name="WooBIN_bot",
            meeting_topic="진행 중인 회의",
            status="active",
            updated_at="2026-04-17T00:00:00+00:00",
        )

        selected = _select_status_board_session([completed, active])

        self.assertIsNotNone(selected)
        self.assertEqual(selected.session_id, "live001")

    def test_stale_shell_stub_session_is_ignored(self) -> None:
        stale = DelegateSession(
            session_id="ghost001",
            delegate_mode="answer_on_ask",
            bot_display_name="WooBIN_bot",
            status="suspected_ended",
            created_at=(datetime.now(UTC) - timedelta(days=5)).isoformat(),
        )
        stale.ai_state["shell_liveness"] = {
            "last_heartbeat_at": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
        }

        self.assertTrue(_session_is_stale_status_candidate(stale))

    def test_meeting_topic_is_hidden_when_it_is_just_a_number(self) -> None:
        session = DelegateSession(
            session_id="abcd1234ef56",
            delegate_mode="answer_on_ask",
            bot_display_name="WooBIN_bot",
            meeting_topic="86485861467",
            meeting_number="86485861467",
            status="completed",
        )

        self.assertEqual(_friendly_meeting_topic(session), "")


if __name__ == "__main__":
    unittest.main()
