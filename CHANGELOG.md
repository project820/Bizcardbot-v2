# 변경 기록

사용자가 알아야 할 변경을 이 문서에 기록한다.
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) 형식을 따른다.
버전은 [Semantic Versioning](https://semver.org/lang/ko/)을 따른다.

## [Unreleased]

## [2.1.1] - 2026-11-17

### 사용자에게 보이는 변화

#### Fixed

- 릴리스 노트의 저장소 파일 링크를 해당 버전 태그의 절대 주소로 바꿔 GitHub 릴리스에서도 열리게 한다.
- 공개 전 이력 검사가 테스트용 가짜 번호(010-0000-000x)를 개인정보로 오인하지 않게 한다.

봇의 동작은 v2.1.0과 같다.

### 업그레이드 방법

v2.1.0 사용자는 `git pull` 뒤 봇을 다시 시작한다. 봇 동작이 같으므로 건너뛰어도 된다.

### 설정 변경

없음

### 알려진 문제

v2.1.0과 같다.

## [2.1.0] - 2026-11-17

### 사용자에게 보이는 변화

#### Added

- 명함을 Mac 연락처에 저장하는 방식을 추가한다. 기존 Google 직접 연결(gws)도 그대로 쓸 수 있다.
  `setup-contacts`에서 Mac에 등록된 저장 계정을 선택한다.
  Mac의 Google 계정에 연락처 동기화를 켜면 Google Cloud 프로젝트 없이 사용한다.
- 봇 시작 시 Mac 연락처 접근 권한을 요청한다.
  거부 상태는 관리자 채팅 또는 허용된 채팅에 해결 방법을 알린다.
- `doctor`는 선택한 연락처 백엔드의 권한과 계정을 점검한다.

#### Changed

- gws 하위 프로세스는 `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file`을 강제한다.
  기존 필수 설정을 실행 시에도 적용해 keychain 복호화 실패로 인한 로그인 손실을 막는다.
  문서대로 file 방식으로 로그인한 기존 사용자는 같은 저장소를 사용한다.
  keychain 방식 로그인은 지원하지 않으며 file 방식 재로그인이 필요하다.

#### Fixed

- Mac 연락처 생성과 갱신 전에 저장 작업을 명함 파일에 기록한다.
  최초 후보 집합과 원래 백엔드·계정·갱신 대상을 보존한다.
  응답 유실 뒤 후보가 없어도 자동 재생성하지 않는다.
  재생성은 현재 확인 단계의 다시 저장 버튼을 소모한 경우만 허용한다.
- 오래된 UI 카드 사본이 Apple 저장 작업과 소모된 확인을 덮어쓰지 못하게 한다.
- 저장 중 수정 버튼은 상태를 변경하지 않고 저장 중 안내를 보낸다.
  저장 완료와 실패 기록은 최신 카드의 UI 정보를 보존한다.
  종료 상태를 기록한 뒤 이미지를 정리하며 사진 재시도도 카드 잠금을 사용한다.
- 다시 저장 결과에도 사진 실패와 메모 누락, 필드 검증 경고를 표시한다.
- 목록에 저장 중 상태를 표시하고 수정 가능한 명함에만 답장 수정을 안내한다.
- 관리자 미설정 시 허용 채팅으로 보내는 알림은 Mac 연락처 권한 알림으로 한정해 안내한다.
- 확인 단계마다 별도 nonce를 기록해 오래된 버튼과 중복 클릭을 차단한다.
  오래된 버튼에는 이미 처리됨을 응답하고 해당 메시지의 버튼을 제거한다.
- Mac 연락처 갱신은 기존 이름·조직 정보와 목록 값을 보존한다.
  재시도에서도 같은 값을 덧붙이는 합집합 갱신을 적용한다.
  대상 식별자가 사라지면 같은 계정에서 재조회하며 생성으로 전환하지 않는다.
- 사진 재시도도 기록된 계정을 유지한다.
  v2.0.0 명함의 기본 Google 저장과 재시도 동작을 유지한다.
- 한국어 성·이름이 분리된 Mac 연락처를 표시 이름으로도 대조한다.
- 잘못된 관리자 채팅 ID와 연락처 초기화 오류가 polling을 중단하지 않게 한다.
- Mac 권한 알림을 로컬에 기록해 시작·상태 점검·재시작 간 반복을 제한한다.
  같은 상태는 24시간 뒤 재알림하며 권한 상태 변경 시 다시 알린다.
- Google 점검 알림은 기존처럼 관리자 채팅이 지정된 경우에만 전송한다.
- Mac 연락처 사진 수정 전에 필요한 사진 키를 읽는다.
- Google 동기화 중 연락처 식별자가 바뀌면 사진 저장 대상을 다시 확인한다.
  중복 후보가 여러 개면 사진 저장 대상을 자동으로 바꾸지 않는다.

### 업그레이드 방법

1. v2.1.0 코드로 갱신한다.
2. Mac 연락처 방식을 원하면 `python3 -m bizcard_intake setup-contacts`를 실행한다.
3. Google을 원하면 시스템 설정 > 인터넷 계정에서 연락처 동기화를 켠다.
4. `python3 -m bizcard_intake doctor`로 준비 상태를 확인한다.
5. 봇을 실행한 Mac에서 Python의 연락처 접근 요청을 허용한다.

기존 gws 사용자는 설정을 유지한다.
미설정 백엔드의 기본값은 `google`이다.

### 설정 변경

- `BIZCARD_CONTACTS_BACKEND`에 `google` 또는 `apple`을 지정한다.
- `BIZCARD_APPLE_CONTAINER`에 Mac 연락처 계정 식별자를 저장한다.
  빈 값이면 Mac 연락처의 기본 계정을 쓴다.
- [.env.example](.env.example)에 선택 설정과 설치 명령을 추가한다.
- 직접 Python CLI는 프로세스 환경 변수가 `.env`보다 우선한다.
- `./scripts/start_bot.sh`는 `.env` 값을 export해 기존 환경 변수를 덮어쓴다.

### 알려진 문제

- Apple의 별도 entitlement가 없어 Mac 연락처 메모를 읽거나 저장하지 않는다.
  사용자 메모와 검증 경고는 명함 초안에 남기고 저장 결과에 경고한다.
- Google 서버로의 동기화는 macOS 계정 서비스가 담당한다.
  시험 연락처로 이름·전화·이메일·사진의 Google 연락처 동기화와 삭제 반영을 확인했다.
  동기화에는 Mac의 네트워크 상태에 따라 몇 초에서 몇 분이 걸릴 수 있다.
- 터미널과 launchd 봇은 연락처 접근 권한을 따로 요청할 수 있다.
  Python 실행 경로나 책임 프로세스가 달라져도 다시 요청할 수 있다.


## [2.0.0] - 2026-11-17

### 사용자에게 보이는 변화

#### Added

- `setup-telegram` 설치 도우미가 Telegram 봇 생성과 연결을 안내한다.
  도우미는 Telegram 공식 봇 만들기 링크 버튼을 보낸다. iPhone에서 전 과정을 확인했다.
- 설치 도우미가 확인한 사용자를 허용 목록에 자동 입력한다.
- `setup-telegram --manual`로 BotFather를 통한 수동 설정도 지원한다.
- Mac 없이 쓰는 Cloud 방식을 미리보기로 추가한다. 실제 서버에서는 시험하지 않았다.

#### Changed

- 명함 방향을 자동으로 바로잡는다. 명함 17장을 네 방향으로 돌린 68건을 모두 바로잡았다.
  다른 사진에서는 결과가 다를 수 있다.
- 실제 명함 17장으로 v1과 v2를 비교했다.
  방향 판정은 v1의 15/17에서 v2의 16/17로 개선됐다.
  처리 시간은 장당 약 4.1~4.4초로 비슷했다.
  비용도 장당 약 1.1~1.2원으로 비슷했다. 다른 사진에서는 결과가 다를 수 있다.
- 연락처 사진의 종이색을 스캐너처럼 하얗게 맞춘다.
  푸르거나 누렇게 찍힌 밝은 명함이 깨끗한 흰 종이로 보인다.
  금색·검정처럼 진한 색 명함은 그대로 둔다. 글자 인식에 쓰는 이미지는 바꾸지 않는다.
- 명함 1장 처리 시간은 중앙값 약 4.5초→4.0초로 줄었다(17장 1회 측정, 네트워크 변동 포함).
  두 번째 확인 모델(Haiku)의 응답을 짧게 줄였다(중앙값 3.4초→2.8초).
  17장 비교에서 연락처 값 손실은 관측되지 않았다.

#### Fixed

- 종이색 보정이 실패해도 보정 전 연락처 사진을 저장한다. OCR·AI 인식 이미지는 유지한다.
- NumPy가 없어도 CLI를 실행할 수 있다. `doctor`는 연락처 사진의 종이색 보정 중단을 경고한다.
- `setup`과 `doctor`를 포함한 CLI가 프로젝트의 `.env`를 읽는다.
  프로세스 환경 변수 값이 `.env`보다 우선한다.

#### Security

- 봇 시작 스크립트와 CLI는 `.env`를 셸 명령으로 실행하지 않고 설정 값으로만 읽는다.
- 같은 봇이 다른 곳에서도 실행되면 봇 사용자에게 Telegram 메시지로 알린다.
  토큰 도용일 때도 같은 알림이 온다.

#### Removed

- 공개하지 않은 iMessage 입력 시험 기능을 제거한다. 공개판 사용자는 영향이 없다.

### 업그레이드 방법

v1(`Biz-carDDD`)에서 쓰던 Telegram 봇과 Google 로그인은 그대로 쓴다.
아래 명령은 터미널에서 한 단계씩 실행한다.
예시는 옛 폴더와 새 폴더가 같은 상위 폴더에 있는 경우다.
기존 폴더의 경로는 실제 위치에 맞게 바꾼다.

1. 기존 봇을 멈춘다.
   자동 실행을 등록했다면 아래 명령을 실행한다.
   터미널에서 직접 켰다면 그 창에서 Control+C를 누른다.

   ```sh
   launchctl unload -w ~/Library/LaunchAgents/com.bizcard.intake.plist
   ```

2. 새 저장소를 받는다.

   ```sh
   git clone https://github.com/project820/Bizcardbot-v2.git
   cd Bizcardbot-v2
   ```

3. 설정과 저장 전 명함을 옮기고 라이브러리를 설치한다.
   `data` 폴더가 없다는 오류는 무시한다.

   ```sh
   cp ../Biz-carDDD/.env .env
   cp -R ../Biz-carDDD/data ./data
   python3 -m pip install -r requirements.txt
   ```

   v1의 `TELEGRAM_BOT_TOKEN`과 `TELEGRAM_ALLOWED_CHAT_IDS`는 그대로 쓴다.
   새로 설치할 때만 `python3 -m bizcard_intake setup-telegram`을 실행한다.

4. 봇을 시작한다. `Polling @봇이름`이 보이면 성공이다.

   ```sh
   ./scripts/start_bot.sh
   ```

5. 자동 실행을 쓰던 사용자는 Control+C로 봇을 끄고 새 폴더로 다시 등록한다.

   ```sh
   sed -e "s|__PROJECT_ROOT__|$PWD|g" -e "s|__HOME__|$HOME|g" scripts/com.bizcard.intake.plist \
     > ~/Library/LaunchAgents/com.bizcard.intake.plist
   launchctl load -w ~/Library/LaunchAgents/com.bizcard.intake.plist
   ```

6. 봇이 시작하지 않을 때만 환경을 점검한다.
   `doctor`는 프로젝트의 `.env`를 데이터로 읽는다.
   `doctor`가 안내한 누락 항목을 해결한 뒤 4단계부터 다시 진행한다.

   ```sh
   python3 -m bizcard_intake doctor
   ```

### 설정 변경

- v1의 `TELEGRAM_BOT_TOKEN`과 `TELEGRAM_ALLOWED_CHAT_IDS`는 그대로 쓴다.
  새 설치에서만 설치 도우미가 검증한 사용자를 자동으로 추가한다.
- `BIZCARD_ONBOARDING_URL`은 프로세스 환경 변수, 대상 `.env` 순으로 읽는다.
  미설정이면 기본 설치 도우미를 쓴다.
  빈 값이면 BotFather 수동 경로를 이용한다.
- OpenAI 키는 `.env`의 `OPENAI_API_KEY`나 `~/.config/openai/credentials.env`에서 읽는다.
  `BIZCARD_OPENAI_ENV_FILE`로 별도 키 파일을 지정할 수도 있다.
  v1에서 다른 프로젝트의 키 파일에 의존했다면 `.env`에 `OPENAI_API_KEY=`를 직접 적는다.
  키가 없으면 Codex CLI를 먼저 쓰고, 없으면 Claude CLI를 쓴다.
  선택한 경로에 따라 속도와 정확도가 달라진다.
- `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file`을 유지한다.
  기존 Google 로그인과 복호화 키를 새 환경에서 확인한다.
- 나머지 선택 설정은 [.env.example](https://github.com/project820/Bizcardbot-v2/blob/v2.0.0/.env.example)을 확인한다.

### 알려진 문제

- Cloud 미리보기는 배포 전이다. 운영 전환용으로 검증되지 않았다.
- 자동 설정은 프로젝트 운영자가 운영하는 설치 도우미(@BizcardSetupBot)를 거친다.
  도우미는 내 봇의 토큰을 저장하지 않는다.
  도우미는 명함 사진과 Google 계정에 접근하지 않는다.
  다만 Telegram 구조상 도우미 운영자는 내 봇의 토큰을 다시 조회할 수 있다.
  토큰이 있으면 봇에 온 메시지를 읽을 수 있다.
  이 점이 걸리면 `python3 -m bizcard_intake setup-telegram --manual`로 직접 만든다.
  수동 경로는 도우미를 거치지 않는다.
- 같은 토큰으로 봇을 두 곳에서 실행하면 충돌한다.
  먼저 다른 Mac이나 옛 폴더의 봇을 끈다.
  모르는 사용이 의심되면 `python3 -m bizcard_intake setup-telegram --manual`로 새 봇을 연결한다.
- 새 봇을 만든 직후 확인이 잠시 실패할 수 있다. 설치기는 최대 3회 시도한다.
  그래도 실패해 설치를 다시 실행하면 봇이 하나 더 만들어진다.
