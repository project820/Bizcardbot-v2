# cloud/ 독립 검토 지적 수정 결과

지적 3과 4를 수정했다.
`cloud/` 밖의 작업자 변경은 수정하지 않았다.
실제 외부 API 호출과 배포는 실행하지 않았다.

## 지적별 수정

| 지적 | 수정 결과 |
| --- | --- |
| 3. 두 isolate의 중복 저장 | 작업별 SQLite DO 하나로 전달하고, 원자적 `awaiting_confirmation → saving`을 확정한 실행만 연락처 생성을 시작한다. |
| 4. 지연된 put 중 저장 정체 | callback 영수증과 결정을 DO에 보존하고, 기존 Promise 반환 대신 새 처리 차례를 붙여 초안 완료 후 저장을 실행한다. |

`queued → drafting → awaiting_confirmation → saving → saved/unknown`을 구현했다.
초안 오류는 `failed`로 기록한다.
취소는 `cancelled`로 기록한다.
사진과 callback의 `update_id` 중복 차단도 DO가 처리한다.

DO의 입력 게이트 안에서는 저장소 트랜잭션만 수행한다.
외부 API를 기다리는 동안에도 callback을 기록할 수 있다.
초안 완료 전이가 최신 작업 상태를 읽어 대기 결정을 보존한다.

Google 연락처 생성 시도는 작업당 최대 1회다.
이미 `saving`인 작업은 재시작 후에도 자동 저장하지 않는다.
저장 기록과 함께 설정한 60초 DO alarm이 정체를 복구한다.
기한이 지난 `saving`은 `unknown`으로 바뀐다.
다시 저장하지 말고 Google 주소록을 확인하라고 안내한다.
늦은 Google 응답은 `unknown`을 덮어쓰지 않는다.

초안 처리에도 60초 watchdog을 설정한다.
중단된 초안은 자동 모델 재호출 없이 `failed`로 끝난다.
작업 생성 7일 뒤 alarm은 초안과 중복 영수증을 제거한다.
만료 정리 중 새 작업의 alarm을 지우는 경합도 방지했다.

## 무료 플랜 공식 근거

2026-11-17에 Parallel 검색으로 Cloudflare 공식 문서를 확인했다.
SQLite 기반 Durable Object는 Workers Free에서 사용 가능하다.
추가 유료 구독은 요구되지 않는다.

- https://developers.cloudflare.com/durable-objects/platform/pricing/
- https://developers.cloudflare.com/durable-objects/platform/limits/
- https://developers.cloudflare.com/durable-objects/api/sqlite-storage-api/

공식 요금 문서의 무료 한도를 README에 반영했다.

| 항목 | 한도 |
| --- | --- |
| DO 요청 | 100,000회/일이다. |
| DO 실행 시간 | 13,000 GB-s/일이다. |
| SQLite 읽기 | 5,000,000행/일이다. |
| SQLite 쓰기 | 100,000행/일이다. |
| SQLite 저장량 | 계정 전체 5GB다. |

무료 한도 초과 시 해당 작업은 오류로 실패한다.
무료 플랜 전환이나 과금 설정 변경은 실행하지 않았다.
기존 KV 기준 약 250장/일 계산은 제거했다.
정확한 장당 DO 사용량은 실서비스 측정이 필요하다.

alarm과 migration의 공식 근거도 확인했다.

- https://developers.cloudflare.com/durable-objects/api/alarms/
- https://developers.cloudflare.com/durable-objects/reference/durable-objects-migrations/

Wrangler에는 지원되는 `new_sqlite_classes` migration을 사용했다.
로컬 Explorer도 `JobObject`의 `use_sqlite:true`를 반환했다.

## 바꾼 파일

| 파일 | 변경 |
| --- | --- |
| `cloud/src/index.ts` | 작업 DO와 영수증 및 alarm을 구현했다. |
| `cloud/test/worker.test.ts` | 기존 검증을 DO로 바꾸고 경합·복구 회귀 시험을 추가했다. |
| `cloud/test/fixtures/queued-job.json` | KV 쓰기 횟수를 제거하고 DO 만료 시점을 추가했다. |
| `cloud/wrangler.toml` | KV와 Cron을 DO binding 및 SQLite migration으로 교체했다. |
| `cloud/README.md` | DO 흐름과 무료 한도 및 unknown 처리와 로컬 실행법을 갱신했다. |
| `cloud/WORK_REPORT.md` | 현재 수정과 검증 결과로 갱신했다. |

운영 의존성을 추가하지 않았다.
commit과 checkout 및 stash는 실행하지 않았다.
pip install도 실행하지 않았다.

## 실행한 명령과 결과

별도 표시가 없으면 `cloud/`에서 실행했다.

### 실패 재현

```sh
npx vitest run -t 'independent review regressions'
```

수정 전에 두 시험이 실패했다.
독립 Worker 모듈과 독립 KV 읽기 뷰에서 생성 호출이 2회 발생했다.
초안 put 완료 지연 중 callback에서는 `saved` 대신 `saving`이 남았다.
DO 전환 후 두 Worker 모듈을 같은 작업 DO로 연결해 검증했다.
지연된 put 중 callback도 저장 완료까지 검증했다.

### 타입 검사와 Vitest

```sh
npm run typecheck && npx vitest run
```

최종 타입 검사는 종료 코드 0을 반환했다.
Vitest는 1개 파일의 43개 시험을 모두 통과했다.

- 두 Worker 모듈의 동시 저장은 한 번만 생성했다.
- 다른 callback update ID의 동시 저장도 한 번만 생성했다.
- 초안 put 지연 중 callback을 받아 저장까지 완료했다.
- OCR 대기 중 저장 결정을 받아 완료 후 처리했다.
- 취소와 저장 경쟁은 먼저 수락한 결정 하나로 끝났다.
- 객체 재시작 후 queued 작업과 대기 결정을 복구했다.
- 중단된 saving은 alarm으로 unknown이 됐다.
- Google 성공 후 저장소 기록 실패도 unknown이 됐다.
- OAuth 대기 중 alarm 만료는 Google 생성을 막았다.
- Google의 늦은 응답도 unknown을 덮어쓰지 않았다.
- 7일 만료 정리는 개인정보와 영수증을 함께 제거했다.
- 만료 정리와 신규 작업이 겹쳐도 신규 alarm을 보존했다.
- LOCAL_MOCK은 global fetch를 호출하지 않았다.

### Wrangler와 curl

```sh
CLOUDFLARE_LOAD_DEV_VARS_FROM_DOT_ENV=false npx wrangler dev --env local --local --ip 127.0.0.1 --port 8789
curl -sS -i http://127.0.0.1:8789/health
curl -sS -i http://127.0.0.1:8789/webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Telegram-Bot-Api-Secret-Token: fixture-secret' \
  --data-binary @test/fixtures/photo-update.json
curl -sS -i http://127.0.0.1:8789/__local/jobs/900001
curl -sS -i http://127.0.0.1:8789/webhook \
  -H 'Content-Type: application/json' \
  -H 'X-Telegram-Bot-Api-Secret-Token: fixture-secret' \
  --data-binary @test/fixtures/save-update.json
curl -sS http://127.0.0.1:8789/cdn-cgi/local/explorer/api/workers/durable_objects/namespaces
```

Wrangler `4.163.0`이 로컬 서버를 실행했다.
`JOBS (JobObject)`는 local Durable Object로 표시됐다.
`LOCAL_MOCK=1`과 `DRY_RUN_SAVE=1`을 확인했다.
공개된 `fixture-secret`만 사용했다.
저장 callback 2건은 병렬 curl로도 전송했다.

| 확인 | 결과 |
| --- | --- |
| `/health` | HTTP 200이며 `storage:durable-object-sqlite`를 반환했다. |
| 사진 webhook | HTTP 200을 반환했다. |
| 초안 상태 조회 | `awaiting_confirmation`을 반환했다. |
| 병렬 저장 callback 2건 | 둘 다 HTTP 200을 반환했다. |
| 최종 상태 조회 | `saved`, `resourceName:dry-run`을 반환했다. |
| 잘못된 webhook secret | HTTP 401을 반환했다. |
| 로컬 SQLite | Explorer의 `use_sqlite:true`를 확인했다. |
| 서버 재시작 후 재전송 | 사진과 저장 모두 HTTP 200이며 기존 `saved`를 유지했다. |
| 별도 취소 작업 `900101` | 사진과 취소 모두 HTTP 200이며 `cancelled`로 끝났다. |
| 최종 코드의 새 작업 `900201` | 사진과 저장 모두 HTTP 200이며 `saved`, `resourceName:dry-run`으로 끝났다. |

재시작 후 동일 사진의 OpenAI 모의 호출은 반복되지 않았다.
취소 재현용 JSON은 무시되는 `cloud/.wrangler/`에 저장했다.
검증 후 Ctrl-C로 서버를 종료했다.
실제 Telegram·Google·OpenAI는 호출하지 않았다.

## 미해결 사항

요청한 지적 3과 4의 미해결 항목은 없다.
아래 검증과 제품 한계는 남는다.

| 항목 | 남은 한계와 다음 행동 |
| --- | --- |
| Google과 DO 사이의 중단 | 분산 트랜잭션이 없어 unknown은 실제 주소록에서 확인해야 한다. |
| 운영 환경 검증 | 호출 금지에 따라 실제 서비스와 다중 배포 환경을 시험하지 않았다. 별도 승인 후 배포 환경에서 검증해야 한다. |
| 기존 KV 기록 | 자동 이관을 구현하지 않았다. 필요하면 별도 이관 작업을 해야 한다. |
| 무료 처리량 | 요청·실행 시간·SQLite 사용량을 실사용으로 측정해야 한다. |
| 알림 실패 | Telegram 안내는 한 번 시도하며 전달 성공까지 보장하지 않는다. |
| 별도 사진 작업 | 새 사진 update나 7일 만료 후 재전송은 새 작업으로 취급한다. |
| 모델 비용과 품질 | 기존 입력 토큰 강제 제한과 실제 OCR 품질은 이번 범위에서 검증하지 않았다. |
