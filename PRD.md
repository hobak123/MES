# 식품 제조 ERP·MES 통합 시스템 PRD

- 문서 버전: v1.0
- 프로젝트명: Food Manufacturing ERP & MES
- 서비스 환경: PC Web / Android / iOS
- 기본 언어: 한국어
- 시간대: Asia/Seoul

## 1. 프로젝트 개요

원료 입고부터 검수, 재고, 계량, 반제품, 제조공정, 완제품, 출고까지 통합 관리한다. 모든 원료 및 생산 LOT를 QR로 식별하고 정방향·역방향 추적을 지원한다. 종이 기록을 최소화하고 COA 및 매입 영수증 OCR, 실시간 수불, 단계별 Excel/PDF 출력을 구현한다.

ERP 범위: 구매·공급업체·입고·재고·생산계획·완제품·출고·보고서. MES 범위: 제조지시·계량·반제품·공정·품질·수율·추적. 회계, 세무, 급여, 인사는 초기 범위에서 제외하고 추후 연동 가능하게 한다.

## 2. 기술 스택 및 아키텍처

- Web: Next.js, React, TypeScript, Tailwind CSS, shadcn/ui
- Mobile: React Native, Expo, TypeScript (Android/iOS)
- Backend: Python, FastAPI, SQLAlchemy, Alembic, Pydantic
- DB: PostgreSQL; 비동기 작업: Redis, Celery
- Storage: S3 호환 저장소, 개발용 MinIO
- OCR: Google Cloud Vision, CLOVA OCR, PaddleOCR 등의 Provider Adapter
- QR: 서버 생성, 모바일 카메라 및 USB/Bluetooth 스캐너
- Excel: openpyxl; PDF: ReportLab 또는 HTML 기반 생성, 한글 폰트 지원
- 인증: JWT Access/Refresh Token, RBAC
- 배포: Docker Compose, Nginx, HTTPS, 환경변수 설정

검증된 호환 버전을 고정한다. Web/Mobile → 동일 HTTPS REST API → PostgreSQL/Redis/Storage/OCR/QR/보고서 서비스 구조로 설계한다. 클라이언트별 독립 재고를 금지한다.

## 3. 사용자 권한

| 역할 | 주요 권한 |
|---|---|
| SUPER_ADMIN | 조직·공장·사용자·환경 설정 |
| ADMIN | 기준정보, 생산계획, 재고조정 승인, 보고서 |
| QUALITY_MANAGER | 입고 및 공정 검사, COA, 부적합, 완제품 출고 승인, 회수 |
| WAREHOUSE_MANAGER | 입고, QR, 재고, 이동, 반납 |
| PRODUCTION_MANAGER | 배합비, 제조지시, 생산계획, 공정, 실적 |
| WORKER | QR 스캔, 계량, 공정 기록, 반제품 생성 |
| SHIPPING_MANAGER | 출고, 거래처, 출고 조회 |
| VIEWER | 허용된 조회 및 출력 |

복수 역할, 공장별 접근 제한, 화면/API 권한 검사, 작업자 및 시간 기록을 적용한다. 승인 기록 변경은 사유 및 관리자 승인이 필요하다.

## 4. 기준정보 관리

### 4.1 원료 마스터

신규 원료 등록 버튼 또는 팝업에서 다음 3개 대분류를 선택한다.

- MAIN_MATERIAL (주원료): 분말, 액상, 농산물, 수산물, 축산물, 기타 식품 배합 원료.
- SUB_MATERIAL (부원료): 포장재, 용기, 필름, 뚜껑, 라벨, 박스.
- OTHER_MATERIAL (기타): 청소도구, 필터, 유지보수 부품, 위생용품, 공장 소모품.

위 분류는 내부 관리 분류이며 식품공전상 식품유형, 식품첨가물 여부, 배합 투입 여부는 별도 필드로 관리한다.

등록 항목: 원료코드(RM/PM/OM-000001), 원료명, 대·중분류, 식품유형, 규격, 기본/구매/재고 단위, 환산 기준, 공급업체, 제조업체, 원산지, 보관조건·위치, 안전/최대재고, 알레르기, COA·검수·소비기한·LOT 관리 여부, 배합 사용 가능 여부, 이미지, 비고, 활성 상태.

마스터 등록은 재고를 증가시키지 않는다. 기타 원료는 기본적으로 배합에 투입하지 않으며 필터 등은 설비 사용 이력에 연결 가능하게 한다.

### 4.2 공급업체 및 거래처

코드, 명칭, 사업자등록번호, 담당자, 연락처, 이메일, 주소, 공급 원료, 거래 상태, 첨부, 비고.

### 4.3 창고 및 위치

공장, 창고 코드/명, 보관 구역, 랙, 온도 조건, 활성 상태. 상온·냉장·냉동·부자재·소모품·반제품·완제품·격리 창고 지원.

## 5. 원료 입고관리

### 5.1 업무 흐름

1. 원료 선택 또는 신규 등록 및 분류 확인.
2. COA 또는 영수증 촬영/업로드.
3. OCR 실행 및 입고 필드 자동 입력.
4. 작업자 확인·수정 및 현장 검수.
5. 품질 승인 및 입고 확정.
6. 내부 LOT/포장 ID 및 QR 자동 생성.
7. 라벨 출력 및 부착.
8. 재고 및 수불부 자동 반영.

### 5.2 입력 항목

입고번호 자동 생성, 입고일시 서버 자동 연동, 원료코드/명/분류, 공급업체, 제조업체, 공급업체 LOT, 내부 LOT, 입고수량/단위, 포장 수량/중량, 원산지, 제조일자, 소비기한 또는 유통기한 및 기한 유형, 보관조건, 창고, 입고자, 검수자, 검수결과.

추가: 구매단가/금액, 발주번호, 영수증/거래명세서/COA 번호, 첨부, 비고. 필수 여부는 원료 분류 및 마스터 설정에 따라 적용(청소도구 등 소비기한 해당 없음 허용).

상태: DRAFT, OCR_PROCESSING, OCR_REVIEW, INSPECTION_PENDING, HOLD, ACCEPTED, REJECTED, POSTED, CANCELLED.

입고 확정 시 재고 생성. 보류 및 검수대기 실물은 격리재고로 관리 가능하며 생산 투입 금지. 부적합은 정상 가용재고로 전환 금지.

## 6. OCR 자동 추출

모바일 카메라, 갤러리, 연속 촬영, 회전/자르기/재촬영 지원. PC는 JPG/JPEG/PNG/PDF 다중 업로드 지원.

문서 유형: COA, 영수증, 거래명세서, 납품서, 원료 라벨, 기타 증빙.

추출 필드: 공급업체, 제조업체, 원료명, 공급업체 LOT, 제조일자, 소비/유통기한, 수량, 중량, 단위, 원산지, COA 번호, 문서 발행일, 시험 항목/기준/결과/판정.

처리: 원본 저장 → 전처리 → OCR → 구조화 → 필드 매핑 → 마스터 매칭 → 날짜/단위 정규화 → 신뢰도 평가 → 검토 → 저장.

```json
{
  "document_type": "COA",
  "supplier_name": "ABC 원료",
  "manufacturer_name": "ABC 제조",
  "material_name": "혼합분말",
  "supplier_lot": "ABC261125",
  "country_of_origin": "대한민국",
  "manufactured_date": "2026-11-20",
  "expiry_date": "2027-11-19",
  "quantity": 100,
  "unit": "kg",
  "confidence": 0.96,
  "review_required": true
}
```

- 항목별 신뢰도 표시, 기본 검토 기준 90% (관리자 변경 가능).
- 낮은 신뢰도 및 누락 필드 강조, 수동 수정, 원본과 결과 동시 표시.
- 없는 정보 임의 생성 금지. 날짜/단위 검증 및 중복 문서 감지.
- 고신뢰 결과도 입고 확정 전 주요 필드 사용자 검토.
- 문서 발행일과 실제 입고일 구분. 외관·실측 온도는 현장 입력 또는 센서 연동.
- 여러 원료가 포함된 영수증은 개별 입고 항목으로 분리. 복수 증빙 연결 및 문서 간 LOT/수량 불일치 표시.
- 실패 시 수동 입력 가능.

## 7. 입고 검수 및 품질

외관, 포장 파손, 오염, 이물, 냄새, 온도, 운송 상태, 소비기한, 표시사항, COA, 원산지, LOT, 수량 등 검수 항목 제공.

결과: PASS, FAIL, HOLD, NOT_APPLICABLE.

원료별 검수 양식·허용 기준·온도 범위 설정, 초과 경고, 부적합 사유, 사진, 검수자 확인 및 승인, PDF 출력, 격리/반품/폐기. COA 시험 결과와 현장검사 분리 저장. 검수 결과는 LOT 연결.

## 8. LOT 및 QR 관리

| 유형 | 표시 번호 예시 |
|---|---|
| 주원료 | RM-YYYYMMDD-000001 |
| 부원료 | PM-YYYYMMDD-000001 |
| 기타 | OM-YYYYMMDD-000001 |
| 포장 | PKG-YYYYMMDD-000001 |
| 계량 반제품 | WB-YYYYMMDD-000001 |
| 공정 반제품 | WIP-YYYYMMDD-000001 |
| 완제품 | FG-YYYYMMDD-000001 |
| 제조지시 | MO-YYYYMMDD-000001 |
| 생산 배치 | BATCH-YYYYMMDD-000001 |

서버 생성, UUID 내부 키, 원자적 번호 발급 및 Unique Constraint 적용.

QR에는 변경되지 않는 식별자만 저장하고 최신 상세정보는 서버 조회한다.

```json
{"version":1,"type":"RAW_MATERIAL_PACKAGE","id":"UUID","lot":"RM-20261125-000001"}
```

유형: RAW_MATERIAL_LOT, RAW_MATERIAL_PACKAGE, WEIGHING_BATCH, PROCESS_WIP, FINISHED_GOODS_LOT, FINISHED_GOODS_PACKAGE, EQUIPMENT, STORAGE_LOCATION. 개인정보·단가·인증정보 포함 금지.

100kg 입고(20kg × 5포) 시 원료 LOT 1개, 서로 다른 포장 ID 및 QR 5개 생성. 개별 포장과 전체 LOT 잔량 모두 조회. 포장에서 7kg 사용 시 해당 포장 13kg, 전체 93kg.

라벨: 회사명, 원료/제품명, 코드, 내부/공급업체 LOT, 포장 ID, 최초 수량, 제조/입고일, 소비기한, 보관조건, 검수상태, QR.

출력: 50×30, 60×40, 80×50, 100×70mm 및 사용자 지정. PDF, 브라우저 인쇄, 네트워크/USB/지원 Bluetooth 프린터, ZPL/TSPL 어댑터. 설정된 지원 환경에서 자동 인쇄. 출력 실패로 입고/LOT 중복 생성 금지. 재출력은 동일 QR 유지 및 사유·횟수·작업자·시간 기록. 실시간 잔량은 스캔으로 조회.

## 9. 재고 및 수불관리

조회 기준: 조직, 공장, 창고, 위치, 품목, 분류, LOT, 포장, 공급업체, 제조일, 소비기한, 품질/재고 상태.

수불 유형: RECEIPT, PRODUCTION_ISSUE, PRODUCTION_RETURN, SUPPLIER_RETURN, DISPOSAL, ADJUSTMENT_IN/OUT, TRANSFER_IN/OUT, SAMPLE_USE, GENERAL_USE, WIP_RECEIPT/ISSUE, FG_RECEIPT, FG_SHIPMENT, REVERSAL.

현재고 = 기초 + 입고 + 반납 + 이동입고 + 증가조정 - 생산투입 - 반품 - 폐기 - 이동출고 - 감소조정 - 기타 사용.

가용재고 = 합격 및 사용 가능한 현재고 - 예약수량.

- 소비기한 관리 품목 FEFO, 기타 FIFO 기본 추천.
- 기한 경과, 부적합, 보류, 재고 부족 원료 투입 금지.
- 음수 재고 금지, 포장/LOT 분할, 잔량, 실사, 승인 조정, 창고 이동.
- 알림: 소비기한 90/60/30/7일 전 및 경과, 안전재고 미달, 검수/COA 미완료, 장기 미사용. 설정 가능.
- 모든 변경은 불변 수불원장 기록. 현재고 직접 수정 금지. 정정은 역거래/조정.
- 원장 기록과 잔량 변경 및 이동 양쪽 처리를 동일 DB 트랜잭션으로 처리.
- 행 잠금 또는 동등한 동시성 제어. Decimal/NUMERIC 사용.
- 동일 차원 단위 환산 지원. 중량↔부피는 검증된 품목별 밀도 필요.

## 10. 제품 마스터 및 배합비

제품 필드: 제품코드/명, 식품유형, 품목제조보고번호, 규격, 내용량, 포장 단위, 박스당 입수, 표준 배치량, 소비기한 기준, 보관조건, 표준 공정/수율, 이미지, 설명, 활성 상태.

BOM 필드: 제품, 버전, 원료코드/명, 배합 비율, 표준 사용량/단위, 허용오차, 투입 순서/공정, 계량 그룹, 필수 및 대체 허용 여부, 적용기간, 승인자/상태.

유형: FORMULA, PACKAGING, PROCESS_AUXILIARY. 포장재는 배합비 100% 산정에서 제외.

예: 기준 100kg에서 A 50%, B 30%, C 15%, D 5%. 200kg 생산 시 각각 100/60/30/10kg. 포장 1kg 기준 계획 200EA, 포장재는 별도 표준 소요량 계산.

비율 및 고정량 기반 BOM, 합계 검증, 오차, 복사, 버전, 승인, 적용일 관리. 승인 전 사용 금지. 제조지시서에 승인 BOM 및 공정 버전 스냅샷 저장, 기존 이력 불변.

## 11. 생산계획 및 제조지시서

필드: 제조지시번호, 생산일, 제품, BOM 버전, 계획 배합량/단위, 목표 완제품량, 배치수/배치량, 라인/작업장, 담당자, 예정시간, 공정, 원료/포장재 필요량, 비고, 첨부, 승인자.

상태: DRAFT, APPROVED, WAITING, WEIGHING, WEIGHING_COMPLETED, PRODUCING, QUALITY_PENDING, COMPLETED, CLOSED, HOLD, CANCELLED.

제품 선택 시 승인 BOM 호출, 필요량 자동 계산, 가용재고 비교, 부족 표시, FEFO/FIFO 추천, 예약, 승인, 복수 배치, 진행률, QR, Excel/PDF 출력.

비율 필요량 = 계획 배합량 × 비율 / 100. 고정량 필요량 = 표준 사용량 × 생산 배율. 완제품 목표량 기준 계산 시 표준 수율을 반영하며 배합량과 완제품량은 별도 관리.

## 12. QR 기반 계량

1. 제조지시 및 배치 선택.
2. 원료 목록 확인 및 포장 QR 스캔.
3. 원료 일치, 품질, 소비기한, 잔량 검증.
4. 목표량·누적 계량량·남은 필요량 확인.
5. 실제 계량량 입력 및 허용오차 확인.
6. 사용 확정, 원료 차감, 포장 잔량 계산, 작업자/시간 기록.
7. 계량 그룹 완료 후 반제품 LOT 및 QR 생성.

화면: 제품/지시/배치, 원료코드/명, LOT/포장, 목표/누적/필요/실제량, 오차, 현재/예상 잔량, 기한, 검수, 작업자/시간/상태.

수동 입력 및 숫자 키패드 필수. USB/Bluetooth/RS232/TCP 전자저울 Adapter 확장, 총중량·용기중량·순중량·안정화 상태·저울 ID 기록 가능.

차단: 지시에 없는 원료, 미승인 대체, 기한 경과, 부적합/보류, 재고 부족, 잘못된 QR, 사용불가 포장, 완료/취소 지시, 오차 초과 미승인, 단위 불일치. 오차는 개별 및 그룹 누적 기준 검증.

20kg 포장에 8kg, 5kg 사용 후 7kg 잔량. QR 유지. 하나의 원료 10kg을 LOT A 6kg, B 4kg으로 분할 계량 가능하며 두 LOT 추적.

미확정 취소 가능. 확정은 승인된 역거래, 사유 및 반납 상태 확인. 혼합/오염 원료 정상 반납 금지. 후속 공정 존재 시 직접 취소 금지.

## 13. 계량 반제품

혼합 전 투입원료 세트를 계량 그룹 단위로 생성. 예: A 10kg, B 5kg, C 3kg, D 2kg → WB-20261125-000001, 총 20kg. 물리적으로 분리된 용기는 상위 QR과 개별 용기 QR 연결.

필드: ID/LOT, 제조지시, 배치, 제품, BOM 버전, 그룹, 사용 원료/LOT/포장/실제량, 총량, 작업자/완료시간, 보관위치/가능시간, 검수, QR, 사용상태.

상태: WEIGHING, READY, HOLD, PARTIALLY_CONSUMED, CONSUMED, DISCARDED, CANCELLED.

스캔 시 제조 대상, 지시, 배치, 원료/LOT/수량, 총량, 담당자, 시간, 다음 공정, 사용 가능 여부 표시. 소진 QR 재투입 금지. 허용된 부분투입은 잔량 기준 관리.

## 14. 제조공정

제품별 공정 설정: 계량, 투입, 혼합, 가열, 살균, 냉각, 여과, 숙성, 건조, 충진, 금속검출, 포장, 완제품검사/입고 등의 추가/삭제/순서/조건 설정.

공정 마스터 필드: 코드/명/순서, 작업장/설비/담당자, 표준 시간/온도/압력/속도, 기타 조건, 검사/허용 기준, 필수 기록, 승인 여부, QR 여부, 다음 공정, CCP 여부.

작업: 지시 확인 → 이전 반제품 QR → 배치/상태 검증 → 시작 → 투입량/조건/검사 → 종료 → 산출량/손실/폐기 → 수율 → 필요 승인 → 신규 WIP LOT/QR → 다음 공정.

기록: 지시/배치/실행 ID/공정, 입력 LOT·수량, 출력 LOT·수량, 정상/불량/폐기/손실, 수율, 시작/종료/작업시간, 설비/작업자, 온도/압력/속도, 검사, 비고/사진, 승인자.

상태: WAITING, IN_PROGRESS, PAUSED, COMPLETED, HOLD, FAILED, CANCELLED.

필수 이전 공정 미완료, 다른 배치(승인 병합 제외), 부적합/보류, 소진/초과 사용, 필수조건 누락, 잘못된 시간 차단. 검사 기준 초과 경고 및 보류.

원료 LOT → 포장 ID → 계량 기록 → WB → 혼합 WIP → 후속 WIP → 포장 → FG. 서로 다른 QR을 부모-자식 추적 관계로 연결하며 QR 문자열 자체를 동일하게 하지 않는다.

분할/병합 및 재작업 지원. 입력/출력 LOT, 수량/단위, 작업시간, 작업자, 승인자, 사유 기록. 재작업도 기존 이력 유지.

## 15. 포장재 관리

포장 공정에서 필름, 용기, 뚜껑, 라벨, 박스 QR 스캔. 코드, LOT, 포장 ID, 사용/불량/폐기/반납/실소모량, 완제품 LOT, 작업자/시간 기록. 수불 자동 반영 및 완제품 역추적 연결.

## 16. 완제품 생산관리

최종 공정 완료 → 포장재 확인 → 생산실적/정상/불량/손실 → 소비기한 → FG LOT → 품질검사 → QR/라벨 → 완제품 재고 → 품질 승인 시 가용 전환 → 지시 마감.

필드: FG LOT, 제품, 지시/배치, 생산일/시작/종료, 소비기한, 내용량, 계획/실제/정상/불량/폐기량, 총 투입량, 정상 순중량, 손실, 수율, 원료/포장재 LOT, 공정 이력, 검사, 담당자/승인자, 창고, QR.

소비기한은 승인된 제품별 계산 및 기산일 기준 적용, 품질 승인 시 확인. 원료 기한을 단순 복사하지 않는다.

완제품 LOT QR 및 개별 제품/박스/팔레트 QR 지원. 하위 단위 ID와 수량을 상위 LOT에 연결.

## 17. 생산수율 및 실적

- 생산수율 = 정상 완제품 순중량 / 실제 총 식품원료 투입량 × 100
- 계획 달성률 = 정상 생산수량 / 계획 생산수량 × 100
- 불량률 = 불량수량 / 총 생산수량 × 100
- 공정수율 = 정상 산출량 / 공정 투입량 × 100

분모 0 또는 차원 불일치 시 확인 필요 표시.

물질수지 차이 = 총 투입량 - 정상 산출량 - 불량량 - 폐기량 - 확인 손실량 - 남은 공정재고량. 수분 증발/흡수/추가 물 투입 별도 기록. 항목 중복 집계 금지. 허용 차이 초과 시 마감 제한 및 관리자 확인.

일/주/월/제품/라인/지시별 실적, 작업시간, 계획 대비, 수율, 공정 손실, 표준 대비 실제 원료/포장재 사용량 분석.

## 18. 출고 및 반품

거래처 코드/명, 담당자, 연락처, 배송지, 상태, 비고.

출고번호/일자, 거래처/납품처, 제품, FG LOT/포장 ID, 수량/단위, 창고, 담당자, 배송정보, 승인자.

거래처 선택 → 제품 선택 → QR → 재고 및 품질 확인 → 수량 → 확정 → 차감 및 거래처 연결 → 출고명세서.

미승인/보류/부적합/기한경과 출고 금지. 반품은 원출고/LOT 연결, 수량/사유, 격리 후 품질 승인 재입고 또는 폐기.

## 19. 통합 LOT 추적 및 회수

정방향: 원료 입고 → 포장 → 계량 → WB → WIP → FG → 출고 → 거래처. 입고/잔량/사용/폐기/반품량, 지시/배치, 영향 LOT, FG 생산/재고/출고량 및 거래처/일자 조회.

역방향: FG → 지시/배치/BOM 버전 → 공정/작업자/설비 → WB → 원료/포장/사용량 → 공급업체/제조업체/원산지/날짜 → 검수/COA 원본 및 포장재/완제품 검사.

그래프 노드: 원료 LOT, 포장, 계량 반제품, WIP, FG, 출고, 거래처. 실제 투입/산출 연결, 상세 클릭 및 증빙 조회, 분할/병합 추적. 물리적으로 확정할 수 없는 개별 제품 원료량은 배치 실측량과 비례 추정량을 구분.

회수: 사건 등록, 문제 LOT 선택, 영향 WIP/FG/재고/출고/거래처 검색, 보류, 회수 대상 및 실제 회수량, 진행률, 보고서. 품질관리 권한으로 보류/해제 승인.

## 20. Excel/PDF 및 인쇄

모든 단계별 개별 출력과 제조지시별 전체 생산기록 일괄 출력을 지원한다.

- 입고: 입고대장, 검수일지, COA, 부적합, 반품/폐기.
- 재고: 주/부/기타 수불부, LOT/포장/창고 잔량, 기한 임박, 실사, 조정.
- 생산: BOM, 제조지시, 계량일지, WB 대장, 공정 작업/검사, 설비, CCP, 포장재, 생산실적/수율/원료 사용.
- 완제품: 생산/검사/재고/수불/출고/반품.
- 추적: 정방향/역방향, 통합 생산 이력, 회수 대상/거래처.

공통 검색: 기간, 품목, LOT, 업체, 라인, 작업자, 지시, 창고, 검사상태. 화면 인쇄, Excel/PDF 다운로드, 미리보기. 화면 필터와 출력 일치.

양식: 회사명/로고, 문서명/번호, 조회기간, 출력일시/담당, 페이지, 작성/검토/승인 확인란, 서명/승인 기록, 합계/소계, 비고.

PDF: A4 세로/가로, 한글, 페이지 자동 분할 및 표 헤더 반복, 잘림 방지, QR. XLSX: 날짜/수량 형식, 자동필터, 헤더 고정, 열너비, 합계, 복수 시트, LOT 문자열 보존, 수식 삽입 공격 방지. 출력 시점/필터 로그.

## 21. PC 웹 UI

좌측 메뉴: 대시보드, 기준정보(원료/분류/제품/BOM/업체/창고/설비/공정), 입고(OCR/검수/COA/부적합/QR), 재고(수불/이동/실사/조정/기한), 생산계획(일정/지시/소요량/배치), MES(계량/WB/공정/포장재/실적/수율), 완제품(생산/검사/재고/QR), 출고/반품, 추적/회수, 보고서, 시스템(권한/사업장/알림/프린터/OCR/감사로그).

대시보드 KPI: 당일 입고 건수/단위별 수량, 재고 품목수, 기한 임박, 생산지시/진행/완료, 검사대기, 출고. 차트: 월별 실적, 제품별 생산, 수불 추이, 수율, 공정, 불량/폐기, 창고. 알림: 기한, 부족, 검수, 이상, 지연, 보류, OCR.

산업용 ERP 스타일, 흰색/네이비/블루, 정상 녹색, 경고 주황, 위험 빨강, 진행 파랑. 반응형, 정렬/페이지네이션/필터/상세 팝업/검색저장/로딩/한국어 오류/확인창/공통 출력 버튼.

## 22. 모바일 앱 UI

홈 버튼: 입고, 촬영/OCR, QR, 계량, 공정, 완제품, 재고, 출고. 하단 탭: 홈, 작업목록, QR, 재고, 더보기.

입고: 신규/분류/촬영/분석/수정/검수/확정/QR/출력. 계량: 제품/지시/배치/진행률, 목표/현재/남은 수량, 스캔/입력/확정/WB 생성. 공정: 현재/다음, QR, 투입량, 시작/조건/검사/종료/산출/QR.

현장 UX: 큰 버튼, 한 손 조작, 숫자 키패드, 스캔 소리/진동, 오류 경고, 플래시, 완료 구분, 화면 잠금 방지 옵션.

오프라인: 연결상태, 촬영/입력 임시저장, 재연결 전송, 동기화 표시, 중복 방지, 충돌 재확인. 서버 검증 전 재고 차감·투입·생산완료 최종 확정 금지.

## 23. 데이터베이스

공통: UUID PK, organization_id, facility_id(해당 시), created_at/by, updated_at/by, status, version(필요 시). 거래 물리삭제 금지, 마스터 비활성화. NUMERIC(20,6), TIMESTAMPTZ(UTC 저장, 서울 표시), DATE(제조/기한).

### 테이블 목록

- 조직/권한: organizations, facilities, users, roles, permissions, user_roles, role_permissions, user_facilities
- 기준: materials, material_categories, suppliers, customers, products, units, unit_conversions, warehouses, storage_locations, equipments
- 입고: material_receipts, material_receipt_items, material_lots, material_packages, receipt_documents, document_links, ocr_jobs, ocr_extracted_fields, inspection_templates, inspection_template_items, quality_inspections, quality_inspection_results
- 재고: inventory_balances, inventory_transactions, inventory_reservations, stock_transfers, stock_counts, stock_adjustments
- BOM: bom_headers, bom_versions, bom_items
- 생산: production_orders, production_order_items, production_batches, production_routings, routing_versions, routing_steps, weighing_batches, weighing_records, process_definitions, process_executions, process_inputs, process_outputs, process_parameter_records, packaging_consumptions
- 완제품: finished_goods_lots, finished_goods_packages, shipment_headers, shipment_items, shipment_returns
- 추적: trace_nodes, trace_events, trace_event_inputs, trace_event_outputs, recall_cases, recall_case_lots, recall_case_shipments
- 공통: qr_codes, qr_print_logs, printer_settings, file_attachments, approval_requests, notifications, report_export_logs, audit_logs, idempotency_keys

### 주요 필드

- materials: material_code, material_name, category_id, food_type, base_unit_id, purchase_unit_id, storage_condition, safety_stock, default_supplier_id, default_country_of_origin, expiry_tracking_required, coa_required, inspection_required, production_usable, allergen_information, is_active.
- material_lots: material_id, receipt_item_id, lot_number, supplier_lot_number, supplier_id, manufacturer_name, country_of_origin, received_date, manufactured_date, expiry_date, expiry_type, original_quantity, base_unit_id, quality_status.
- material_packages: material_lot_id, package_number, original_quantity, base_unit_id, package_status, qr_code_id. 위치/잔량은 inventory_balances 기준.
- inventory_transactions: transaction_number, transaction_type, material_id/product_id, trace_node_id, material_lot_id, package_id, warehouse_id, storage_location_id, quality_status, quantity, base_unit_id, direction, balance_before/after, reference_type/id, transaction_group_id, reversal_of_id, idempotency_key, performed_by/at, reason. 불변 원장.
- weighing_records: weighing_batch_id, production_order_id, production_batch_id, material_id, material_lot_id, material_package_id, target_quantity, actual_quantity, unit_id, tolerance_min/max, scale_equipment_id, inventory_transaction_id, weighed_by/at, approval_status, status.
- process_executions: production_order_id, production_batch_id, routing_step_id, process_definition_id, equipment_id, started_at, completed_at, total_input_quantity, good_output_quantity, defective_quantity, waste_quantity, process_loss_quantity, unit_id, process_yield, quality_status, operated_by, approved_by, status. 복수 입력/출력 별도 테이블.
- finished_goods_lots: product_id, production_order_id, production_batch_id, lot_number, manufactured_date, expiry_date, planned_quantity, total_produced_quantity, good_quantity, defective_quantity, waste_quantity, net_weight, base_unit_id, production_yield, quality_status, qr_code_id.
- trace_nodes: node_type, entity_id, lot_number, organization_id, facility_id.
- trace_events: event_type, production_order_id, production_batch_id, reference_type/id, performed_at/by.
- trace_event_inputs / trace_event_outputs: trace_event_id, trace_node_id, quantity, unit_id.

추적 이벤트 유형: RECEIPT, SPLIT, WEIGHING, MIXING, PROCESSING, MERGE, PACKAGING, FINISHING, REWORK, SHIPMENT. 복수 입력/출력 지원. Recursive CTE 등으로 양방향 탐색. 취소/재작업 이벤트 추가, 과거 이력 덮어쓰기 금지, 순환/잘못된 연결 검증.

인덱스: organization_id, facility_id, material_code, product_code, lot_number, package_number, expiry_date, production_order_id, production_batch_id, trace_node_id, transaction_type, created_at. 조직별 품목코드, 내부 LOT, QR, 거래 idempotency 유일성, FK, 음수 잔량 금지.

## 24. API

기본 경로 `/api/v1`. 아래 표의 각 항목은 해당 HTTP 메서드와 경로를 구현한다.

| 모듈 | API |
|---|---|
| 인증 | POST /auth/login, /auth/logout, /auth/refresh; GET /auth/me |
| 기준 | GET/POST /materials, /suppliers, /products, /warehouses; GET/PATCH /materials/{id}, /products/{id} |
| 입고 | GET/POST /receipts; GET/PATCH /receipts/{id}; POST /receipts/{id}/documents, /inspection, /approve, /post, /reverse, /cancel |
| OCR | POST /ocr/upload, /ocr/jobs; GET /ocr/jobs/{id}, /ocr/jobs/{id}/result; POST /ocr/jobs/{id}/confirm, /retry |
| QR | POST /qr/generate, /qr/resolve; GET /qr/{id}, /qr/{id}/label; POST /qr/{id}/print, /reprint |
| 재고 | GET /inventory, /inventory/lots, /packages, /transactions, /expiring, /low-stock (모두 /inventory 하위); POST /inventory/transfer, /adjustment, /stock-count, /reservation, /return, /disposal |
| BOM | GET/POST /boms; GET /boms/{id}; POST /boms/{id}/versions, /approve, /calculate |
| 제조지시 | GET/POST /production-orders; GET /production-orders/{id}; POST /production-orders/{id}/approve, /start, /hold, /complete, /close, /cancel |
| 계량 | POST /weighing/validate-qr, /weighing/records; GET /weighing/batches/{id}; POST /weighing/batches/{id}/complete, /weighing/records/{id}/reverse, /return |
| 공정 | GET/POST /processes; GET /process-executions/{id}; POST /process-executions, /process-executions/{id}/start, /inputs, /parameters, /pause, /complete, /hold |
| 완제품 | GET/POST /finished-goods; GET /finished-goods/{id}; POST /finished-goods/{id}/inspection, /release, /hold, /labels |
| 출고 | GET/POST /shipments; GET /shipments/{id}; POST /shipments/{id}/confirm, /reverse, /return |
| 추적/회수 | GET /trace/forward/{lot_id}, /trace/backward/{lot_id}, /trace/graph/{lot_id}; POST /trace/search; GET/POST /recalls; GET /recalls/{id}; POST /recalls/{id}/hold-stock |
| 보고서 | GET /reports/{report_type}; POST /reports/{report_type}/excel, /pdf; GET /report-exports/{id} |

축약된 하위 경로는 앞의 리소스 및 ID 접두사를 적용한다. 인증/권한/조직·공장 범위, 입력 검증, 페이지네이션, 검색/정렬, 일관된 오류, 요청 ID, 감사, 상태 전이 검사. 변경 API Idempotency-Key 지원. 동일 키에 다른 본문이면 충돌 오류 처리.

```json
{"success":true,"data":{},"message":"정상 처리되었습니다.","request_id":"UUID"}
```

```json
{"success":false,"error_code":"INSUFFICIENT_STOCK","message":"사용 가능한 원료 재고가 부족합니다.","details":{},"request_id":"UUID"}
```

## 25. 보안 및 감사

HTTPS, 안전한 비밀번호 해시, JWT/Refresh 갱신·폐기, 로그인 제한, RBAC, 조직/공장 격리, SQL injection/XSS/CSRF 대응, 파일 실제 형식/크기/악성 검사, 비인가 다운로드 차단, 비밀 환경변수. 모바일 보안 저장소. OCR 키 프론트 노출 금지, COA/영수증 비공개, 외부 OCR 문서 전송 정책.

감사 대상: 로그인/로그아웃, 마스터 변경, 입고/정정, 품질, BOM/지시, 재고/계량, 생산완료, QR 재출력, 출고, 회수, 보고서, 권한. 작업자, 유형, 대상, 전후 값, 사유, 시각, 요청 ID, IP/장치. 비밀번호/토큰 제외. 일반 관리자 수정/삭제 금지.

## 26. 비기능 요구사항

초기 목표(부하 테스트 필요): 동시 50명, 연간 수불 100만 건, 목록/QR p95 2초 이내, 일반 저장 p95 3초 이내. 대량 보고서 비동기, OCR 진행 상태 표시.

트랜잭션/동시성/중복 방지, 오류 로그/모니터링, 작업 큐 재시도, 실패 재처리, 원장 정합성 검증. DB/첨부 암호화 백업, 별도 위치, 보관 주기, 복구 테스트. 초기 RPO 24시간, RTO 4시간(운영 규모에 따라 재조정). 다중 공장/창고/라인/사용자, 외부 ERP, 저울/센서/PLC 확장.

## 27. 예외 처리

- OCR 실패: 재시도, 원본, 수동 입력, 실패 사유.
- QR 오류: 재스캔, LOT 직접검색, 재출력/훼손 사유.
- 재고 부족: 부족 표시, 다른 적합 LOT 추천, 확정 차단, 알림.
- 오투입: 원료/지시/배치/품질/기한 검증 및 차단.
- 중복 스캔: 조회 허용, 중복 확정 차단, 별도 정상 계량 허용.
- 프린터 실패: 경고, PDF 대체, 동일 QR 재출력.
- 서버 장애: 임시저장, 연결 표시, 재전송, 서버 승인 전 확정 금지.
- 손실/불량: 수량/원인/사진/승인, 수불 반영 및 추적 유지.

## 28. 프로젝트 구조

```text
food-erp-mes/
├── PRD.md
├── README.md
├── TASKS.md
├── .env.example
├── .gitignore
├── docker-compose.yml
├── apps/
│   ├── web/src/{app,components,features,hooks,lib,types}/
│   └── mobile/{app,components,features,services,hooks}/
├── backend/
│   ├── app/{api,core,models,schemas,repositories,services,workers}/
│   ├── alembic/
│   ├── tests/
│   ├── requirements.txt
│   └── Dockerfile
├── packages/{api-client,shared-types,shared-utils}/
├── infrastructure/{nginx,scripts,backup}/
├── docs/{architecture.md,database-erd.md,api-spec.md,business-rules.md,user-manual.md,deployment.md}
└── tests/e2e/
```

services 하위: ocr, inventory, production, weighing, traceability, qr, printing, reports. 각 앱 package.json 및 backend/app/main.py 생성.

## 29. 개발 단계

- Phase 1: 프로젝트, Docker, PostgreSQL, Redis, FastAPI, Web/Mobile 기본구조, 인증/권한, UI, migration, 테스트.
- Phase 2: 원료/공급업체/창고, 입고, OCR, 검수, LOT/QR, 포장, 재고/수불.
- Phase 3: 제품/BOM/버전, 지시, 배치, 소요량, 예약, 계획.
- Phase 4: 모바일 스캔, 계량/차감/잔량, WB, 공정/QR, 분할/병합, 포장재.
- Phase 5: FG/품질/재고/수율, 출고, 양방향 추적/그래프, 회수.
- Phase 6: Excel/PDF/인쇄, 대시보드/알림/감사, 보안/백업/성능, 배포 및 Android/iOS 빌드.

## 30. 필수 테스트

| ID | 조건 | 기대 결과 |
|---|---|---|
| 001 | COA/영수증 촬영 | 추출/자동 입력/누락 표시/수정/원본 저장 |
| 002 | 100kg, 20kg × 5포 입고 | LOT 1, 포장 QR 5, 총 100kg, 개별 20kg |
| 003 | 20kg에서 7kg 계량 | 13kg, 수불/지시/LOT/작업자 연결 |
| 004 | 10kg 목표, A 6+B 4kg | 두 LOT 차감 및 WB 연결 |
| 005 | 잘못된 원료 스캔 | 경고/차단, 차감 없음 |
| 006 | 기한 경과 원료 | 차단/차감 없음 |
| 007 | WB→WIP→FG | FG에서 모든 원료 역추적 |
| 008 | 원료가 FG 3개에 사용 | 3개 및 재고/출고/거래처 조회 |
| 009 | 5kg 재고에 7kg 요청 | 거부, 5kg 유지 |
| 010 | 동일 키 요청 2회 | 1회 기록/차감 |
| 011 | 10kg 재고에 동시 7kg 요청 2건 | 한 건 성공, 3kg 잔량 |
| 012 | 지시/기간 보고서 | 화면/XLSX/PDF 일치, 한글/합계/개별 출력 |
| 013 | 모바일 입고 후 PC 조회 | 동일 LOT/잔량, 중복 없음 |
| 014 | A+B→C→D/E | 분할/병합 양방향 연결 유지 |
| 015 | BOM v1 지시 후 v2 개정 | 기존 v1, 신규 승인 v2 |
| 016 | 품질 미승인 FG | 실물 재고 조회, 출고 차단 |
| 017 | 확정 계량 정정 | 원본 유지, 승인 역거래, 후속 LOT 제한 |
| 018 | 작업자 승인 및 타 공장 접근 | 거부, 데이터 변경 없음 |
| 019 | 입고 성공 후 인쇄 실패 | 입고 유지, 동일 QR 재출력 |
| 020 | 오프라인 저장 후 재전송 | 서버 재검증, 중복 차감 없음 |

## 31. 통합 E2E 시나리오

1. 원료 A 100kg(20kg×5), B 50kg, C 30kg 입고 및 검수/QR. 포장재 98EA 이상 별도 입고.
2. 제품 및 100kg 기준 BOM A 50kg/B 30kg/C 20kg 승인.
3. MO-20261125-000001 생성, 계량 및 잔량 A 50/B 20/C 10kg 검증.
4. WB-20261125-000001 총 100kg 생성.
5. 혼합: 100kg 투입, 99kg 산출, 1kg 손실, WIP-20261125-000001.
6. 충진/포장: 99kg 투입, 98kg 정상 순중량, 1kg 손실, 1kg 규격 98EA, 포장재 98EA 소모.
7. FG-20261125-000001, 수율 98%, 품질 승인.
8. 60EA 출고 후 38EA 잔량.
9. FG 역추적 시 A/B/C, 실사용량, 입고, COA, 계량, 공정, 포장재, 품질, 거래처 확인.
10. A 정방향 추적 시 사용 50kg, 잔량 50kg, WB/WIP/FG, 생산 98EA, 출고 60EA, 잔량 38EA 확인.

## 32. 개발 완료 기준

- [ ] PC 웹 및 Android/iOS 정상 실행, 동일 API/DB 연동
- [ ] 사용자 역할 및 공장 권한
- [ ] 실제 OCR 및 검토/수정, 원료 3개 분류, 입고/검수
- [ ] LOT/포장 QR 생성·인쇄·스캔, 재고 및 수불 자동 반영
- [ ] BOM 버전, 제조지시, QR 계량, 사용/잔량, WB QR
- [ ] 공정별 QR 및 작업 이력, 분할/병합, 포장재 연결
- [ ] FG QR, 수율, 품질, 출고, 양방향 추적 및 COA 조회
- [ ] 각 단계 Excel/PDF/인쇄, 전체 제조기록 출력
- [ ] 중복 차감 및 음수 재고 방지, 감사, 백업/복구
- [ ] 필수 테스트 통과, 설치/운영 매뉴얼

## 33. AI 개발 에이전트 실행 지침

이 PRD를 요구사항 기준으로 사용한다. 목업이 아닌 실제 기능 및 API/DB 연동 구현. 하드코딩 가상 재고로 완료 처리 금지. 재고 트랜잭션 및 LOT 무결성 보존. OCR 실제 Provider 및 개발용 Mock/수동 입력 지원, 운영 Mock 금지. QR 생성/스캔, 실제 XLSX/PDF 생성. 자동화 테스트, migration, .env.example, README, 시드 데이터. 비밀 하드코딩 금지. 핵심 기능 생략 금지. 장비 호환은 검증 후 확정. 불확실 사항 설정화 또는 결정 기록. 기존 정상 기능 임의 삭제 금지.

실행 순서:
1. PRD 전체 분석.
2. docs/architecture.md 작성.
3. docs/database-erd.md에 Mermaid ERD 작성.
4. docs/api-spec.md 상세 명세.
5. docs/business-rules.md 상태 전이, 승인, 재고, 품질, 추적 규칙 작성.
6. TASKS.md Phase별 체크리스트 작성.
7. 프로젝트 및 Docker 개발환경 구축.
8. DB/Backend/API/인증 구축.
9. Web/Mobile 구현 및 API 연결.
10. 각 Phase 구현 및 테스트.
11. 입고~출고 통합 테스트.
12. 오류 수정 및 운영 배포 문서.

각 단계 완료 여부를 TASKS.md에 기록한다. 테스트 실패 또는 미검증 기능은 완료 처리하지 않는다. 외부 API 키 및 장비가 필요한 기능은 별도 표시한다.

## 34. VS Code 개발 시작 프롬프트

```text
현재 프로젝트의 PRD.md를 전체 분석하고 실제 운영 가능한 식품 제조 ERP·MES를 개발해 줘. 단순 UI 목업이 아니라 PostgreSQL, Backend API, PC 웹, Android/iOS 앱이 연동되어야 해. 아키텍처, ERD, API 명세, 업무 규칙, TASKS.md를 먼저 작성하고 Phase 1부터 순차 구현해 줘. 재고 중복 차감, 음수 재고, LOT 추적 누락을 방지하고 각 단계마다 테스트를 실행해 줘. 테스트하지 않은 기능을 완료 처리하지 말고 외부 API 키나 장비가 필요한 기능은 별도로 표시해 줘.
```

## 35. 운영 전 확인 사항

OCR 정확도 및 비용, 실제 COA/영수증 양식, 라벨 프린터 모델 및 통신, 저울/센서 연동, 클라우드 또는 사내 서버, 앱 배포, 사업장별 검수 및 HACCP 기준, 사내 보고서 양식을 확인한다. 본 PRD의 기술 스택·성능 목표는 제안값이며 장비 호환과 법적 요구사항 충족은 실제 환경에서 별도 검증한다. 대시보드보다 수불 정확성 및 LOT 추적 무결성을 우선한다.
