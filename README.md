# ZOOM_MEETING_BOT

`ZOOM_MEETING_BOT`은 기준본의 회의 엔진 흐름을 유지한 채, 다른 사용자도 자기 PC에서 자기 Zoom 미팅 봇을 만들 수 있게 하기 위한 범용 CLI 킷입니다.

이 저장소의 가장 중요한 원칙은 아래 두 가지입니다.

- 범용화는 엔진을 새로 만드는 것이 아니라, 같은 엔진을 다른 사용자 설정으로 실행 가능하게 만드는 쪽에서 한다.

## 이 문서를 어떻게 읽으면 좋은가

처음 받는 사람은 아래 순서로 보면 됩니다.

1. `어떤 방식으로 쓸지 고르기`
2. `진짜 처음 시작하는 순서`
3. `configure에서 무엇을 넣는지 이해하기`
4. `회의 한 번 돌려보기`
5. `문제 생기면 어디를 확인하는지 보기`

---

## 어떤 방식으로 쓸지 먼저 고르기

이 킷은 크게 두 방식으로 쓸 수 있습니다.

### 1. `runtime_only`

Zoom 회의 엔진만 돌립니다.

이런 사람에게 맞습니다.

- Telegram 자동 전송은 아직 필요 없음
- 먼저 `회의 입장 -> 수집 -> 전사 -> 요약 -> PDF 생성`만 보고 싶음
- 가장 단순한 상태로 엔진 자체를 검증하고 싶음

### 2. `launcher`

Zoom 회의 엔진 + 결과물 전달 계층까지 같이 돌립니다.

이런 사람에게 맞습니다.

- 회의 종료 후 PDF를 Telegram으로 자동 전달하고 싶음
- 이후 메테우스나 Telegram runner까지 확장할 가능성이 있음

처음 테스트는 보통 아래 둘 중 하나로 시작하면 됩니다.

- PDF까지만 먼저 보고 싶다:
  `runtime_only`
- PDF를 개인 1:1 DM으로 자동 전송까지 보고 싶다:
  `launcher_dm`

---

## 운영체제가 달라도 명령은 같습니다

사용자는 Windows든 macOS든 같은 CLI 명령을 씁니다.

- `quickstart`
- `create-session`
- `status`
- `stop`

예시를 그대로 따라칠 때는 `.\scripts\zoom-meeting-bot.ps1` 또는 `./scripts/zoom-meeting-bot.sh`를 쓰는 쪽을 기본으로 보시면 됩니다.
bare `zoom-meeting-bot` 명령은 `.venv`를 활성화했을 때만 바로 동작할 수 있습니다.

차이는 내부에서만 처리합니다.

- Windows:
  - `winget`
  - PowerShell
  - 현재 검증된 오디오 경로
- macOS:
  - `brew`
  - shell script
  - macOS용 오디오 경로

즉 표면 명령은 같고, 설치기와 오디오 어댑터만 OS별로 갈라집니다.

---

## 진짜 처음 시작하는 순서

### Windows

```powershell
.\scripts\bootstrap.ps1
.\scripts\zoom-meeting-bot.ps1 quickstart --preset launcher_dm --yes
.\scripts\zoom-meeting-bot.ps1 create-session "회의링크" --passcode "암호" --open
```

### macOS

```bash
./scripts/bootstrap.sh
./scripts/zoom-meeting-bot.sh quickstart --preset launcher_dm --yes
./scripts/zoom-meeting-bot.sh create-session "회의링크" --passcode "암호" --open
```

macOS first-run notes:

- `bootstrap.sh` assumes `python3` is available.
- `bootstrap.sh` installs Homebrew automatically when it is missing.
- `quickstart` installs `pandoc`, `LibreOffice`, `ffmpeg`, `whisper-cpp`, and `BlackHole 2ch` when they are missing, and it also downloads the bundled default `whisper.cpp` model automatically if the repository does not already contain it.
- If `BlackHole 2ch` is installed for the first time, macOS may require one reboot before meeting-output capture becomes available.
- If CUDA is unavailable on macOS, the final offline transcription path uses the local CPU `faster-whisper` backend instead of dropping straight to live-only quality.

위 흐름의 뜻은 이렇습니다.

- `bootstrap`
  - `.venv`를 만들고, 이 킷 자체를 설치합니다.
- `setup`
  - 필요한 디렉터리를 만들고
  - Python 패키지와 외부 도구 설치를 진행합니다.
- `init`
  - 첫 설정 파일을 만듭니다.
- `configure`
  - 실제 사용자 값들을 입력합니다.
- `doctor`
  - 이 PC에서 지금 실행 가능한 상태인지 확인합니다.

`doctor`에서 막히는 문제가 없을 때만 다음 단계로 가는 게 좋습니다.

---

## preset은 뭘 고르면 되나

### `--preset runtime_only`

가장 단순한 시작점입니다.

이 preset을 고르면:

- Zoom 회의 엔진만 실행
- Telegram 자동 전송 없음
- 먼저 PDF까지 되는지 보기 쉬움

### `--preset launcher_dm`

회의 종료 후 PDF를 Telegram 개인 1:1 DM으로 보내는 기본 preset입니다.

이 preset을 고르면:

- `execution_mode = launcher`
- Telegram은 켜짐
- 대화 route는 끔
- PDF artifact route는 `personal_dm`

### `--preset launcher_metheus`

메테우스 프로젝트 route까지 고려하는 preset입니다.

이 preset을 고르면:

- `execution_mode = launcher`
- Telegram은 켜짐
- conversation/artifact route가 `metheus_project`
- launcher backend가 `metheus_cli`

---

## configure에서 무엇을 넣나

`configure` 단계는 대체로 아래 순서로 묻습니다.

### 1. 기본 프로필

- `bot 이름`
  - 사용자에게 보이는 봇 이름
  - 예: `WooBIN_bot`
- `작업 공간 이름`
  - 내부 식별용 이름
  - 보통 기본값 그대로 두어도 됩니다

### 2. Zoom 앱 정보

아래 값은 Zoom App Marketplace에서 가져옵니다.

- `Client ID`
- `Client Secret`

기준은:

- `General App`
- `Meeting SDK 활성화`
- `programmatic join use case 활성화`

입니다.

### 3. 로컬 AI 본체 정보

- `Hugging Face token`
- `회의 출력 장치 이름`
- `codex 명령어`
- `pandoc 명령어`
- `LibreOffice(soffice) 명령어`
- 필요하면 `whisper.cpp` 경로

중요한 점:

- 마이크는 기본적으로 Windows/macOS 기본 마이크를 씁니다.
- 회의 출력 장치는 직접 맞춰주는 편이 안전합니다.

### 4. 런타임 정보

- `execution_mode`
  - `runtime_only`
  - `launcher`
- `audio_mode`
  - 보통 `conversation` 그대로 두면 됩니다.

### 5. Telegram 정보

Telegram을 켜면 아래를 입력합니다.

- `bot_name`
- `bot_token`

그리고 route를 고릅니다.

#### conversation route

현재 지원 개념:

- `none`
- `metheus_project`

#### PDF artifact route

현재 지원 개념:

- `none`
- `personal_dm`
- `telegram_chat`
- `metheus_project`

예를 들어 “PDF를 내 개인 DM으로만 받고 싶다”면:

- `conversation_route = none`
- `artifact_route = personal_dm`
- `artifact_route.chat_id = 내 1:1 DM chat_id`

---

## setup은 무엇을 설치하나

`setup` 단계는 아래를 순서대로 묻고 진행합니다.

### 기본 디렉터리

- `data/exports`
- `data/audio`
- `.tmp/zoom-meeting-bot`

### Python 패키지

- Windows:
  - `.[observer-windows,meeting-quality]`
- macOS:
  - `.[observer-macos,meeting-quality]`

### 외부 도구

- `pandoc`
- `LibreOffice`
- `ffmpeg`

### macOS additional prerequisites

- `python3`
- macOS Microphone and Screen Recording permissions
- If `BlackHole 2ch` is installed during `quickstart`, reboot macOS once before the first real meeting capture

### 모델 준비

- 전사 모델
- 화자 분리 모델

각 단계는 자동 강행이 아니라 `Y/N`로 물어봅니다.

---

## doctor는 무엇을 보나

`doctor` 단계는 아래를 봅니다.

- 설정 파일이 있는지
- Zoom Client ID/Secret이 있는지
- `codex`, `pandoc`, `soffice`가 잡히는지
- `soundcard`, `soundfile`, `faster-whisper`, `pyannote.audio`가 있는지
- GPU/CUDA가 잡히는지
- `whisper.cpp` 경로가 있는지
- Telegram route 조합이 현재 모드에서 지원되는지
- launcher 모드에 필요한 backend 조건이 맞는지

추천은:

- `runtime_only`로 시작하면:
  - Windows: `.\scripts\zoom-meeting-bot.ps1 doctor --mode runtime_only`
  - macOS: `./scripts/zoom-meeting-bot.sh doctor --mode runtime_only`
- `launcher`로 시작하면:
  - Windows: `.\scripts\zoom-meeting-bot.ps1 doctor --mode launcher`
  - macOS: `./scripts/zoom-meeting-bot.sh doctor --mode launcher`

`Doctor finished with no blocking problems.`가 보일 때까지 정리하고 다음 단계로 가는 게 좋습니다.

---

## 회의를 한 번 돌려보는 가장 쉬운 순서

### A. PDF까지만 먼저 보고 싶을 때

```powershell
.\scripts\zoom-meeting-bot.ps1 quickstart --preset runtime_only --yes
.\scripts\zoom-meeting-bot.ps1 create-session "https://us06web.zoom.us/j/..." --passcode "123456" --open
```

회의 종료 후:

- `data/exports/<session_id>/...pdf`
- `...docx`
- `...md`

를 보면 됩니다.

### B. PDF를 개인 1:1 DM으로도 받고 싶을 때

```powershell
.\scripts\zoom-meeting-bot.ps1 quickstart --preset launcher_dm --yes
.\scripts\zoom-meeting-bot.ps1 create-session "https://us06web.zoom.us/j/..." --passcode "123456" --open
```

회의 종료 후:

- `data/exports/<session_id>/...pdf`
- Telegram 개인 DM

둘 다 확인하면 됩니다.

---

## 세션 관련 명령

### 세션 생성

```powershell
.\scripts\zoom-meeting-bot.ps1 create-session "회의링크" --passcode "암호" --open
```

이 명령은:

- 회의 링크에서 `meeting_number`를 자동 추출하고
- 세션을 만들고
- `--open`이면 join page까지 바로 엽니다.

### 세션 목록 보기

```powershell
.\scripts\zoom-meeting-bot.ps1 list-sessions
```

### 세션 상세 보기

```powershell
.\scripts\zoom-meeting-bot.ps1 show-session <session_id>
```

### 세션 페이지 열기

```powershell
.\scripts\zoom-meeting-bot.ps1 open-session <session_id>
```

---

## package 명령은 무엇을 하나

`zoom-meeting-bot package`는 현재 작업 공간을 배포용 zip으로 묶습니다.

기본 출력 경로:

- `./.dist/zoom-meeting-bot-alpha-YYYYMMDD-HHMMSS.zip`

포함:

- `src/`
- `scripts/`
- `schemas/`
- `doc/`
- `README.md`
- 예시 설정 파일

제외:

- `.venv`
- `.tmp`
- `data`
- `__pycache__`
- `*.pyc`
- `*.egg-info`

즉 다른 담당자에게 “이 킷 코드와 스크립트”만 전달하고 싶을 때 쓰는 명령입니다.

---

## 완전 처음 받는 사람을 위한 추천 순서

진짜 처음 받는 분은 아래 3단계만 먼저 따라가시면 됩니다.

### 1. 부트스트랩

Windows:

```powershell
.\scripts\bootstrap.ps1
```

macOS:

```bash
./scripts/bootstrap.sh
```

이 단계는 `.venv`와 CLI 실행 기반을 준비하는 단계입니다.

### 2. 처음 준비 한 번에 끝내기

개인 Telegram DM까지 받아보는 기본 경로는 OS별로 아래 한 줄입니다.

Windows:

```powershell
.\scripts\zoom-meeting-bot.ps1 quickstart --preset launcher_dm --yes
```

macOS:

```bash
./scripts/zoom-meeting-bot.sh quickstart --preset launcher_dm --yes
```

macOS notes:

- If bundled `whisper.cpp` does not already contain a macOS CLI, `setup` first tries a Homebrew `whisper-cpp` install and only builds `tools/whisper.cpp/build-macos` as a last resort.
- If CUDA is unavailable on macOS, final offline transcription uses the local CPU `faster-whisper` path.

이 명령은 아래를 한 번에 처리합니다.

- `init`
- `configure`
- `setup`
- `doctor`
- `start`

- `setup` tries the bundled `tools/whisper.cpp` assets first. If the default `ggml-large-v3-turbo-q5_0.bin` model is missing, it downloads it with the upstream `whisper.cpp` script, then on macOS it tries a Homebrew `whisper-cpp` install and only falls back to a local `build-macos` build when necessary.

설명:

- `launcher_dm`은 회의 종료 후 PDF를 개인 Telegram DM으로 받는 기본 preset입니다.
- `--yes`는 설치/준비 단계에서 추천 선택지를 자동으로 받습니다.
- Windows에서 NVIDIA GPU가 감지되면 `setup`이 CUDA용 `torch`/`torchaudio`도 자동으로 맞춰서 final transcription 품질 경로를 살리려 시도합니다.
- PDF까지만 먼저 보고 싶으면 아래처럼 OS별 `runtime_only`를 쓰시면 됩니다.

Windows:

```powershell
.\scripts\zoom-meeting-bot.ps1 quickstart --preset runtime_only --yes
```

macOS:

```bash
./scripts/zoom-meeting-bot.sh quickstart --preset runtime_only --yes
```

### 3. 회의 세션 만들기

Windows:

```powershell
.\scripts\zoom-meeting-bot.ps1 create-session "회의링크" --passcode "암호" --open
```

macOS:

```bash
./scripts/zoom-meeting-bot.sh create-session "회의링크" --passcode "암호" --open
```

이 명령은 회의 세션을 만들고, 런타임이 아직 안 떠 있으면 자동으로 시작한 뒤 회의 진입 페이지까지 엽니다.

### 회의가 끝나면 확인할 것

- `data/exports/<session_id>/...pdf`
- preset과 route 설정에 따라 Telegram DM 또는 지정 route 전달

### 세부 단계를 직접 나눠서 하고 싶다면

아래 저수준 명령도 그대로 사용할 수 있습니다.

```powershell
.\scripts\zoom-meeting-bot.ps1 setup
.\scripts\zoom-meeting-bot.ps1 init --preset launcher_dm
.\scripts\zoom-meeting-bot.ps1 configure
.\scripts\zoom-meeting-bot.ps1 doctor --mode launcher
.\scripts\zoom-meeting-bot.ps1 start
.\scripts\zoom-meeting-bot.ps1 status
.\scripts\zoom-meeting-bot.ps1 create-session "회의링크" --passcode "암호" --open
```

---

## 문제 생기면 어디서 먼저 보나

### 1. 설정이 맞는지

```powershell
.\scripts\zoom-meeting-bot.ps1 show-config
```

### 2. 준비 상태가 맞는지

```powershell
.\scripts\zoom-meeting-bot.ps1 doctor --mode launcher
```

### 3. 런타임이 살아 있는지

```powershell
.\scripts\zoom-meeting-bot.ps1 status
```

### 4. 진단 번들 만들기

```powershell
.\scripts\zoom-meeting-bot.ps1 support-bundle
```

이 JSON을 주면 다른 사람 PC 문제를 보기 훨씬 쉬워집니다.

---

## 지금 이 킷의 목표

이 저장소의 목표는 `WooBIN_bot` 복제본 하나를 만드는 것이 아닙니다.

목표는:

- 다른 사용자도
- 자기 Zoom 앱 값
- 자기 Hugging Face token
- 자기 Telegram route
- 자기 PC 환경

을 넣어서, 자기만의 Zoom 미팅 봇을 만들 수 있게 하는 것입니다.

단, 그 범용화는 어디까지나 `LUSH_KR`의 흐름, 결과물, 품질을 유지하는 범위 안에서만 진행합니다.
