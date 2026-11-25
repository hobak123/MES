# 작업 인수인계 (2026-11-25 일시 정지 시점)

## 상태 요약
| 영역 | 상태 |
|---|---|
| 설계 문서 (architecture / ERD / api-spec / business-rules / TASKS) | 완료 |
| 백엔드 Phase 1 기반 (인증·RBAC·공장격리·감사·번호·Idempotency) | 완료 · 테스트 10건 통과 |
| 백엔드 Phase 2 입고·OCR·검수·LOT/QR·라벨·재고 | 완료 · 테스트 15건 통과 (필수 001·002·009·010·011·013·019) |
| 백엔드 Phase 3 BOM·라우팅·제조지시·예약 | 완료 · 테스트 3건 통과 (필수 015) |
| 백엔드 Phase 4 계량·WB·공정·포장재·분할/병합·정정 | 완료 · 테스트 8건 통과 (필수 003·004·005·006·014·017) |
| 추적 API(forward/backward/graph/search), 승인/사용자/설정/프린터/알림/감사로그 API | 구현됨 (Phase 4 테스트에서 추적·승인 경로 검증) |
| **Phase 5 완제품·출고·반품·회수** | **진행 중** — 서비스 `backend/app/services/fg_shipping.py` 작성 완료. **API 라우터(`api/v1/fg.py`)와 테스트(007·008·016·018) 아직 작성하지 않음** |
| Phase 6 보고서 XLSX/PDF·대시보드·Celery 워커·docker-compose·nginx·백업·README | 미착수 |
| PC 웹 (apps/web) | 에이전트가 시작 직후 일시 정지 — `packages/shared-types`, `packages/shared-utils` 일부만 생성됨, `apps/web` 비어 있음 |
| 모바일 (apps/mobile) | 에이전트가 Expo 기본 템플릿(SDK 58)만 생성 후 일시 정지 |

## 재개 순서 (다음 세션)
1. 개발 DB 기동: `backend/.venv/Scripts/python.exe infrastructure/scripts/dev-db.py start`
2. 백엔드 테스트로 상태 확인: `cd backend && .venv/Scripts/python.exe -m pytest` (현재 36건 통과 예상)
3. Phase 5: `api/v1/fg.py` 작성(완제품/출고/반품/회수 엔드포인트, docs/api-spec.md §12~14) → router 등록 → `tests/test_phase5_fg.py`(필수 007·008·016·018)
4. Phase 6: 보고서(services/reports), 대시보드, workers/tasks.py(Celery), docker-compose/nginx/backup, README/deployment/user-manual
5. 웹·모바일: 병렬 에이전트 재시작 (기존 지시 내용: PRD §21/§22, docs/api-spec.md 기준, 웹은 apps/web+packages, 모바일은 apps/mobile 전담)
6. 통합 E2E (PRD §31), 필수 테스트 012·020, TASKS.md 최종 갱신

## 환경 메모
- Python 3.12 (사용자 설치) — 반드시 `backend/.venv/Scripts/python.exe` 사용 (`python` 은 Windows Store 스텁)
- 개발 DB: pgserver 동봉 PostgreSQL 16, 포트 55432, DB `foodmes`/`foodmes_test`, 데이터 `.devdata/pg` (timezone 데이터 없음 → 접속 옵션 timezone 지정 금지)
- 시드: `cd backend && .venv/Scripts/python.exe -m app.seed --demo` / 개발용 계정 비밀번호 `Passw0rd!` (admin, manager, qm, wh, pm, worker, ship, viewer, worker2)
- API 실행: `cd backend && .venv/Scripts/python.exe -m uvicorn app.main:app --port 8000` → `/api/v1/docs`
- Docker/Redis 미설치: Celery 비활성(인프로세스 BackgroundTasks), 운영 구성은 docker-compose 로 작성 예정
