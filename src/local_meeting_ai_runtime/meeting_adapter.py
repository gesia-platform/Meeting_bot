"""Meeting SDK integration points for the delegate runtime."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .models import DelegateSession

DEFAULT_MEETING_SDK_WEB_SCRIPT_URL = os.getenv(
    "ZOOM_MEETING_SDK_WEB_SCRIPT_URL",
    "https://source.zoom.us/4.0.5/zoom-meeting-embedded-4.0.5.min.js",
)
DEFAULT_MEETING_SDK_VENDOR_SCRIPT_URLS = [
    os.getenv("ZOOM_MEETING_SDK_REACT_SCRIPT_URL", "https://source.zoom.us/4.0.5/lib/vendor/react.min.js"),
    os.getenv("ZOOM_MEETING_SDK_REACT_DOM_SCRIPT_URL", "https://source.zoom.us/4.0.5/lib/vendor/react-dom.min.js"),
    os.getenv("ZOOM_MEETING_SDK_REDUX_SCRIPT_URL", "https://source.zoom.us/4.0.5/lib/vendor/redux.min.js"),
    os.getenv("ZOOM_MEETING_SDK_REDUX_THUNK_SCRIPT_URL", "https://source.zoom.us/4.0.5/lib/vendor/redux-thunk.min.js"),
    os.getenv("ZOOM_MEETING_SDK_LODASH_SCRIPT_URL", "https://source.zoom.us/4.0.5/lib/vendor/lodash.min.js"),
]


class MeetingSdkError(RuntimeError):
    """Raised when the browser Meeting SDK flow cannot be prepared."""


class MeetingSdkAdapter:
    def _env_bool(self, name: str, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        return value.strip().lower() in {"1", "true", "yes", "on"}

    def _default_view_type(self) -> str:
        view_type = os.getenv("DELEGATE_MEETING_VIEW_TYPE", "minimized").strip().lower()
        if view_type not in {"active", "gallery", "minimized", "ribbon", "speaker"}:
            return "minimized"
        return view_type

    def _video_options(self) -> dict[str, Any]:
        return {
            "defaultViewType": self._default_view_type(),
            "isResizable": False,
            "viewSizes": {
                "default": {"width": 320, "height": 180},
                "ribbon": {"width": 320, "height": 180},
            },
        }

    def _init_options(self) -> dict[str, Any]:
        return {
            "language": os.getenv("ZOOM_MEETING_SDK_LANGUAGE", "ko-KR"),
            "patchJsMedia": True,
            "leaveOnPageUnload": True,
            "maximumVideosInGalleryView": 1,
            "customize": {
                "sharing": {"options": {"hideShareAudioOption": True}},
                "video": self._video_options(),
            },
        }

    def _post_join_actions(self) -> dict[str, Any]:
        return {
            "disconnectAudio": self._env_bool("DELEGATE_DISCONNECT_AUDIO_ON_JOIN", False),
            "muteSelf": self._env_bool("DELEGATE_MUTE_SELF_ON_JOIN", True),
            "hideMeetingUiOnJoin": self._env_bool("DELEGATE_HIDE_MEETING_UI_ON_JOIN", True),
            "viewType": self._default_view_type(),
            "videoOptions": self._video_options(),
            "reapplyIntervalMs": int(os.getenv("DELEGATE_REAPPLY_SILENT_JOIN_MS", "8000")),
        }

    def _ui_profile(self) -> dict[str, Any]:
        return {
            "hideMeetingUiOnJoin": self._env_bool("DELEGATE_HIDE_MEETING_UI_ON_JOIN", True),
            "showMeetingUiToggle": True,
        }

    def _desktop_launch_target(self, session: DelegateSession) -> dict[str, Any]:
        meeting_number = session.meeting_number or session.meeting_id or ""
        join_url = session.join_url or ""
        parsed = urlsplit(join_url) if join_url else None
        host = parsed.netloc if parsed and parsed.netloc else "zoom.us"
        passcode = session.passcode or ""
        if not passcode and parsed:
            values = parse_qs(parsed.query).get("pwd") or [""]
            passcode = values[0]

        protocol_url = ""
        if meeting_number:
            protocol_url = (
                f"zoommtg://{host}/join"
                f"?action=join&confno={quote(str(meeting_number))}"
                f"&uname={quote(session.bot_display_name)}"
            )
            if passcode:
                protocol_url += f"&pwd={quote(passcode)}"

        launch_url = protocol_url or join_url
        return {
            "join_url": join_url,
            "protocol_url": protocol_url,
            "launch_url": launch_url,
            "fallback_url": join_url,
            "meeting_number": str(meeting_number),
            "passcode_present": bool(passcode),
            "can_launch": bool(launch_url),
        }

    def build_join_ticket(
        self,
        session: DelegateSession,
        *,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        sdk_key = os.getenv("ZOOM_MEETING_SDK_KEY", "").strip()
        sdk_secret = os.getenv("ZOOM_MEETING_SDK_SECRET", "").strip()
        meeting_number = session.meeting_number or session.meeting_id or ""
        browser_join_url = ""
        browser_auto_join_url = ""
        desktop_auto_launch_url = ""
        meeting_sdk_config_url = ""
        desktop_launch = self._desktop_launch_target(session)
        if base_url:
            browser_join_url = f"{base_url}/delegate/join/{session.session_id}"
            browser_auto_join_url = f"{browser_join_url}?auto=browser"
            desktop_auto_launch_url = f"{browser_join_url}?auto=desktop"
            meeting_sdk_config_url = f"{base_url}/delegate/sessions/{session.session_id}/meeting-sdk-config"

        return {
            "sdk_key_present": bool(sdk_key),
            "sdk_secret_present": bool(sdk_secret),
            "sdk_script_url": DEFAULT_MEETING_SDK_WEB_SCRIPT_URL,
            "sdk_vendor_script_urls": DEFAULT_MEETING_SDK_VENDOR_SCRIPT_URLS,
            "display_name": session.bot_display_name,
            "meeting_number": meeting_number,
            "passcode_present": bool(session.passcode),
            "join_url": session.join_url or "",
            "browser_join_url": browser_join_url,
            "browser_auto_join_url": browser_auto_join_url,
            "desktop_auto_launch_control_url": desktop_auto_launch_url,
            "meeting_sdk_config_url": meeting_sdk_config_url,
            "can_join_in_browser": bool(sdk_key and sdk_secret and meeting_number and session.passcode),
            "desktop_launch": desktop_launch,
            "desktop_launch_url": desktop_launch["launch_url"],
            "desktop_protocol_url": desktop_launch["protocol_url"],
            "desktop_fallback_url": desktop_launch["fallback_url"],
            "can_join_in_zoom_workplace": desktop_launch["can_launch"],
            "ui_profile": self._ui_profile(),
            "post_join_actions": self._post_join_actions(),
        }

    async def join(self, session: DelegateSession, *, base_url: str) -> dict[str, Any]:
        ticket = self.build_join_ticket(session, base_url=base_url)
        missing = []
        if not ticket["sdk_key_present"]:
            missing.append("ZOOM_MEETING_SDK_KEY")
        if not ticket["sdk_secret_present"]:
            missing.append("ZOOM_MEETING_SDK_SECRET")
        if not ticket["meeting_number"]:
            missing.append("meeting_number")
        if not ticket["passcode_present"]:
            missing.append("passcode")

        if missing:
            if ticket["can_join_in_zoom_workplace"]:
                return {
                    **ticket,
                    "status": "desktop_ready",
                    "message": (
                        "Browser join is missing Meeting SDK credentials, but Zoom Workplace launch "
                        "is available through desktop_launch_url."
                    ),
                    "missing": missing,
                }
            return {
                **ticket,
                "status": "blocked",
                "message": "Browser join is not ready. Missing: " + ", ".join(missing),
                "missing": missing,
            }

        return {
            **ticket,
            "status": "browser_ready",
            "message": (
                "Open browser_join_url and click Join Meeting, or use desktop_launch_url to open "
                "Zoom Workplace for the same delegate session."
            ),
        }

    def build_client_config(self, session: DelegateSession, *, base_url: str) -> dict[str, Any]:
        meeting_number = session.meeting_number or session.meeting_id or ""
        passcode = session.passcode or ""
        sdk_key = os.getenv("ZOOM_MEETING_SDK_KEY", "").strip()
        sdk_secret = os.getenv("ZOOM_MEETING_SDK_SECRET", "").strip()

        if not sdk_key or not sdk_secret:
            raise MeetingSdkError("ZOOM_MEETING_SDK_KEY and ZOOM_MEETING_SDK_SECRET must be configured.")
        if not meeting_number:
            raise MeetingSdkError("The delegate session does not contain a meeting number.")
        if not passcode:
            raise MeetingSdkError("The delegate session does not contain a meeting passcode.")

        return {
            "sdkKey": sdk_key,
            "signature": self._generate_signature(
                sdk_key=sdk_key,
                sdk_secret=sdk_secret,
                meeting_number=meeting_number,
                role=0,
                expiration_seconds=7200,
            ),
            "sdkVendorScriptUrls": DEFAULT_MEETING_SDK_VENDOR_SCRIPT_URLS,
            "sdkScriptUrl": DEFAULT_MEETING_SDK_WEB_SCRIPT_URL,
            "meetingNumber": meeting_number,
            "password": passcode,
            "userName": session.bot_display_name,
            "userEmail": "",
            "registrantToken": "",
            "zakToken": "",
            "leaveUrl": f"{base_url}/delegate/leave/{session.session_id}",
            "initOptions": self._init_options(),
            "postJoinActions": self._post_join_actions(),
            "uiProfile": self._ui_profile(),
        }

    def _generate_signature(
        self,
        *,
        sdk_key: str,
        sdk_secret: str,
        meeting_number: str,
        role: int,
        expiration_seconds: int,
    ) -> str:
        issued_at = int(time.time()) - 30
        expires_at = issued_at + expiration_seconds
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "appKey": sdk_key,
            "sdkKey": sdk_key,
            "mn": meeting_number,
            "role": role,
            "iat": issued_at,
            "exp": expires_at,
            "tokenExp": expires_at,
        }
        encoded_header = self._encode_segment(header)
        encoded_payload = self._encode_segment(payload)
        signing_input = f"{encoded_header}.{encoded_payload}".encode("utf-8")
        digest = hmac.new(sdk_secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
        signature = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("utf-8")
        return f"{encoded_header}.{encoded_payload}.{signature}"

    def _encode_segment(self, payload: dict[str, Any]) -> str:
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("utf-8")
