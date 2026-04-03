# 기준본 추출 메모

이 문서는 `LUSH_KR` 기준본에서 무엇을 새 작업 공간으로 가져왔는지, 그리고 무엇은 의도적으로 가져오지 않았는지 정리한 메모입니다.

## 이번에 가져온 것

- `src/local_meeting_ai_runtime/*.py`
- `src/lush_local_ai_launcher/*.py`
- `doc/templates/meeting-summary-reference.docx`

즉, 회의 엔진 코어와 런처 코어, PDF/docx 스타일 템플릿까지 새 작업 공간으로 복사했습니다.

## 의도적으로 가져오지 않은 것

- `.env`
- `data/`
- `.tmp/`
- `tests/`
- 사용자별 로컬 상태 파일
- 메테우스 프로젝트 바인딩 파일
- Telegram bot 토큰 파일

즉 기준본의 개인 비밀값, 회의 데이터, 로컬 상태는 새 작업 공간으로 복사하지 않았습니다.

## 아직 범용화되지 않은 지점

아래 항목들은 현재 복사된 코드 안에 남아 있지만, 이후 설정층으로 빼야 합니다.

- Zoom Client ID / Secret / SDK Key / SDK Secret
- Hugging Face token
- 로컬 오디오 장치 이름
- Codex 경로 / ffmpeg 경로 / whisper.cpp 경로
- Telegram bot 이름 / artifact route / project binding
- 런처 상태 파일 경로
- 개별 사용자의 작업 폴더 기준 상대 경로

## 특히 먼저 비워야 할 곳

### 1. 런타임과 launcher의 env 의존성

현재 엔진 코어는 `DELEGATE_*`, `ZOOM_*` 환경 변수를 직접 읽습니다.

범용 CLI 킷에서는 이 값을:

- CLI 설정 파일에서 관리하고
- 실행 직전에 env로 주입하거나
- 설정 객체로 변환하는 방식

중 하나로 정리해야 합니다.

### 2. 사용자 로컬 경로 fallback

예를 들어 artifact exporter에는 현재 사용자 PC 기준 fallback 경로가 남아 있습니다.

- Pandoc fallback
- LibreOffice fallback

이건 제품 관점에서 개인 기준이므로 나중에 제거하거나 일반화해야 합니다.

### 3. Telegram artifact 전달

현재 launcher 쪽 브리지는 메테우스 프로젝트/목적지 구조를 기본 전제로 두고 있습니다.

범용화 단계에서는:

- project channel route
- personal DM route
- no delivery

정도로 분리해 다룰 필요가 있습니다.

## 이번 단계의 원칙

- `LUSH_KR`는 계속 기준본으로 유지
- 새 작업 공간에서는 복사 후 수정
- 기준본의 코드와 설정은 절대 직접 수정하지 않음

## 다음 단계

1. 복사된 코어를 `CLI 설정 파일`과 연결
2. `.env` 직독 구간을 사용자 설정 스키마로 치환
3. `doctor`가 실제 copied engine 기준으로 준비 상태를 점검하도록 확장
4. `start/status/stop/create-session`을 새 킷에서 실제로 연결
5. 이후 launcher와 Telegram route 일반화로 확장

## 현재 진행 상황

현재는 위 단계 중 아래까지 진행된 상태입니다.

- CLI 설정 파일 생성
- 설정 파일 -> env 변환 레이어 추가
- 복사된 Zoom 런타임을 `start/status/stop`으로 관리하는 1차 연결
- `create-session`을 통해 런타임에 회의 세션 생성 요청 가능
- `launcher`를 `runtime_only`와 `launcher` 모드로 구분할 수 있는 설정층 추가
- copied launcher에서 Telegram runner on/off, artifact bot token 직접 주입, artifact route label/chat_id 제어 가능하게 확장

아직 하지 않은 것은:

- 메테우스 Telegram conversation runner 완전 일반화
- Telegram 대화 runner의 비메테우스 polling 구현
- 설치 자동화
- 웹사이트 연동 표면 정리
