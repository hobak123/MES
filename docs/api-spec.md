# API 명세 — `/api/v1`

- 실행 중인 서버의 OpenAPI: `GET /api/v1/openapi.json`, Swagger UI: `/api/v1/docs` (요청/응답 스키마의 최종 기준).
- 인증: `Authorization: Bearer <access_token>`. 공장 선택: `X-Facility-Id: <uuid>` (생략 시 사용자 기본 공장).
- 변경 API: `Idempotency-Key: <uuid>` 권장 (재고/계량/출고 등 수량 변경 API는 클라이언트가 항상 전송).
- 요청 추적: `X-Request-ID` (없으면 서버 생성, 응답 헤더/본문에 반환).
- 수량/금액은 JSON **문자열 숫자**로 응답(정밀도 보존). 요청은 숫자 또는 문자열 허용.
- 목록 공통 쿼리: `page`(1~), `size`(1~500, 기본 20), `sort`(필드, `-` 내림차순), `q`(검색어) + 모듈별 필터.

## 공통 응답

```json
{"success":true,"data":{},"message":"정상 처리되었습니다.","request_id":"UUID"}
{"success":true,"data":{"items":[],"total":0,"page":1,"size":20,"pages":0},"message":"…","request_id":"UUID"}
{"success":false,"error_code":"INSUFFICIENT_STOCK","message":"사용 가능한 재고가 부족합니다.","details":{},"request_id":"UUID"}
```

### 주요 오류 코드

| HTTP | error_code | 의미 |
|---|---|---|
| 401 | UNAUTHORIZED / INVALID_TOKEN / TOKEN_EXPIRED / INVALID_CREDENTIALS / ACCOUNT_LOCKED / REFRESH_TOKEN_REUSED | 인증 |
| 403 | FORBIDDEN / FACILITY_FORBIDDEN | 권한 / 공장 범위 |
| 404 | NOT_FOUND | 대상 없음(타 조직 데이터 포함) |
| 409 | INVALID_STATE_TRANSITION / IDEMPOTENCY_CONFLICT / DUPLICATE / DUPLICATE_DOCUMENT / IMMUTABLE_RECORD | 상태·중복 |
| 422 | VALIDATION_ERROR | 입력 형식 |
| 422 | INSUFFICIENT_STOCK / EXPIRED_MATERIAL / QUALITY_NOT_RELEASED / MATERIAL_NOT_IN_ORDER / SUBSTITUTE_NOT_ALLOWED / PACKAGE_NOT_USABLE / UNIT_MISMATCH / TOLERANCE_EXCEEDED / INVALID_QR / ORDER_NOT_WEIGHABLE / DOWNSTREAM_EXISTS / BOM_RATIO_INVALID / BOM_NOT_APPROVED / PREVIOUS_STEP_INCOMPLETE / BATCH_MISMATCH / REQUIRED_PARAMETER_MISSING / MASS_BALANCE_EXCEEDED / COA_REQUIRED / EXPIRY_REQUIRED / UNIT_CONVERSION_REQUIRED | 업무 규칙 |

---

## 1. 인증

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | /auth/login | - | `{organization_code?="DEFAULT", username, password}` → `{access_token, refresh_token, expires_in, user}`. 5회 실패 시 15분 잠금 |
| POST | /auth/refresh | - | `{refresh_token}` → 새 토큰쌍(회전). 재사용 탐지 시 전체 세션 폐기 |
| POST | /auth/logout | 로그인 | `{refresh_token?}` 생략 시 전 세션 폐기 |
| GET | /auth/me | 로그인 | 사용자, 역할, 공장, 권한 목록 |

## 2. 기준정보

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | /materials | master:read | 필터: `major_category, category_id, is_active, production_usable` |
| POST | /materials | master:write | `material_code` 생략 시 RM/PM/OM-000001 자동. 재고 미생성 |
| GET/PATCH | /materials/{id} | read / write | PATCH 전후 값 감사, `is_active=false`로 비활성화 |
| GET/POST | /material-categories | read / write | 대분류별 중분류 |
| GET/POST | /suppliers, GET/PATCH /suppliers/{id} | read / write | |
| GET/POST | /customers, GET/PATCH /customers/{id} | read / customer:write | |
| GET/POST | /products, GET/PATCH /products/{id} | read / write | |
| GET/POST | /warehouses, PATCH /warehouses/{id} | read / write | `warehouse_type`: AMBIENT/REFRIGERATED/FROZEN/PACKAGING/CONSUMABLE/WIP/FINISHED/QUARANTINE |
| GET/POST | /warehouses/{id}/locations | read / write | 보관 위치(구역/랙) |
| GET/POST | /equipments, PATCH /equipments/{id} | read / write | |
| GET | /units, POST /unit-conversions | read / write | |
| GET | /inspection-templates | read | 검수 양식 |

## 3. 입고

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | /receipts | receipt:read | 필터: `status, supplier_id, date_from, date_to, material_id` |
| POST | /receipts | receipt:write | `{supplier_id, purchase_order_number?, receipt_document_number?, coa_number?, note?, items:[{material_id, quantity, unit_id?, package_count, package_quantity?, supplier_lot_number?, manufacturer_name?, country_of_origin?, manufactured_date?, expiry_date?, expiry_type?, warehouse_id?, storage_location_id?, unit_price?, note?}]}` → DRAFT. 입고번호·입고일시 서버 생성 |
| GET | /receipts/{id} | receipt:read | 품목, 문서, OCR, 검수, LOT/포장 포함 |
| PATCH | /receipts/{id} | receipt:write | DRAFT/OCR_REVIEW/INSPECTION_PENDING/HOLD 에서만. items 전체 교체 |
| POST | /receipts/{id}/documents | receipt:write | multipart `files[]`, `document_type`, `run_ocr?=true` → 문서 저장, 중복(해시) 감지, OCR 작업 생성 |
| POST | /receipts/{id}/submit-inspection | receipt:write | DRAFT → INSPECTION_PENDING |
| POST | /receipts/{id}/inspection | receipt:inspect | `{source:"FIELD"|"COA", results:[{item_code, result, measured_value?, text_value?, note?}], overall_result, nonconformance_reason?, disposition?}` → PASS/HOLD/FAIL 반영, 온도 범위 초과 자동 표시 |
| POST | /receipts/{id}/approve | receipt:approve | 품질 승인 → ACCEPTED (POSTED면 LOT PASSED) |
| POST | /receipts/{id}/post | receipt:post | 확정: LOT/포장/QR/재고/수불/추적 생성 (Idempotency-Key) → `{lots:[{lot, packages[]}], labels}` |
| POST | /receipts/{id}/reverse | receipt:reverse | `{reason}` 미사용 LOT 만 REVERSAL |
| POST | /receipts/{id}/cancel | receipt:write | `{reason}` 미확정 입고 취소 |

## 4. OCR

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | /ocr/upload | ocr:use | multipart `file`, `document_type`, `receipt_id?` → 문서 + OCR 작업(QUEUED) |
| POST | /ocr/jobs | ocr:use | `{document_id, receipt_id?, document_type?}` 기존 문서로 작업 생성 |
| GET | /ocr/jobs/{id} | ocr:use | 상태(QUEUED/PROCESSING/REVIEW_REQUIRED/COMPLETED/FAILED/CONFIRMED), 진행 |
| GET | /ocr/jobs/{id}/result | ocr:use | 필드별 `{field_name, raw_value, normalized_value, confidence, is_missing, needs_review, matched_entity_id}`, 품목 분리(item_index), 원본 파일 URL, 문서 간 불일치 |
| POST | /ocr/jobs/{id}/confirm | ocr:use | `{fields:{name: value}, items:[{…}]}` 사용자 검토값 저장 → 입고 품목에 반영 |
| POST | /ocr/jobs/{id}/retry | ocr:use | 실패/재처리 |
| GET | /files/{id} | 해당 모듈 read | 비공개 파일 다운로드(인증·조직 검사) |

## 5. QR / 라벨

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | /qr/generate | qr:print | `{qr_type, entity_id}` 엔티티 QR 보장(멱등) |
| POST | /qr/resolve | qr:read | `{raw}` JSON payload 또는 LOT/포장번호 → `{qr_type, entity, status, quantity, lot_quantity, quality_status, expiry_date, location, usable, warnings[]}` |
| GET | /qr/{id} | qr:read | QR 메타 + payload |
| GET | /qr/{id}/image | qr:read | PNG |
| GET | /qr/{id}/label | qr:read | `?size=80x50&format=pdf|zpl|tspl` 라벨 |
| POST | /qr/{id}/print | qr:print | `{printer_id?, size?, copies?}` 네트워크 프린터 전송 또는 PDF 생성. 실패 시 `{status:"FAILED", fallback_pdf_url}` |
| POST | /qr/{id}/reprint | qr:reprint | `{reason, printer_id?, size?}` 사유 필수, 동일 QR |
| GET | /qr/{id}/print-logs | qr:read | 출력 이력 |
| POST | /qr/labels/batch | qr:print | `{qr_ids[], size}` 다건 라벨 PDF |

## 6. 재고

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | /inventory | inventory:read | 품목별 집계(현재고/예약/가용/격리). 필터: `item_type, major_category, material_id, product_id, warehouse_id, quality_status` |
| GET | /inventory/lots | inventory:read | LOT별 잔량 |
| GET | /inventory/packages | inventory:read | 포장별 잔량(잔량 행) |
| GET | /inventory/transactions | inventory:read | 수불 원장. 필터: `transaction_type, material_id, product_id, lot_number, date_from, date_to, reference_type` |
| GET | /inventory/expiring | inventory:read | `days=90` 기한 임박/경과 |
| GET | /inventory/low-stock | inventory:read | 안전재고 미달 |
| GET | /inventory/recommend | inventory:read | `material_id, quantity` FEFO/FIFO 추천 |
| POST | /inventory/transfer | inventory:move | `{balance_id, quantity, to_warehouse_id, to_storage_location_id?, reason?}` |
| POST | /inventory/adjustment | inventory:adjust_request | `{balance_id, direction, quantity, reason}` → PENDING |
| POST | /inventory/adjustments/{id}/approve | inventory:adjust_approve | 승인 → 거래 반영 / `/reject` |
| GET | /inventory/adjustments | inventory:read | |
| POST | /inventory/stock-count | inventory:count | `{warehouse_id?, lines:[{balance_id, counted_quantity}], note}` → 차이분 조정요청 자동 생성 |
| POST | /inventory/reservation | inventory:reserve | `{balance_id, quantity, production_order_id?, reason}` |
| POST | /inventory/return | inventory:move | 생산 반납 `{balance_id(원 포장), quantity, production_order_id?, reason, contaminated=false}` |
| POST | /inventory/disposal | inventory:dispose | `{balance_id, quantity, kind:"DISPOSAL"|"SUPPLIER_RETURN"|"SAMPLE_USE"|"GENERAL_USE", reason}` → 승인 요청 |
| POST | /inventory/verify-ledger | inventory:adjust_approve | 원장 Σ 와 잔량 일치 검증 |

## 7. BOM

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | /boms | bom:read | `product_id` |
| POST | /boms | bom:write | `{product_id, name, version:{base_quantity, base_unit_id, base_fg_quantity?, effective_from?, items:[{item_type, material_id, quantity_basis, ratio_percent?, standard_quantity?, unit_id, tolerance_pct, sequence, weighing_group?, process_step_code?, is_required, substitute_allowed, substitute_material_ids?}]}}` |
| GET | /boms/{id} | bom:read | 헤더 + 버전 + 품목 |
| POST | /boms/{id}/versions | bom:write | 새 버전(`copy_from_version_id?` 복사) |
| PATCH | /boms/versions/{version_id} | bom:write | DRAFT 만 수정 |
| POST | /boms/{id}/approve | bom:approve | `{version_id}` 합계 검증 후 APPROVED, 이전 APPROVED → OBSOLETE |
| POST | /boms/{id}/calculate | bom:read | `{planned_quantity, target_fg_quantity?, version_id?}` → 품목별 필요량, 가용재고, 부족량, FEFO 추천 |

## 8. 공정 마스터 / 라우팅

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET/POST | /processes | process:read / write | 공정 마스터 |
| PATCH | /processes/{id} | process:write | |
| GET/POST | /routings | process:read / write | `{product_id, name, steps:[{process_definition_id, sequence, step_name, is_required, parameter_specs?, output_type}]}` |
| POST | /routings/{id}/approve | process:write | |

## 9. 제조지시

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | /production-orders | production:read | 필터: `status, product_id, date_from, date_to` |
| POST | /production-orders | production:write | `{production_date, product_id, planned_quantity?, target_fg_quantity?, batch_count, line_code?, assignee_id?, scheduled_start?, scheduled_end?, routing_version_id?, note?}` — 승인 BOM 자동 선택 + 스냅샷 + 필요량 |
| GET | /production-orders/{id} | production:read | 품목 필요량/예약/투입, 배치, WB, 공정, FG, 진행률 |
| GET | /production-orders/{id}/requirements | production:read | 가용재고 비교, 부족, 추천 LOT |
| POST | /production-orders/{id}/approve | production:approve | 예약 생성 `{require_full_stock?}` |
| POST | /production-orders/{id}/start | production:execute | → WAITING |
| POST | /production-orders/{id}/hold | production:execute | `{reason}` / `/release-hold` |
| POST | /production-orders/{id}/complete | production:execute | QUALITY_PENDING → COMPLETED |
| POST | /production-orders/{id}/close | production:close | `{remaining_wip_quantity?, confirmed_loss_quantity?, override_reason?}` 물질수지 |
| POST | /production-orders/{id}/cancel | production:write | `{reason}` 예약 해제 |
| GET | /production-orders/{id}/yield | production:read | 수율/달성률/불량률/물질수지 |

## 10. 계량

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | /weighing/validate-qr | weighing:execute | `{production_order_id, production_batch_id, raw}` → 원료/LOT/포장/목표/누적/남은량/현재·예상 잔량/기한/검수 + `valid`, `errors[]` (재고 변경 없음) |
| POST | /weighing/records | weighing:execute | `{production_order_id, production_batch_id, raw | package_id, actual_quantity, gross_weight?, tare_weight?, is_stable?, scale_equipment_id?, override_approved_by?}` (Idempotency-Key) → 차감 + 기록 |
| GET | /weighing/batches | production:read | `production_order_id` |
| GET | /weighing/batches/{id} | production:read | WB 상세(투입 원료/LOT/포장/실제량) |
| POST | /weighing/batches/{id}/complete | weighing:execute | 그룹 완료 → WB LOT/QR, WIP_RECEIPT |
| POST | /weighing/records/{id}/reverse | weighing:execute | `{reason}` 승인권자는 즉시 실행, 그 외 승인요청 생성 |
| POST | /weighing/records/{id}/return | weighing:execute | 미확정/오계량 반납 `{reason, contaminated}` (= reverse 와 동일 정정 경로, 오염 시 폐기) |

## 11. 공정 실행

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| POST | /process-executions | process:execute | `{production_order_id, production_batch_id?, routing_step_sequence | process_definition_id, equipment_id?, is_rework?}` |
| GET | /process-executions | process:read | `production_order_id` |
| GET | /process-executions/{id} | process:read | 입력/출력/조건/포장재 |
| POST | /process-executions/{id}/start | process:execute | 이전 공정 검사 |
| POST | /process-executions/{id}/inputs | process:execute | `{raw | trace_node_id, quantity, merge_approved_by?}` WB/WIP 투입 (Idempotency-Key) |
| POST | /process-executions/{id}/packaging | process:execute | `{raw | package_id, used_quantity, defective_quantity?, waste_quantity?}` 포장재 소모 |
| POST | /process-executions/{id}/parameters | process:execute | `{records:[{parameter_code, value_numeric?, value_text?, source?}]}` |
| POST | /process-executions/{id}/pause | process:execute | `/resume` |
| POST | /process-executions/{id}/complete | process:execute | `{outputs:[{quantity}], defective_quantity, waste_quantity, process_loss_quantity, moisture_change_quantity?, inspection_result?, note?, fg?:{good_quantity(EA), defective_quantity, waste_quantity}}` → WIP/FG LOT + QR |
| POST | /process-executions/{id}/hold | process:execute | `{reason}` |
| POST | /process-executions/{id}/approve | process:approve | 승인 필요 공정 |

## 12. 완제품

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | /finished-goods | fg:read | 필터: `quality_status, product_id, production_order_id` |
| POST | /finished-goods | fg:write | (보조) 완료된 포장 공정에서 FG LOT 생성 |
| GET | /finished-goods/{id} | fg:read | 원료/포장재 LOT, 공정 이력, 검사, 재고, 출고 |
| POST | /finished-goods/{id}/inspection | fg:inspect | `{results[], overall_result}` |
| POST | /finished-goods/{id}/release | fg:release | 품질 승인 → RELEASED(가용), 소비기한 확인 |
| POST | /finished-goods/{id}/hold | quality:hold 또는 fg:release | `{reason}` |
| POST | /finished-goods/{id}/labels | fg:write | `{level:"UNIT"|"BOX"|"PALLET", count, quantity_per_package}` 하위 포장 QR 생성 |

## 13. 출고 / 반품

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | /shipments | shipment:read | |
| POST | /shipments | shipment:write | `{customer_id, shipment_date?, delivery_address?, delivery_info?, items:[{fg_lot_id | raw, quantity}]}` |
| GET | /shipments/{id} | shipment:read | |
| POST | /shipments/{id}/confirm | shipment:confirm | 검증 + FG_SHIPMENT + 추적 (Idempotency-Key) |
| POST | /shipments/{id}/reverse | shipment:confirm | `{reason}` |
| POST | /shipments/{id}/return | shipment:return | `{shipment_item_id, quantity, reason}` → 격리 |
| POST | /shipment-returns/{id}/decide | fg:release | `{decision:"RESTOCK"|"DISPOSE", reason}` |
| GET | /shipments/{id}/statement | shipment:read | 출고명세서 PDF |

## 14. 추적 / 회수

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | /trace/forward/{lot_id} | trace:read | `lot_id` = trace node id 또는 엔티티 id 또는 LOT 번호. 영향 LOT, 재고, 출고, 거래처 요약 |
| GET | /trace/backward/{lot_id} | trace:read | 원료/포장재/공급업체/원산지/검수/COA/공정/작업자/설비, 실측 vs 비례추정 |
| GET | /trace/graph/{lot_id} | trace:read | `{nodes[], edges[]}` |
| POST | /trace/search | trace:read | `{q, node_type?}` LOT/공급업체 LOT/포장번호 검색 |
| GET/POST | /recalls | recall:read / manage | `{title, description, severity, lot_ids[]}` → 영향 범위 자동 산출 |
| GET | /recalls/{id} | recall:read | 대상 LOT/재고/출고/거래처, 진행률 |
| POST | /recalls/{id}/hold-stock | recall:manage | 영향 재고 품질 HOLD |
| POST | /recalls/{id}/recovery | recall:manage | `{recall_shipment_id, recovered_quantity}` |
| POST | /recalls/{id}/close | recall:manage | `{release_hold?:bool, reason}` |

## 15. 보고서

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET | /reports | report:read | 보고서 유형 목록 |
| GET | /reports/{report_type} | report:read | 화면 데이터 `{title, columns, rows, totals, filters}` (출력과 동일 쿼리) |
| POST | /reports/{report_type}/excel | report:export | `{filters}` → XLSX (대량은 비동기: `{export_id, status}`) |
| POST | /reports/{report_type}/pdf | report:export | `{filters, orientation?}` → PDF |
| GET | /report-exports/{id} | report:read | 상태/다운로드 (`?download=1`) |
| GET | /production-orders/{id}/record-bundle | report:export | `?format=pdf|xlsx` 제조지시 전체 생산기록 일괄 |

보고서 유형: `receipt_ledger`, `receipt_inspection`, `nonconformance`, `inventory_ledger_main|sub|other`, `inventory_balance_lot`, `inventory_balance_package`, `inventory_balance_warehouse`, `expiring`, `stock_count`, `stock_adjustment`, `bom`, `production_order`, `weighing_log`, `wb_ledger`, `process_log`, `ccp_log`, `packaging_usage`, `production_performance`, `material_usage`, `fg_production`, `fg_inventory`, `shipment_ledger`, `shipment_return`, `trace_forward`, `trace_backward`, `recall_targets`.

## 16. 시스템 / 공통

| 메서드 | 경로 | 권한 | 설명 |
|---|---|---|---|
| GET/POST | /users, PATCH /users/{id} | user:manage | 역할·공장 지정, 비밀번호 재설정 |
| GET | /roles | user:manage | 역할-권한 |
| GET/POST | /facilities | system:manage | |
| GET/PUT | /settings | system:manage | OCR 임계값, 알림 일수 등 |
| GET/POST | /printers, PATCH /printers/{id} | system:manage | 프린터 설정 |
| GET | /notifications, POST /notifications/{id}/read, POST /notifications/scan | 로그인 | 알림 |
| GET | /approvals, POST /approvals/{id}/approve, /reject | approval:decide | 승인 요청 |
| GET | /audit-logs | audit:read | 조회 전용 |
| GET | /dashboard | 로그인 | KPI·차트·알림 |
