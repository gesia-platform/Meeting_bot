"""Starlette app for the local meeting AI runtime."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .ai_client import AiDelegateClient
from .artifact_exporter import MeetingArtifactExporter
from .local_observer import LocalObserver, LocalObserverError
from .meeting_adapter import MeetingSdkAdapter, MeetingSdkError
from .service import DelegateService
from .storage import SessionStore
from .summary_pipeline import DelegateSummaryPipeline
from .zoom_client import ZoomRestClient, ZoomRuntimeError


def _load_dotenv() -> None:
    env_path = Path(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#") or "=" not in entry:
            continue
        key, value = entry.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _base_url(request: Request) -> str:
    configured = os.getenv("DELEGATE_PUBLIC_BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{scheme}://{host}"


def create_service(*, store_path: str | None = None, export_dir: str | None = None) -> DelegateService:
    _load_dotenv()
    return DelegateService(
        store=SessionStore(path=store_path or os.getenv("DELEGATE_STORE_PATH", "data/delegate_sessions.json")),
        zoom_client=ZoomRestClient(),
        ai_client=AiDelegateClient(),
        meeting_adapter=MeetingSdkAdapter(),
        local_observer=LocalObserver(),
        summary_pipeline=DelegateSummaryPipeline(),
        artifact_exporter=MeetingArtifactExporter(),
        export_dir=export_dir or os.getenv("DELEGATE_EXPORT_DIR", "data/exports"),
    )


def create_app(*, store_path: str | None = None, export_dir: str | None = None) -> Starlette:
    service = create_service(store_path=store_path, export_dir=export_dir)
    watchdog_task: asyncio.Task[None] | None = None

    async def startup() -> None:
        nonlocal watchdog_task
        await service.recover_incomplete_sessions()
        watchdog_task = asyncio.create_task(service.run_auto_completion_watchdog())

    async def shutdown() -> None:
        nonlocal watchdog_task
        if watchdog_task is None:
            return
        watchdog_task.cancel()
        try:
            await watchdog_task
        except asyncio.CancelledError:
            pass
        watchdog_task = None

    async def health(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "service": "local-meeting-ai-runtime"})

    async def runtime_overview(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, **service.runtime_overview()})

    async def list_sessions(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "sessions": [item.to_dict() for item in service.list_sessions()]})

    async def create_session(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            session = await service.create_session(payload, base_url=_base_url(request))
        except ZoomRuntimeError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        return JSONResponse({"ok": True, "session": session.to_dict()})

    async def get_session(request: Request) -> JSONResponse:
        session = service.get_session(request.path_params["session_id"])
        if session is None:
            return JSONResponse({"ok": False, "error": "Unknown delegate session."}, status_code=404)
        return JSONResponse({"ok": True, "session": session.to_dict()})

    async def session_heartbeat(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
        try:
            session, result = service.record_shell_heartbeat(request.path_params["session_id"], payload)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        return JSONResponse({"ok": True, "session": session.to_dict(), "heartbeat": result})

    async def session_readiness(request: Request) -> JSONResponse:
        session = service.get_session(request.path_params["session_id"])
        if session is None:
            return JSONResponse({"ok": False, "error": "Unknown delegate session."}, status_code=404)
        service.refresh_join_ticket(session.session_id, base_url=_base_url(request))
        refreshed = service.get_session(session.session_id)
        return JSONResponse(
            {
                "ok": True,
                "session_id": session.session_id,
                "preflight": refreshed.preflight if refreshed else session.preflight,
                "join_ticket": refreshed.join_ticket if refreshed else session.join_ticket,
            }
        )

    async def start_session(request: Request) -> JSONResponse:
        try:
            session, launch = await service.start_session(request.path_params["session_id"], base_url=_base_url(request))
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        return JSONResponse({"ok": True, "session": session.to_dict(), "launch": launch})

    async def meeting_sdk_config(request: Request) -> JSONResponse:
        try:
            config = service.build_meeting_sdk_config(request.path_params["session_id"], base_url=_base_url(request))
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except MeetingSdkError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "config": config})

    async def join_page(request: Request) -> HTMLResponse:
        session = service.get_session(request.path_params["session_id"])
        if session is None:
            return HTMLResponse("<h1>Unknown delegate session</h1>", status_code=404)

        join_ticket = service.refresh_join_ticket(session.session_id, base_url=_base_url(request))
        title = "WooBIN_bot Zoom Entrance"
        meeting_label = session.meeting_topic or session.meeting_number or session.meeting_id or "Unknown meeting"
        config_url = f"{_base_url(request)}/delegate/sessions/{session.session_id}/meeting-sdk-config"
        sdk_script_url = str(join_ticket.get("sdk_script_url") or "")
        vendor_urls = [str(item) for item in join_ticket.get("sdk_vendor_script_urls", []) if str(item).strip()]
        desktop_launch_url = str(join_ticket.get("desktop_launch_url") or "")
        desktop_ready = "true" if desktop_launch_url else "false"
        browser_ready = "true" if join_ticket.get("can_join_in_browser") else "false"

        vendor_tags = "\n".join(
            f'<script src="{url}" defer></script>'
            for url in vendor_urls
        )

        html = f"""<!doctype html>
<html lang="ko">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <style>
      body {{
        margin: 0;
        font-family: "Segoe UI", sans-serif;
        background: #f4f6fb;
        color: #162033;
      }}
      main {{
        max-width: 980px;
        margin: 0 auto;
        padding: 28px 20px 40px;
      }}
      .card {{
        background: #fff;
        border: 1px solid #d8dfec;
        border-radius: 16px;
        padding: 20px;
        box-shadow: 0 10px 30px rgba(17, 24, 39, 0.06);
      }}
      h1 {{
        margin: 0 0 8px;
        font-size: 28px;
      }}
      p {{
        line-height: 1.5;
      }}
      .meta {{
        margin: 18px 0;
        padding: 14px;
        border-radius: 12px;
        background: #f7f9fc;
      }}
      .actions {{
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        margin: 20px 0 14px;
      }}
      button, a.button {{
        border: none;
        border-radius: 999px;
        padding: 12px 18px;
        font-weight: 600;
        text-decoration: none;
        cursor: pointer;
      }}
      button.primary {{
        background: #1752ff;
        color: #fff;
      }}
      button.secondary,
      a.secondary {{
        background: #eef2ff;
        color: #23314f;
      }}
      .status {{
        margin-top: 12px;
        min-height: 24px;
        font-size: 14px;
      }}
      #meetingSDKElement {{
        margin-top: 22px;
        min-height: 360px;
        border-radius: 16px;
        overflow: hidden;
        background: #e8edf8;
      }}
    </style>
    {vendor_tags}
    <script src="{sdk_script_url}" defer></script>
  </head>
  <body>
    <main>
      <section class="card">
        <h1>WooBIN_bot Zoom Entrance</h1>
        <p>이 페이지는 Zoom 출입구입니다. 실제 회의 이해와 수집은 뒤의 로컬 AI 본체가 담당합니다.</p>
        <div class="meta">
          <strong>Meeting</strong>: {meeting_label}<br>
          <strong>Delegate</strong>: {session.bot_display_name}<br>
          <strong>Mode</strong>: {session.delegate_mode}
        </div>
        <div class="actions">
          <button class="primary" id="joinButton" type="button">Open in Zoom Workplace</button>
          <button class="secondary" id="browserJoinButton" type="button">Join in Browser (Experimental)</button>
          <a class="button secondary" href="{config_url}" target="_blank" rel="noreferrer">Open SDK Config</a>
        </div>
        <div class="status" id="status">Ready. 기본 입장은 Zoom Workplace를 사용합니다.</div>
        <div id="meetingSDKElement"></div>
      </section>
    </main>
    <script>
      const configUrl = {json.dumps(config_url)};
      const browserReady = {browser_ready};
      const statusEl = document.getElementById("status");
      const joinButton = document.getElementById("joinButton");

      async function loadConfig() {{
        const response = await fetch(configUrl, {{ credentials: "same-origin" }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || "Failed to load Meeting SDK config.");
        }}
        return payload.config;
      }}

      async function joinMeeting() {{
        if (!browserReady) {{
          statusEl.textContent = "Browser join is not ready for this session.";
          return;
        }}
        if (!window.ZoomMtgEmbedded || typeof window.ZoomMtgEmbedded.createClient !== "function") {{
          statusEl.textContent = "Zoom Meeting SDK script is not ready yet. Please wait a few seconds and retry.";
          return;
        }}
        joinButton.disabled = true;
        statusEl.textContent = "Loading Meeting SDK config...";
        try {{
          const config = await loadConfig();
          const client = window.ZoomMtgEmbedded.createClient();
          client.init({{
            zoomAppRoot: document.getElementById("meetingSDKElement"),
            ...config.initOptions
          }});
          statusEl.textContent = "Joining meeting as WooBIN_bot...";
          await client.join({{
            sdkKey: config.sdkKey,
            signature: config.signature,
            meetingNumber: config.meetingNumber,
            password: config.password,
            userName: config.userName,
            userEmail: config.userEmail,
            zak: config.zakToken
          }});
          statusEl.textContent = "WooBIN_bot joined the meeting.";
        }} catch (error) {{
          statusEl.textContent = "Join failed: " + (error && error.message ? error.message : String(error));
          joinButton.disabled = false;
        }}
      }}

      joinButton.addEventListener("click", joinMeeting);
    </script>
  </body>
</html>"""
        return HTMLResponse(html)

    async def join_page_v2(request: Request) -> HTMLResponse:
        session = service.get_session(request.path_params["session_id"])
        if session is None:
            return HTMLResponse("<h1>Unknown delegate session</h1>", status_code=404)

        join_ticket = service.refresh_join_ticket(session.session_id, base_url=_base_url(request))
        auto_mode = str(request.query_params.get("auto") or "").strip().lower()
        if auto_mode not in {"browser", "desktop"}:
            auto_mode = ""
        title = "WooBIN_bot Zoom Entrance"
        meeting_label = session.meeting_topic or session.meeting_number or session.meeting_id or "Unknown meeting"
        config_url = f"{_base_url(request)}/delegate/sessions/{session.session_id}/meeting-sdk-config"
        inputs_url = f"{_base_url(request)}/delegate/sessions/{session.session_id}/inputs"
        heartbeat_url = f"{_base_url(request)}/delegate/sessions/{session.session_id}/heartbeat"
        observe_audio_start_url = f"{_base_url(request)}/delegate/sessions/{session.session_id}/observe/audio/start"
        observe_audio_stop_url = f"{_base_url(request)}/delegate/sessions/{session.session_id}/observe/audio/stop"
        complete_url = f"{_base_url(request)}/delegate/sessions/{session.session_id}/complete"
        sdk_script_url = str(join_ticket.get("sdk_script_url") or "")
        vendor_urls = [str(item) for item in join_ticket.get("sdk_vendor_script_urls", []) if str(item).strip()]
        desktop_launch_url = str(join_ticket.get("desktop_launch_url") or "")
        desktop_ready = "true" if desktop_launch_url else "false"
        browser_ready = "true" if join_ticket.get("can_join_in_browser") else "false"
        runtime_overview = service.runtime_overview()
        auto_audio_enabled = "true" if (
            runtime_overview.get("audio_transcription_ready")
            and runtime_overview.get("local_observer_capabilities", {}).get("system_audio_capture")
        ) else "false"
        heartbeat_interval_ms = max(int(os.getenv("DELEGATE_SESSION_HEARTBEAT_INTERVAL_MS", "3000")), 1000)
        auto_audio_seconds = float(os.getenv("DELEGATE_AUTO_OBSERVE_AUDIO_SECONDS", "12"))
        auto_audio_interval_ms = int(os.getenv("DELEGATE_AUTO_OBSERVE_AUDIO_INTERVAL_MS", "250"))
        auto_audio_mode = str(runtime_overview.get("auto_observe_audio_mode") or "conversation")

        vendor_tags = "\n".join(
            f'<script src="{url}" defer></script>'
            for url in vendor_urls
        )

        html = f"""<!doctype html>
<html lang="ko">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <style>
      body {{
        margin: 0;
        font-family: "Segoe UI", sans-serif;
        background: #f4f6fb;
        color: #162033;
      }}
      main {{
        max-width: 980px;
        margin: 0 auto;
        padding: 28px 20px 40px;
      }}
      .card {{
        background: #fff;
        border: 1px solid #d8dfec;
        border-radius: 16px;
        padding: 20px;
        box-shadow: 0 10px 30px rgba(17, 24, 39, 0.06);
      }}
      h1 {{
        margin: 0 0 8px;
        font-size: 28px;
      }}
      p {{
        line-height: 1.5;
      }}
      .meta {{
        margin: 18px 0;
        padding: 14px;
        border-radius: 12px;
        background: #f7f9fc;
      }}
      .actions {{
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        margin: 20px 0 14px;
      }}
      button, a.button {{
        border: none;
        border-radius: 999px;
        padding: 12px 18px;
        font-weight: 600;
        text-decoration: none;
        cursor: pointer;
      }}
      button.primary {{
        background: #1752ff;
        color: #fff;
      }}
      a.secondary {{
        background: #eef2ff;
        color: #23314f;
      }}
      .status {{
        margin-top: 12px;
        min-height: 24px;
        font-size: 14px;
      }}
      #meetingSDKElement {{
        margin-top: 22px;
        min-height: 360px;
        border-radius: 16px;
        overflow: hidden;
        background: #e8edf8;
      }}
    </style>
    {vendor_tags}
    <script src="{sdk_script_url}" defer></script>
  </head>
  <body>
    <main>
      <section class="card">
        <h1>WooBIN_bot Zoom Entrance</h1>
        <p>이 페이지는 Zoom 출입구입니다. 실제 회의 이해와 수집은 뒤의 로컬 AI 본체가 담당합니다.</p>
        <div class="meta">
          <strong>Meeting</strong>: {meeting_label}<br>
          <strong>Delegate</strong>: {session.bot_display_name}<br>
          <strong>Mode</strong>: {session.delegate_mode}
        </div>
        <div class="actions">
          <button class="primary" id="joinButton" type="button">Open in Zoom Workplace</button>
          <button class="secondary" id="browserJoinButton" type="button">Join in Browser (Experimental)</button>
          <a class="button secondary" href="{config_url}" target="_blank" rel="noreferrer">Open SDK Config</a>
        </div>
        <div class="status" id="status">Ready. 기본 입장은 Zoom Workplace를 사용합니다.</div>
        <div id="meetingSDKElement"></div>
      </section>
    </main>
    <script>
      const configUrl = {json.dumps(config_url)};
      const inputsUrl = {json.dumps(inputs_url)};
      const heartbeatUrl = {json.dumps(heartbeat_url)};
      const observeAudioStartUrl = {json.dumps(observe_audio_start_url)};
      const observeAudioStopUrl = {json.dumps(observe_audio_stop_url)};
      const completeUrl = {json.dumps(complete_url)};
      const heartbeatIntervalMs = {heartbeat_interval_ms};
      const delegateName = {json.dumps(session.bot_display_name)};
      const desktopLaunchUrl = {json.dumps(desktop_launch_url)};
      const desktopReady = {desktop_ready};
      const browserReady = {browser_ready};
      const autoJoinMode = {json.dumps(auto_mode)};
      const autoAudioEnabled = {auto_audio_enabled};
      const autoAudioSeconds = {auto_audio_seconds};
      const autoAudioIntervalMs = {auto_audio_interval_ms};
      const autoAudioMode = {json.dumps(auto_audio_mode)};
      const statusEl = document.getElementById("status");
      const joinButton = document.getElementById("joinButton");
      const browserJoinButton = document.getElementById("browserJoinButton");
      const runtimeState = {{
        client: null,
        config: null,
        seenInputKeys: new Set(),
        publishedReplyKeys: new Set(),
        audioObserverRunning: false,
        completeRequested: false,
        completionArmed: false,
        chatObserverAttachedAtMs: 0,
        joined: false,
        launchMode: null,
        sessionClockStartMs: 0,
        heartbeatTimer: null,
        heartbeatInFlight: false,
        autoJoinRequested: false
      }};

      function currentSessionOffsetSeconds() {{
        if (!runtimeState.sessionClockStartMs) {{
          return null;
        }}
        return Math.max(0, (Date.now() - runtimeState.sessionClockStartMs) / 1000);
      }}

      function sendCompleteBeacon() {{
        try {{
          return navigator.sendBeacon(completeUrl);
        }} catch (error) {{
          console.warn("Failed to send completion beacon.", error);
          return false;
        }}
      }}

      function sendStopObserverBeacon() {{
        try {{
          return navigator.sendBeacon(observeAudioStopUrl);
        }} catch (error) {{
          console.warn("Failed to send audio-stop beacon.", error);
          return false;
        }}
      }}

      async function sendHeartbeat(reason = "interval") {{
        if (runtimeState.heartbeatInFlight) {{
          return;
        }}
        runtimeState.heartbeatInFlight = true;
        try {{
          await fetch(heartbeatUrl, {{
            method: "POST",
            headers: {{ "content-type": "application/json" }},
            credentials: "same-origin",
            keepalive: reason !== "interval",
            body: JSON.stringify({{
              reason,
              joined: runtimeState.joined,
              completion_armed: runtimeState.completionArmed,
              audio_observer_running: runtimeState.audioObserverRunning,
              launch_mode: runtimeState.launchMode,
              visibility_state: document.visibilityState || null,
              user_agent: navigator.userAgent || null
            }})
          }});
        }} catch (error) {{
          console.warn("Failed to send control-shell heartbeat.", error);
        }} finally {{
          runtimeState.heartbeatInFlight = false;
        }}
      }}

      function ensureHeartbeatLoop() {{
        if (runtimeState.heartbeatTimer !== null) {{
          return;
        }}
        runtimeState.heartbeatTimer = window.setInterval(() => {{
          sendHeartbeat("interval").catch(() => undefined);
        }}, heartbeatIntervalMs);
        sendHeartbeat("page_ready").catch(() => undefined);
      }}

      function stopHeartbeatLoop() {{
        if (runtimeState.heartbeatTimer === null) {{
          return;
        }}
        window.clearInterval(runtimeState.heartbeatTimer);
        runtimeState.heartbeatTimer = null;
      }}

      async function loadConfig() {{
        const response = await fetch(configUrl, {{ credentials: "same-origin" }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || "Failed to load Meeting SDK config.");
        }}
        return payload.config;
      }}

      function compactText(value) {{
        return String(value || "").replace(/\\s+/g, " ").trim();
      }}

      function parseZoomTimestampMs(value) {{
        if (value === null || value === undefined) {{
          return null;
        }}
        if (typeof value === "number" && Number.isFinite(value)) {{
          return value > 1e12 ? value : value * 1000;
        }}
        const text = compactText(value);
        if (!text) {{
          return null;
        }}
        if (/^\\d+$/.test(text)) {{
          const numeric = Number(text);
          if (Number.isFinite(numeric)) {{
            return numeric > 1e12 ? numeric : numeric * 1000;
          }}
        }}
        const parsed = Date.parse(text);
        return Number.isFinite(parsed) ? parsed : null;
      }}

      function formatJoinError(error) {{
        if (!error) {{
          return "Unknown join error.";
        }}
        if (typeof error === "string") {{
          return error;
        }}
        const parts = [];
        if (error.message) parts.push(String(error.message));
        if (error.reason && error.reason !== error.message) parts.push(String(error.reason));
        if (error.errorMessage && error.errorMessage !== error.message) parts.push(String(error.errorMessage));
        if (error.errorCode !== undefined) parts.push("code=" + String(error.errorCode));
        if (error.type) parts.push("type=" + String(error.type));
        if (!parts.length) {{
          try {{
            return JSON.stringify(error);
          }} catch (_jsonError) {{
            return String(error);
          }}
        }}
        return parts.join(" | ");
      }}

      function makeInputKey(input) {{
        const metadata = input.metadata || {{}};
        return [
          input.input_type || "",
          input.speaker || "",
          input.text || "",
          input.source || "",
          metadata.msgId || metadata.id || metadata.timestamp || metadata.userId || metadata.state || metadata.status || ""
        ].join("::");
      }}

      function isDirectAddress(text) {{
        const normalized = compactText(text).toLowerCase();
        if (!normalized) {{
          return false;
        }}
        const botName = compactText(delegateName).toLowerCase();
        return normalized.includes("@" + botName) || normalized.includes(botName) || /\\?$/.test(normalized);
      }}

      async function ingestInputs(inputs) {{
        const deduped = [];
        for (const input of inputs) {{
          const key = makeInputKey(input);
          if (!key || runtimeState.seenInputKeys.has(key)) {{
            continue;
          }}
          runtimeState.seenInputKeys.add(key);
          deduped.push(input);
        }}
        if (!deduped.length) {{
          return null;
        }}
        const response = await fetch(inputsUrl, {{
          method: "POST",
          headers: {{ "content-type": "application/json" }},
          credentials: "same-origin",
          body: JSON.stringify({{ inputs: deduped }})
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || "Failed to ingest meeting inputs.");
        }}
        return payload;
      }}

      async function maybePublishReply(reply) {{
        if (!reply || reply.status !== "ready_to_publish" || !reply.draft) {{
          return;
        }}
        if (!runtimeState.client || typeof runtimeState.client.sendChat !== "function") {{
          return;
        }}
        const draft = compactText(reply.draft);
        const replyKey = String(reply.reply_key || draft.toLowerCase()).trim().toLowerCase();
        if (!draft || runtimeState.publishedReplyKeys.has(replyKey)) {{
          return;
        }}
        runtimeState.publishedReplyKeys.add(replyKey);
        try {{
          await runtimeState.client.sendChat(draft);
          await ingestInputs([
            {{
              input_type: "bot_reply",
              speaker: delegateName,
              text: draft,
              source: "zoom_chat_publish",
              metadata: {{
                status: "sent",
                trigger: reply.trigger || "direct_question",
                provider: reply.provider || "codex_exec"
              }}
            }}
          ]);
          statusEl.textContent = "WooBIN_bot replied in the meeting chat.";
        }} catch (error) {{
          statusEl.textContent = "Reply draft prepared, but Zoom chat publish failed.";
          console.warn("Failed to publish delegate reply via Zoom chat.", error);
        }}
      }}

      async function triggerAutoComplete(reason) {{
        if (runtimeState.completeRequested) {{
          return;
        }}
        runtimeState.completeRequested = true;
        runtimeState.completionArmed = true;
        sendCompleteBeacon();
        try {{
          const response = await fetch(completeUrl, {{
            method: "POST",
            credentials: "same-origin",
            keepalive: true
          }});
          const payload = await response.json();
          if (!response.ok || !payload.ok) {{
            throw new Error(payload.error || "Failed to complete the session.");
          }}
          statusEl.textContent = "Meeting ended. Summary and PDF generation started.";
        }} catch (error) {{
          statusEl.textContent = "Meeting ended, but automatic summary/PDF generation failed.";
          console.warn("Failed to auto-complete delegate session.", reason, error);
        }}
      }}

      async function startAudioObserver() {{
        if (!autoAudioEnabled || runtimeState.audioObserverRunning) {{
          return;
        }}
        const response = await fetch(observeAudioStartUrl, {{
          method: "POST",
          headers: {{ "content-type": "application/json" }},
          credentials: "same-origin",
          body: JSON.stringify({{
            seconds: autoAudioSeconds,
            interval_ms: autoAudioIntervalMs,
            audio_mode: autoAudioMode,
            metadata: {{
              capture_profile: "join_page_auto_observer"
            }}
          }})
        }});
        const payload = await response.json();
        if (!response.ok || !payload.ok) {{
          throw new Error(payload.error || "Failed to start continuous meeting audio observation.");
        }}
        runtimeState.audioObserverRunning = true;
        statusEl.textContent = "Continuous local meeting listening started.";
        return payload;
      }}

      async function stopAudioObserver() {{
        if (!runtimeState.audioObserverRunning) {{
          return;
        }}
        try {{
          await fetch(observeAudioStopUrl, {{
            method: "POST",
            credentials: "same-origin"
          }});
        }} catch (error) {{
          console.warn("Failed to stop continuous local audio observation.", error);
        }} finally {{
          runtimeState.audioObserverRunning = false;
        }}
      }}

      function extractParticipantName(payload) {{
        return compactText(
          payload && (
            payload.displayName ||
            payload.userName ||
            payload.user_name ||
            payload.name ||
            payload.participantName ||
            payload.participant_name ||
            (payload.sender && payload.sender.name)
          )
        ) || "participant";
      }}

      function extractChatText(payload) {{
        if (!payload) {{
          return "";
        }}
        if (Array.isArray(payload.message)) {{
          return compactText(payload.message.join(" "));
        }}
        return compactText(payload.message || payload.text || "");
      }}

      async function ingestAttendeeSnapshot(client, reason) {{
        if (!client || typeof client.getAttendeeslist !== "function") {{
          return;
        }}
        try {{
          const attendees = client.getAttendeeslist();
          const items = Array.isArray(attendees) ? attendees : [];
          if (!items.length) {{
            return;
          }}
          const inputs = items
            .map((item) => {{
              const speaker = extractParticipantName(item);
              if (!speaker) {{
                return null;
              }}
              return {{
                input_type: "participant_state",
                speaker,
                source: "zoom_attendee_snapshot",
                metadata: {{
                  state: "present",
                  event: "attendee-snapshot",
                  reason: compactText(reason) || "snapshot",
                  session_offset_seconds: currentSessionOffsetSeconds(),
                  userId: item && item.userId,
                  raw: item || {{}}
                }}
              }};
            }})
            .filter(Boolean);
          if (!inputs.length) {{
            return;
          }}
          await ingestInputs(inputs);
        }} catch (error) {{
          console.warn("Failed to snapshot attendee list.", error);
        }}
      }}

      function scheduleAttendeeSnapshot(client, reason, delayMs = 0) {{
        window.setTimeout(() => {{
          ingestAttendeeSnapshot(client, reason).catch((error) => {{
            console.warn("Failed to schedule attendee snapshot.", error);
          }});
        }}, Math.max(0, Number(delayMs) || 0));
      }}

      async function attachInputObservers(client, config) {{
        if (client.__delegateInputObserversAttached) {{
          return;
        }}
        client.__delegateInputObserversAttached = true;
        runtimeState.client = client;
        runtimeState.config = config;
        runtimeState.chatObserverAttachedAtMs = Date.now();

        client.on("connection-change", async (payload) => {{
          try {{
            await ingestInputs([
              {{
                input_type: "meeting_state",
                source: "zoom_connection_change",
                metadata: {{
                  status: compactText(payload && payload.state) || "unknown",
                  raw: payload || {{}}
                }}
              }}
            ]);
            if (payload && payload.state) {{
              statusEl.textContent = "Zoom state: " + payload.state;
              const normalizedState = compactText(payload.state).toLowerCase();
              if (normalizedState === "closed") {{
                await stopAudioObserver();
                await triggerAutoComplete("connection-change:closed");
              }}
            }}
          }} catch (error) {{
            console.warn("Failed to ingest Zoom connection change.", error);
          }}
        }});

        client.on("chat-on-message", async (payload) => {{
          try {{
            const speaker = extractParticipantName(payload);
            const text = extractChatText(payload);
            if (!text) {{
              return;
            }}
            if (compactText(speaker).toLowerCase() === compactText(delegateName).toLowerCase()) {{
              return;
            }}
            const messageTimestampMs = parseZoomTimestampMs(payload && payload.timestamp);
            if (
              messageTimestampMs !== null
              && runtimeState.chatObserverAttachedAtMs
              && messageTimestampMs + 1000 < runtimeState.chatObserverAttachedAtMs
            ) {{
              return;
            }}
            const result = await ingestInputs([
              {{
                input_type: "meeting_chat",
                speaker,
                text,
                source: "zoom_chat_message",
                direct_question: isDirectAddress(text),
                metadata: {{
                  id: payload && payload.id,
                  timestamp: payload && payload.timestamp,
                  sender: payload && payload.sender || {{}},
                  receiver: payload && payload.receiver || {{}}
                }}
              }}
            ]);
            const processed = result && result.ingest_result && result.ingest_result.processed && result.ingest_result.processed[0];
            await maybePublishReply(processed && processed.reply);
          }} catch (error) {{
            console.warn("Failed to ingest meeting chat message.", error);
          }}
        }});

        for (const [eventName, stateName] of [["user-added", "joined"], ["user-removed", "left"], ["user-updated", "updated"]]) {{
          client.on(eventName, async (payload) => {{
            try {{
              await ingestInputs([
                {{
                  input_type: "participant_state",
                  speaker: extractParticipantName(payload),
                  source: "zoom_participant_event",
                  metadata: {{
                    state: stateName,
                    event: eventName,
                    session_offset_seconds: currentSessionOffsetSeconds(),
                    userId: payload && payload.userId,
                    raw: payload || {{}}
                  }}
                }}
              ]);
              await ingestAttendeeSnapshot(client, eventName);
              scheduleAttendeeSnapshot(client, eventName + "-settled", 1200);
            }} catch (error) {{
              console.warn("Failed to ingest participant state.", error);
            }}
          }});
        }}

        client.on("active-speaker", async (payload) => {{
          try {{
            const speakers = Array.isArray(payload) ? payload : [];
            if (!speakers.length) {{
              return;
            }}
            const observedAt = new Date().toISOString();
            const inputs = speakers
              .map((item) => {{
                const speaker = extractParticipantName(item);
                if (!speaker || compactText(speaker).toLowerCase() === compactText(delegateName).toLowerCase()) {{
                  return null;
                }}
                return {{
                  input_type: "participant_state",
                  speaker,
                  source: "zoom_active_speaker",
                  metadata: {{
                    state: "speaking",
                    event: "active-speaker",
                    observed_at: observedAt,
                    timestamp: observedAt,
                    session_offset_seconds: currentSessionOffsetSeconds(),
                    userId: item && item.userId,
                    raw: item || {{}}
                  }}
                }};
              }})
              .filter(Boolean);
            if (!inputs.length) {{
              return;
            }}
            await ingestInputs(inputs);
          }} catch (error) {{
            console.warn("Failed to ingest active speaker event.", error);
          }}
        }});

      }}

      function launchExternalMeetingShell(url) {{
        const link = document.createElement("a");
        link.href = url;
        link.target = "_blank";
        link.rel = "noreferrer noopener";
        link.style.display = "none";
        document.body.appendChild(link);
        link.click();
        window.setTimeout(() => link.remove(), 0);
      }}

      function openZoomWorkplace() {{
        if (!desktopReady || !desktopLaunchUrl) {{
          statusEl.textContent = "Zoom Workplace launch URL is not ready for this session.";
          return;
        }}
        joinButton.disabled = true;
        runtimeState.completionArmed = true;
        runtimeState.joined = true;
        runtimeState.launchMode = "desktop";
        if (autoAudioEnabled) {{
          startAudioObserver().catch((error) => {{
            console.warn("Failed to start continuous local audio observation.", error);
          }});
          statusEl.textContent = "Opening Zoom Workplace and keeping this control page alive for automatic completion...";
        }} else {{
          statusEl.textContent = "Opening Zoom Workplace and keeping this control page alive for automatic completion...";
        }}
        ingestInputs([
          {{
            input_type: "meeting_state",
            source: "zoom_desktop_launch",
            metadata: {{ status: "active", state: "launch_initiated" }}
          }}
        ]).catch((error) => {{
          console.warn("Failed to record desktop launch state.", error);
        }});
        sendHeartbeat("desktop_launch").catch(() => undefined);
        launchExternalMeetingShell(desktopLaunchUrl);
        setTimeout(() => {{
          joinButton.disabled = false;
        }}, 2500);
      }}

      async function joinMeetingInBrowser() {{
        if (!browserReady) {{
          statusEl.textContent = "Browser join is not ready for this session.";
          return false;
        }}
        if (!window.ZoomMtgEmbedded || typeof window.ZoomMtgEmbedded.createClient !== "function") {{
          statusEl.textContent = "Zoom Meeting SDK script is not ready yet. Please wait a few seconds and retry.";
          return false;
        }}
        joinButton.disabled = true;
        browserJoinButton.disabled = true;
        statusEl.textContent = "Loading Meeting SDK config...";
        try {{
          const config = await loadConfig();
          const client = window.ZoomMtgEmbedded.createClient();
          await client.init({{
            zoomAppRoot: document.getElementById("meetingSDKElement"),
            ...config.initOptions
          }});
          await attachInputObservers(client, config);
          statusEl.textContent = "Joining meeting in browser as WooBIN_bot...";
          await client.join({{
            sdkKey: config.sdkKey,
            signature: config.signature,
            meetingNumber: config.meetingNumber,
            password: config.password,
            userName: config.userName,
            userEmail: config.userEmail,
            zak: config.zakToken
          }});
          await ingestInputs([
            {{
              input_type: "participant_state",
              speaker: config.userName || delegateName,
              source: "zoom_join_page",
              metadata: {{ state: "joined", event: "local_join_success" }}
            }},
            {{
              input_type: "meeting_state",
              source: "zoom_join_page",
              metadata: {{ status: "active", state: "joined" }}
            }}
          ]);
          runtimeState.sessionClockStartMs = Date.now();
          runtimeState.completionArmed = true;
          runtimeState.joined = true;
          runtimeState.launchMode = "browser";
          await ingestAttendeeSnapshot(client, "post_join");
          scheduleAttendeeSnapshot(client, "post_join_settled", 1500);
          sendHeartbeat("browser_joined").catch(() => undefined);
          startAudioObserver().catch((error) => {{
            console.warn("Failed to start continuous local audio observation.", error);
          }});
          statusEl.textContent = "WooBIN_bot joined the meeting in browser.";
          return true;
        }} catch (error) {{
          const detail = formatJoinError(error);
          statusEl.textContent = desktopReady
            ? "Browser join failed: " + detail + " | Use Zoom Workplace instead."
            : "Browser join failed: " + detail;
          console.warn("Browser join failed.", error);
          joinButton.disabled = false;
          browserJoinButton.disabled = false;
          return false;
        }}
      }}

      function scheduleAutoJoin() {{
        if (!autoJoinMode || runtimeState.autoJoinRequested) {{
          return;
        }}
        runtimeState.autoJoinRequested = true;
        if (autoJoinMode === "desktop") {{
          statusEl.textContent = "Auto-launching Zoom Workplace...";
          openZoomWorkplace();
          return;
        }}
        const deadlineAt = Date.now() + 30000;
        const attempt = async () => {{
          if (runtimeState.joined || runtimeState.completeRequested) {{
            return;
          }}
          const joined = await joinMeetingInBrowser();
          if (joined) {{
            return;
          }}
          if (Date.now() >= deadlineAt) {{
            runtimeState.autoJoinRequested = false;
            statusEl.textContent = "Auto browser join timed out. Use Join in Browser manually.";
            return;
          }}
          window.setTimeout(() => {{
            attempt().catch((error) => {{
              console.warn("Auto browser join retry failed.", error);
            }});
          }}, 700);
        }};
        window.setTimeout(() => {{
          attempt().catch((error) => {{
            console.warn("Auto browser join failed.", error);
          }});
        }}, 250);
      }}

      joinButton.addEventListener("click", openZoomWorkplace);
      browserJoinButton.addEventListener("click", joinMeetingInBrowser);
      window.addEventListener("load", scheduleAutoJoin);
      document.addEventListener("visibilitychange", () => {{
        const reason = document.visibilityState === "hidden" ? "visibility_hidden" : "visibility_visible";
        sendHeartbeat(reason).catch(() => undefined);
      }});
      window.addEventListener("pageshow", () => {{
        ensureHeartbeatLoop();
        sendHeartbeat("pageshow").catch(() => undefined);
      }});
      window.addEventListener("beforeunload", () => {{
        stopHeartbeatLoop();
        const canAutoComplete = runtimeState.completeRequested
          || (runtimeState.completionArmed && runtimeState.joined);
        if (runtimeState.audioObserverRunning) {{
          sendStopObserverBeacon();
        }}
        if (canAutoComplete) {{
          runtimeState.completeRequested = true;
          sendCompleteBeacon();
        }}
      }});
      window.addEventListener("pagehide", () => {{
        stopHeartbeatLoop();
        const canAutoComplete = runtimeState.completeRequested
          || (runtimeState.completionArmed && runtimeState.joined);
        if (runtimeState.audioObserverRunning) {{
          sendStopObserverBeacon();
        }}
        if (canAutoComplete) {{
          runtimeState.completeRequested = true;
          sendCompleteBeacon();
        }}
      }});
      ensureHeartbeatLoop();
    </script>
  </body>
</html>"""
        return HTMLResponse(html)

    async def leave_page(request: Request) -> HTMLResponse:
        session = service.get_session(request.path_params["session_id"])
        if session is None:
            return HTMLResponse("<h1>Unknown delegate session</h1>", status_code=404)

        leave_completion_error: str | None = None
        try:
            session = await service.complete_session(session.session_id)
        except Exception as exc:
            refreshed = service.get_session(session.session_id)
            if refreshed is not None:
                session = refreshed
            leave_completion_error = str(exc).strip() or exc.__class__.__name__

        complete_url = f"{_base_url(request)}/delegate/sessions/{session.session_id}/complete"
        session_url = f"{_base_url(request)}/delegate/sessions/{session.session_id}"

        html = f"""<!doctype html>
<html lang="ko">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>WooBIN_bot Meeting Wrap-up</title>
    <style>
      body {{
        margin: 0;
        font-family: "Segoe UI", sans-serif;
        background: #f4f6fb;
        color: #162033;
      }}
      main {{
        max-width: 860px;
        margin: 0 auto;
        padding: 28px 20px 40px;
      }}
      .card {{
        background: #fff;
        border: 1px solid #d8dfec;
        border-radius: 16px;
        padding: 20px;
        box-shadow: 0 10px 30px rgba(17, 24, 39, 0.06);
      }}
      h1 {{
        margin: 0 0 8px;
        font-size: 28px;
      }}
      p {{
        line-height: 1.5;
      }}
      .status {{
        margin: 16px 0;
        font-size: 15px;
        min-height: 24px;
      }}
      .links {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        margin-top: 16px;
      }}
      a.button {{
        display: inline-block;
        border-radius: 999px;
        padding: 12px 18px;
        font-weight: 600;
        background: #eef2ff;
        color: #23314f;
        text-decoration: none;
      }}
      pre {{
        white-space: pre-wrap;
        background: #f7f9fc;
        border-radius: 12px;
        padding: 14px;
        min-height: 120px;
      }}
    </style>
  </head>
  <body>
    <main>
      <section class="card">
        <h1>WooBIN_bot Meeting Wrap-up</h1>
        <p>회의를 종료했습니다. 지금부터 로컬 AI 본체가 회의 내용을 정리하고 요약/PDF 생성을 마무리합니다.</p>
        <div class="status" id="status">요약과 PDF를 생성하는 중입니다...</div>
        <div class="links" id="links"></div>
        <pre id="summary"></pre>
      </section>
    </main>
    <script>
      const completeUrl = {json.dumps(complete_url)};
      const sessionUrl = {json.dumps(session_url)};
      const leaveCompletionError = {json.dumps(leave_completion_error)};
      const statusEl = document.getElementById("status");
      const linksEl = document.getElementById("links");
      const summaryEl = document.getElementById("summary");

      function renderLinks(session) {{
        linksEl.innerHTML = "";
        const summaryExports = Array.isArray(session && session.summary_exports) ? session.summary_exports : [];
        const transcriptExports = Array.isArray(session && session.transcript_exports) ? session.transcript_exports : [];
        for (const item of [...summaryExports, ...transcriptExports]) {{
          if (!item || !item.path) {{
            continue;
          }}
          const link = document.createElement("a");
          link.className = "button";
          link.href = item.path;
          link.target = "_blank";
          link.rel = "noreferrer";
          link.textContent = item.label || item.format || "artifact";
          linksEl.appendChild(link);
        }}
      }}

      async function finalizeSession() {{
        if (leaveCompletionError) {{
          console.warn("Leave-page server-side completion failed.", leaveCompletionError);
        }}
        try {{
          const response = await fetch(completeUrl, {{ method: "POST", credentials: "same-origin" }});
          const payload = await response.json();
          if (!response.ok || !payload.ok) {{
            throw new Error(payload.error || "자동 완료에 실패했습니다.");
          }}
          const session = payload.session || {{}};
          statusEl.textContent = "회의 요약과 PDF 생성이 완료되었습니다.";
          summaryEl.textContent = session.summary || "요약문이 아직 없습니다.";
          renderLinks(session);
        }} catch (error) {{
          statusEl.textContent = "자동 완료 중 오류가 발생했습니다. 세션 상태를 다시 불러옵니다.";
          try {{
            const fallback = await fetch(sessionUrl, {{ credentials: "same-origin" }});
            const fallbackPayload = await fallback.json();
            if (fallback.ok && fallbackPayload.ok && fallbackPayload.session) {{
              summaryEl.textContent = fallbackPayload.session.summary || "요약문이 아직 없습니다.";
              renderLinks(fallbackPayload.session);
            }}
          }} catch (_fallbackError) {{
          }}
          console.warn("Failed to auto-complete on leave page.", error);
        }}
      }}

      finalizeSession();
    </script>
  </body>
</html>"""
        return HTMLResponse(html)

    async def append_transcript(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            session = await service.append_transcript(request.path_params["session_id"], payload)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "session": session.to_dict()})

    async def append_chat(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            session, reply = await service.handle_chat_message(request.path_params["session_id"], payload)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "session": session.to_dict(), "reply": reply})

    async def ingest_inputs(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            session, result = await service.ingest_inputs(request.path_params["session_id"], payload)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "session": session.to_dict(), "ingest_result": result})

    async def observe_window(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            session, result = await service.observe_window(request.path_params["session_id"], payload)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except (ValueError, LocalObserverError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "session": session.to_dict(), "observation": result})

    async def observe_audio(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            session, result = await service.observe_system_audio(request.path_params["session_id"], payload)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except (ValueError, LocalObserverError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "session": session.to_dict(), "observation": result})

    async def start_observe_audio(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            session, result = await service.start_audio_observer(request.path_params["session_id"], payload)
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except (ValueError, LocalObserverError, RuntimeError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "session": session.to_dict(), "observation": result})

    async def stop_observe_audio(request: Request) -> JSONResponse:
        try:
            session, result = await service.stop_audio_observer(request.path_params["session_id"])
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        return JSONResponse({"ok": True, "session": session.to_dict(), "observation": result})

    async def complete_session(request: Request) -> JSONResponse:
        try:
            session = await service.complete_session(request.path_params["session_id"])
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        return JSONResponse({"ok": True, "session": session.to_dict()})

    async def summary_package(request: Request) -> JSONResponse:
        session = service.get_session(request.path_params["session_id"])
        if session is None:
            return JSONResponse({"ok": False, "error": "Unknown delegate session."}, status_code=404)
        return JSONResponse(
            {
                "ok": True,
                "session_id": session.session_id,
                "summary": session.summary,
                "action_items": session.action_items,
                "summary_packet": session.summary_packet,
                "summary_exports": session.summary_exports,
                "transcript_exports": session.transcript_exports,
                "artifact_handoffs": session.artifact_handoffs,
            }
        )

    async def session_status(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            session = service.update_status(
                request.path_params["session_id"],
                status=str(payload.get("status") or "").strip(),
                reason=str(payload.get("reason") or "").strip() or None,
            )
        except KeyError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "session": session.to_dict()})

    app = Starlette(
        on_startup=[startup],
        on_shutdown=[shutdown],
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/delegate/runtime/overview", runtime_overview, methods=["GET"]),
            Route("/delegate/sessions", list_sessions, methods=["GET"]),
            Route("/delegate/sessions", create_session, methods=["POST"]),
            Route("/delegate/sessions/{session_id}", get_session, methods=["GET"]),
            Route("/delegate/sessions/{session_id}/heartbeat", session_heartbeat, methods=["POST"]),
            Route("/delegate/sessions/{session_id}/readiness", session_readiness, methods=["GET"]),
            Route("/delegate/sessions/{session_id}/start", start_session, methods=["POST"]),
            Route("/delegate/sessions/{session_id}/status", session_status, methods=["POST"]),
            Route("/delegate/sessions/{session_id}/meeting-sdk-config", meeting_sdk_config, methods=["GET"]),
            Route("/delegate/join/{session_id}", join_page_v2, methods=["GET"]),
            Route("/delegate/leave/{session_id}", leave_page, methods=["GET"]),
            Route("/delegate/sessions/{session_id}/inputs", ingest_inputs, methods=["POST"]),
            Route("/delegate/sessions/{session_id}/observe/window", observe_window, methods=["POST"]),
            Route("/delegate/sessions/{session_id}/observe/audio", observe_audio, methods=["POST"]),
            Route("/delegate/sessions/{session_id}/observe/audio/start", start_observe_audio, methods=["POST"]),
            Route("/delegate/sessions/{session_id}/observe/audio/stop", stop_observe_audio, methods=["POST"]),
            Route("/delegate/sessions/{session_id}/transcript", append_transcript, methods=["POST"]),
            Route("/delegate/sessions/{session_id}/chat", append_chat, methods=["POST"]),
            Route("/delegate/sessions/{session_id}/complete", complete_session, methods=["POST"]),
            Route("/delegate/sessions/{session_id}/summary-package", summary_package, methods=["GET"]),
        ],
    )
    app.state.delegate_service = service
    return app


app = create_app()
