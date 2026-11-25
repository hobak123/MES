# TASKS — Food ERP·MES 개발 체크리스트

표기: `[x]` 구현 + 자동화 테스트 통과 · `[~]` 구현됨, 자동 검증 범위 제한(사유 기재) · `[ ]` 미착수/미완료 · 🔑 외부 API 키 필요 · 🖨 실제 장비 필요

## Phase 0 — 분석 및 설계 문서
- [x] PRD 전체 분석
- [x] docs/architecture.md
- [x] docs/database-erd.md (모델에서 자동 생성, 78 테이블)
- [x] docs/api-spec.md
- [x] docs/business-rules.md
- [x] TASKS.md

## Phase 1 — 기반
- [x] 프로젝트 구조 (apps/web, apps/mobile, backend, packages, infrastructure, docs, tests/e2e)
- [x] 개발 DB: PostgreSQL 16 (pgserver 바이너리, `infrastructure/scripts/dev-db.py`)
- [x] FastAPI 앱, 공통 응답/오류 포맷, request_id, 보안 헤더
- [x] SQLAlchemy 모델 + Alembic 마이그레이션 (불변 원장 트리거 포함)
- [x] 인증: 로그인/로그아웃/refresh 회전·재사용 탐지/me, bcrypt, 로그인 5회 실패 잠금
- [x] RBAC (역할 8종, 권한 52종, DB 기반) + 공장 범위 검사(X-Facility-Id)
- [x] 감사로그(비밀번호/토큰 제외, UPDATE/DELETE 트리거 차단)
- [x] 원자적 번호 발급 (동시성 테스트)
- [x] Idempotency-Key 처리 모듈
- [x] 시드 데이터 (조직/공장 2/역할/권한/단위/분류/창고 8종/공정 13종/검수양식/사용자 9명/데모 마스터)
- [ ] docker-compose.yml (postgres, redis, minio, backend, worker, web, nginx)
- [ ] Web 기본 구조 (Next.js, 로그인, 레이아웃, 좌측 메뉴)
- [ ] Mobile 기본 구조 (Expo, 로그인, 하단 탭)
- [x] Phase 1 테스트: `backend/tests/test_phase1_auth.py` (10건)

## Phase 2 — 기준정보 · 입고 · OCR · 검수 · LOT/QR · 재고
- [ ] 원료/분류/공급업체/거래처/창고/위치/설비/제품 API
- [ ] 입고 CRUD + 상태 전이
- [ ] OCR Provider Adapter (Google 🔑 / CLOVA 🔑 / Paddle(설치 필요) / PDF 텍스트 / Mock-개발전용)
- [ ] OCR 파서/정규화/신뢰도/중복 감지/다품목 분리/문서 간 불일치
- [ ] 입고 검수(템플릿, 온도 범위, COA/현장 분리) 및 품질 승인
- [ ] 입고 확정: LOT/포장/QR/재고/수불/추적 단일 트랜잭션
- [ ] 라벨 PDF(50×30~100×70, 사용자 지정), ZPL/TSPL, 네트워크 프린터 🖨, 재출력 로그
- [ ] 재고 조회(품목/LOT/포장/원장/기한/안전재고), 이동, 조정 승인, 실사, 예약, 반납, 폐기
- [ ] 필수 테스트 001, 002, 009, 010, 011, 013, 019

## Phase 3 — 제품 · BOM · 제조지시
- [ ] BOM 버전/승인/복사/합계 검증/계산(RATIO/FIXED/PER_UNIT)
- [ ] 공정 마스터/라우팅
- [ ] 제조지시 생성(스냅샷)/필요량/가용 비교/FEFO 추천/예약/배치/상태 전이
- [ ] 필수 테스트 015

## Phase 4 — 계량 · WB · 공정 · 포장재
- [ ] QR 계량 검증/차감/잔량/분할 계량/허용오차/WB 생성
- [ ] 계량 정정(승인 역거래)/반납
- [ ] 공정 실행(시작/투입/조건/일시정지/완료/보류/승인), WIP LOT/QR, 분할/병합/재작업
- [ ] 포장재 소모
- [ ] 필수 테스트 003, 004, 005, 006, 014, 017

## Phase 5 — 완제품 · 출고 · 추적 · 회수
- [ ] FG LOT/검사/승인/보류/하위 포장 QR/수율
- [ ] 출고/확정/정정/반품/출고명세서
- [ ] 정·역방향 추적, 그래프, 검색
- [ ] 회수(영향 범위, 보류, 회수량, 진행률)
- [ ] 필수 테스트 007, 008, 016, 018

## Phase 6 — 보고서 · 대시보드 · 운영
- [ ] 보고서 XLSX/PDF(한글, 헤더 반복, 합계, 수식 주입 방지), 제조기록 일괄
- [ ] 대시보드 KPI/차트/알림
- [ ] 알림 스캔(기한/안전재고/검수/장기 미사용)
- [ ] 승인 요청/감사로그 조회 UI
- [ ] 백업/복구 스크립트, 배포 문서, Nginx HTTPS
- [ ] Android/iOS 빌드 설정 (EAS 🔑 계정 필요)
- [ ] 필수 테스트 012, 020
- [ ] 통합 E2E 시나리오 (PRD §31)

## 결정 기록
| # | 결정 | 사유 |
|---|---|---|
| D1 | 개발 DB는 pgserver 동봉 PostgreSQL 16 | 개발 PC에 Docker/PostgreSQL 미설치. 운영은 docker-compose postgres:16 |
| D2 | 잔량 행 = (LOT 노드, 포장 노드, 창고, 위치) | 포장 단위 잔량과 LOT 합계를 동시에 정확히 조회 |
| D3 | 품질상태·기한을 잔량 행에 비정규화 | 가용재고 조회 단순화. 원천은 LOT 엔티티, 동일 트랜잭션 동기화 |
| D4 | 수량 JSON 응답은 문자열 | Decimal 정밀도 보존 |
| D5 | Celery 비활성 시 BackgroundTasks 로 동일 함수 실행 | 개발 PC에 Redis 미설치 |
| D6 | 소비기한 = 제조일 + shelf_life_days − 1 | 제조일 포함 관행. 제품 설정 `shelf_life_basis` 로 변경 가능 |
