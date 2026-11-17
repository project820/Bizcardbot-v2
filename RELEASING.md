# 버전 관리와 릴리스 규칙

이 규칙은 `main`에 올리는 모든 변경에 적용한다.
공개 `main`에는 릴리스 커밋으로 끝나는 묶음만 올린다.
문서만 고친 변경도 PATCH로 릴리스한다.
공개 뒤 수정은 새 PATCH 릴리스로 제공한다. 기존 태그는 옮기지 않는다.
문서만 고쳐도 변경 기록과 사용자 안내를 갱신한다.
공개되는 변경은 버전을 정하고 릴리스 노트를 함께 제공한다.

English summary: Use SemVer and Conventional Commits for every change to main. Keep versions synchronized, record user-facing changes in CHANGELOG, and update setup documentation. Run tests and privacy checks before publishing an annotated tag and a GitHub release with upgrade notes.

## 버전 선택

버전은 SemVer의 `MAJOR.MINOR.PATCH` 형식을 사용한다.

| 구분 | 변경 기준 | 예시 |
| --- | --- | --- |
| MAJOR | 기존 사용 방식이 깨지거나 설정 이전이 필요하다. | `2.0.0`으로 올린다. |
| MINOR | 기존 사용 방식을 유지하면서 기능을 추가한다. | `2.1.0`으로 올린다. |
| PATCH | 버그나 문서를 고친다. | `2.0.1`로 올린다. |

버전의 단일 원천은 `bizcard_intake/__init__.py`의 `__version__`이다.
다음 파일의 `version`도 같은 값으로 맞춘다.

- `onboarding/package.json`을 맞춘다.
- `cloud/package.json`을 맞춘다.
- 두 `package-lock.json`의 최상위와 `packages[""]` 값을 맞춘다.

`python3 scripts/release.py bump major|minor|patch`로 일괄 변경한다.
이 명령은 `[Unreleased]` 내용도 오늘 날짜의 새 버전 절로 옮긴다.
빈 `[Unreleased]` 절은 다음 작업을 위해 남긴다.

## 모든 커밋의 규칙

커밋 메시지는 Conventional Commits 형식으로 작성한다.
기본 형식은 `type(scope): 사용자에게 설명할 변경`이다.
`scope`는 생략할 수 있다.

| 유형 | 용도 | 예시 |
| --- | --- | --- |
| `feat` | 기능을 추가한다. | `feat(telegram): 설치 안내를 추가한다` |
| `fix` | 버그를 고친다. | `fix(env): 설정 파일 읽기를 고친다` |
| `docs` | 문서를 고친다. | `docs: 수동 설치 안내를 보완한다` |
| `chore` | 관리 작업을 한다. | `chore(release): v2.0.1` |
| `refactor` | 내부 구조를 고친다. | `refactor(parser): 공통 처리를 분리한다` |
| `test` | 검증을 보완한다. | `test(release): 버전 불일치를 검사한다` |

호환성이 깨지면 유형 뒤에 `!`를 붙인다.
또는 본문에 `BREAKING CHANGE: 이전에 필요한 작업`을 적는다.
예를 들어 `feat!: 설치 설정 형식을 변경한다`로 작성한다.

버전 선택은 메시지 유형보다 실제 사용자 영향을 우선한다.

`main`에 올리는 변경마다 `CHANGELOG.md`의 `[Unreleased]`에 한 줄을 남긴다.
코드 파일명보다 사용자가 겪는 변화를 쉽게 설명한다.
릴리스 커밋은 해당 줄들을 버전 절로 확정하고 `[Unreleased]`를 비운다.

작은 업데이트도 사용자에게 보이는 변화가 있으면 README와 관련 문서를 갱신한다.
설정이 바뀌면 `.env.example`과 이전 방법을 함께 갱신한다.

## 릴리스 절차

아래 명령은 저장소 루트에서 실행한다.
`public` 원격은 `github.com/project820/Bizcardbot-v2`를 가리켜야 한다.
`X.Y.Z`는 실제 릴리스 버전으로 바꾼다.

커밋과 공개 실행은 저장소 소유자의 명시적 승인을 받은 뒤 진행한다.
테스트 환경이 없으면 먼저 가상환경을 준비한다.

```sh
python3 -m venv .venv
.venv/bin/python3 -m pip install -r requirements.txt
```

1. **모든 테스트를 통과시킨다.**
   Python 테스트는 외부 Google 호출을 모킹해야 한다.
   Node 개발 도구는 미리 설치된 것을 사용한다.

   ```sh
   .venv/bin/python -m unittest discover -s tests
   npm --prefix onboarding test
   npm --prefix onboarding run typecheck
   npm --prefix cloud test
   npm --prefix cloud run typecheck
   ```

2. **버전을 올린다.**
   실제 변경에 맞는 구분 하나를 선택한다.
   아래 네 가지 릴리스 노트 머리글을 `[Unreleased]`에 미리 둔다.
   버전과 CHANGELOG 절을 이미 맞췄다면 이 단계를 건너뛴다.
   이번 `2.0.0`은 이미 맞춰져 있으므로 `bump`를 실행하지 않는다.

   ```sh
   .venv/bin/python scripts/release.py bump patch
   ```

3. **CHANGELOG의 릴리스 절을 확정한다.**
   `bump`가 만든 `[X.Y.Z] - YYYY-MM-DD` 날짜와 내용을 확인한다.
   아래 네 가지 릴리스 노트 항목을 모두 채운다.
   자동 `check`는 네 항목의 존재와 내용 완성을 확인하지 않는다.
   저장소 소유자가 직접 빠진 항목을 확인한다.

4. **사용자 안내를 갱신한다.**
   README의 `새 소식 (What's new)`에 현재 버전을 적는다.
   관련 문서와 `.env.example`도 확인한다.

   변경할 설정이 없으면 릴리스 노트에 그대로 적는다.

5. **공개 파일에서 개인 정보와 비밀값을 검사한다.**
   `.env`와 `data/`를 커밋하지 않는다.
   아래 사전 검사는 Git 추적 파일과 무시되지 않은 새 파일을 검사한다.

   탐지 메시지는 값 대신 파일과 행 번호만 보여준다.

   ```sh
   .venv/bin/python scripts/release.py check --allow-dirty
   python3 scripts/release.py history
   git diff --check
   git add <검토한 파일>
   git diff --cached --stat
   ```

   자동 검사는 토큰 형식과 주요 개인 메일 도메인을 탐지한다.
   이름과 연락처 등 나머지 개인 정보는 공개할 차이에서 직접 확인한다.
   `history`는 모든 참조의 커밋 트리에서 중복 blob을 한 번씩 검사한다.
   비밀값과 개인정보 패턴, 이미지 파일을 발견하면 실패한다.
   결과에는 커밋 짧은 해시와 경로, 유형만 표시한다.
   바이너리 blob의 내용은 검사하지 않는다.
   휴대폰 테스트 값 `010-1234-5678`, `010-5555-0xxx`, `010-0000-0000`, `010-1111-2222`, `010-9999-0000`은 허용한다.

6. **릴리스 커밋을 만든다.**
   5단계에서 검토한 파일만 스테이징했는지 확인한다.
   커밋 메시지는 `chore(release): vX.Y.Z`로 작성한다.

   ```sh
   git commit -m "chore(release): vX.Y.Z"
   ```

7. **엄격한 검사를 통과한 뒤 주석 태그를 만든다.**
   이 시점에는 작업트리가 깨끗하고 같은 태그가 없어야 한다.
   태그를 만든 뒤에는 태그 미존재 검사가 실패하므로 먼저 검사한다.

   ```sh
   .venv/bin/python scripts/release.py check
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   ```

8. **main과 주석 태그를 공개 원격으로 올린다.**

   ```sh
   git push public main --follow-tags
   ```

9. **CHANGELOG 해당 절로 GitHub 릴리스를 만든다.**
   공개 전에 추출 파일의 네 가지 항목을 확인한다.

   ```sh
   .venv/bin/python scripts/release.py notes X.Y.Z > /tmp/bizcard-release-notes.md
   gh release create vX.Y.Z --draft --repo project820/Bizcardbot-v2 --verify-tag --title "vX.Y.Z" --notes-file /tmp/bizcard-release-notes.md
   ```

10. **초안의 본문과 링크를 확인한 뒤 공개한다.**
    GitHub 초안에서 업그레이드 명령과 `.env.example` 링크를 확인한다.
    Release 본문의 저장소 파일 링크는 태그를 포함한 절대 URL을 쓴다.

    ```sh
    gh release edit vX.Y.Z --repo project820/Bizcardbot-v2 --draft=false
    ```

## 릴리스 노트 형식

CHANGELOG는 Keep a Changelog 형식을 따른다.
변경 유형별 절은 아래 표에서 필요한 것만 사용한다.

| 절 | 내용 |
| --- | --- |
| `Added` | 추가한 기능을 적는다. |
| `Changed` | 달라진 동작을 적는다. |
| `Fixed` | 고친 문제를 적는다. |
| `Removed` | 제거한 기능을 적는다. |
| `Security` | 보안 개선을 적는다. |

각 릴리스에는 다음 네 항목을 한국어로 작성한다.
없는 변경은 `없음`으로 명시한다.

```markdown
## [X.Y.Z] - YYYY-MM-DD

### 사용자에게 보이는 변화

#### Fixed

- 사용자가 확인할 수 있는 개선을 설명한다.

### 업그레이드 방법

1. 필요한 준비를 안내한다.
2. 실제 실행할 명령을 적는다.

### 설정 변경

- 바뀐 설정 이름과 기존 사용자의 조치를 적는다.

### 알려진 문제

- 제한 사항과 우회 방법을 적는다.
```

## 검사 명령

`check`는 버전 일치와 CHANGELOG 날짜를 확인한다.
README의 새 소식과 태그 미존재도 확인한다.
작업트리와 공개 대상 파일의 비밀값 패턴도 검사한다.

`--allow-dirty`는 커밋 전 검토에만 사용한다.
`--allow-existing-tag`는 이미 붙은 태그를 검토할 때만 사용한다.
릴리스 실행 전에는 두 옵션 없이 `check`를 통과해야 한다.

스크립트는 커밋과 태그를 만들거나 외부 서비스에 접속하지 않는다.
