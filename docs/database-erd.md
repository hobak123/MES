# 데이터베이스 ERD

> 이 문서는 `infrastructure/scripts/gen_erd.py` 가 SQLAlchemy 모델(`backend/app/models`)에서 자동 생성한다. 모델 변경 후 재생성할 것.

## 공통 규칙

- PK: UUID (`gen_random_uuid()`/uuid4). 조직 데이터는 `organization_id`, 공장 데이터는 `facility_id` 보유.
- 감사 컬럼: `created_at/created_by/updated_at/updated_by`. 수량 `NUMERIC(20,6)`, 시각 `TIMESTAMPTZ`(UTC 저장, Asia/Seoul 표시), 제조/기한 `DATE`.
- 거래 테이블은 물리 삭제 금지(DB 트리거), 마스터는 `is_active` 비활성화.
- 불변 테이블(UPDATE/DELETE 트리거 차단): inventory_transactions, audit_logs, trace_events, trace_event_inputs, trace_event_outputs, qr_print_logs.
- 음수 재고 금지: `inventory_balances.quantity >= 0`, `reserved_quantity <= quantity`, `inventory_transactions.balance_after >= 0` CHECK.
- 유일성: 조직별 품목코드/LOT/포장번호/거래번호, `(qr_type, entity_id)`, `(organization_id, idempotency_key)`, 재고 키 `(lot_node_id, package_node_id, warehouse_id, storage_location_id) NULLS NOT DISTINCT`.
- 공통 FK(`organization_id`, `facility_id`, `created_by`, `updated_by`)는 다이어그램 가독성을 위해 관계선을 생략한다.

총 테이블 수: **78**

## 조직/권한

```mermaid
erDiagram
  organizations {
    varchar code
    varchar name
    varchar business_number
    uuid logo_file_id
    boolean is_active
    uuid id PK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  facilities {
    varchar code
    varchar name
    varchar address
    boolean is_active
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  users {
    varchar username
    varchar full_name
    varchar email
    varchar phone
    varchar password_hash
    uuid default_facility_id FK
    boolean is_active
    integer failed_login_count
    timestamptz locked_until
    timestamptz last_login_at
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  roles {
    varchar code
    varchar name
    text description
    uuid id PK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  permissions {
    varchar code
    varchar name
    varchar module
    uuid id PK
  }
  user_roles {
    uuid user_id PK
    uuid role_id PK
  }
  role_permissions {
    uuid role_id PK
    uuid permission_id PK
  }
  user_facilities {
    uuid user_id PK
    uuid facility_id PK
  }
  user_sessions {
    uuid user_id FK
    varchar token_hash
    timestamptz issued_at
    timestamptz expires_at
    timestamptz revoked_at
    uuid replaced_by
    varchar ip_address
    varchar user_agent
    uuid id PK
  }
  facilities ||--o{ users : "default_facility_id"
  permissions ||--o{ role_permissions : "permission_id"
  roles ||--o{ role_permissions : "role_id"
  roles ||--o{ user_roles : "role_id"
  users ||--o{ user_facilities : "user_id"
  users ||--o{ user_roles : "user_id"
  users ||--o{ user_sessions : "user_id"
```

## 기준정보

```mermaid
erDiagram
  materials {
    varchar material_code
    varchar material_name
    varchar major_category
    uuid category_id FK
    varchar food_type
    boolean is_food_additive
    varchar specification
    uuid base_unit_id FK
    uuid purchase_unit_id FK
    uuid stock_unit_id FK
    varchar conversion_note
    numeric density_kg_per_l
    uuid default_supplier_id FK
    varchar manufacturer_name
    varchar default_country_of_origin
    varchar storage_condition
    uuid default_warehouse_id FK
    numeric safety_stock
    numeric max_stock
    varchar allergen_information
    boolean coa_required
    boolean inspection_required
    boolean expiry_tracking_required
    boolean lot_tracking_required
    boolean production_usable
    uuid inspection_template_id FK
    numeric receiving_temp_min
    numeric receiving_temp_max
    uuid image_file_id
    text note
    boolean is_active
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  material_categories {
    varchar major
    varchar code
    varchar name
    integer sort_order
    boolean is_active
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  suppliers {
    varchar code
    varchar name
    varchar business_number
    varchar contact_name
    varchar phone
    varchar email
    varchar address
    jsonb supplied_materials
    varchar trade_status
    text note
    boolean is_active
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  customers {
    varchar code
    varchar name
    varchar business_number
    varchar contact_name
    varchar phone
    varchar email
    varchar address
    varchar delivery_address
    varchar trade_status
    text note
    boolean is_active
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  products {
    varchar product_code
    varchar product_name
    varchar food_type
    varchar report_number
    varchar specification
    numeric content_quantity
    uuid content_unit_id FK
    uuid base_unit_id FK
    varchar packaging_unit
    integer units_per_box
    numeric standard_batch_size
    uuid standard_batch_unit_id FK
    integer shelf_life_days
    varchar shelf_life_basis
    varchar storage_condition
    numeric standard_yield_pct
    uuid default_warehouse_id FK
    uuid image_file_id
    text description
    boolean is_active
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  units {
    varchar code
    varchar name
    varchar dimension
    numeric factor_to_base
    boolean is_active
    uuid id PK
  }
  unit_conversions {
    uuid material_id FK
    uuid product_id FK
    uuid from_unit_id FK
    uuid to_unit_id FK
    numeric factor
    boolean is_density
    uuid verified_by FK
    boolean is_active
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  warehouses {
    varchar code
    varchar name
    varchar warehouse_type
    numeric temperature_min
    numeric temperature_max
    boolean is_active
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  storage_locations {
    uuid warehouse_id FK
    varchar code
    varchar zone
    varchar rack
    varchar name
    boolean is_active
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  equipments {
    varchar code
    varchar name
    varchar equipment_type
    varchar line_code
    varchar interface_type
    jsonb interface_config
    boolean is_active
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  material_categories ||--o{ materials : "category_id"
  materials ||--o{ unit_conversions : "material_id"
  products ||--o{ unit_conversions : "product_id"
  suppliers ||--o{ materials : "default_supplier_id"
  units ||--o{ materials : "base_unit_id"
  units ||--o{ materials : "purchase_unit_id"
  units ||--o{ materials : "stock_unit_id"
  units ||--o{ products : "base_unit_id"
  units ||--o{ products : "content_unit_id"
  units ||--o{ products : "standard_batch_unit_id"
  units ||--o{ unit_conversions : "from_unit_id"
  units ||--o{ unit_conversions : "to_unit_id"
  warehouses ||--o{ materials : "default_warehouse_id"
  warehouses ||--o{ products : "default_warehouse_id"
  warehouses ||--o{ storage_locations : "warehouse_id"
```

## 입고/OCR/품질

```mermaid
erDiagram
  material_receipts {
    varchar receipt_number
    timestamptz received_at
    uuid supplier_id FK
    varchar purchase_order_number
    varchar receipt_document_number
    varchar coa_number
    uuid received_by FK
    uuid inspected_by FK
    varchar inspection_result
    uuid approved_by FK
    timestamptz approved_at
    uuid posted_by FK
    timestamptz posted_at
    varchar status
    text status_reason
    text note
    integer version
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  material_receipt_items {
    uuid receipt_id FK
    integer line_no
    uuid material_id FK
    varchar supplier_lot_number
    varchar manufacturer_name
    numeric quantity
    uuid unit_id FK
    integer package_count
    numeric package_quantity
    numeric package_weight
    varchar country_of_origin
    date manufactured_date
    date expiry_date
    varchar expiry_type
    varchar storage_condition
    uuid warehouse_id FK
    uuid storage_location_id FK
    numeric unit_price
    numeric amount
    uuid material_lot_id FK
    uuid ocr_job_id FK
    text note
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  material_lots {
    uuid material_id FK
    uuid receipt_item_id FK
    varchar lot_number
    varchar supplier_lot_number
    uuid supplier_id FK
    varchar manufacturer_name
    varchar country_of_origin
    date received_date
    date manufactured_date
    date expiry_date
    varchar expiry_type
    numeric original_quantity
    uuid base_unit_id FK
    varchar quality_status
    varchar status
    uuid qr_code_id FK
    uuid trace_node_id FK
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  material_packages {
    uuid material_lot_id FK
    varchar package_number
    integer sequence_no
    numeric original_quantity
    uuid base_unit_id FK
    varchar package_status
    uuid qr_code_id FK
    uuid trace_node_id FK
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  receipt_documents {
    varchar document_type
    varchar document_number
    date issued_date
    uuid file_id FK
    varchar file_hash
    uuid is_duplicate_of FK
    jsonb coa_test_results
    text note
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  document_links {
    uuid document_id FK
    varchar entity_type
    uuid entity_id
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  ocr_jobs {
    uuid document_id FK
    uuid receipt_id FK
    varchar provider
    varchar document_type
    varchar status
    integer attempts
    text raw_text
    jsonb structured_result
    numeric overall_confidence
    boolean review_required
    text error_message
    timestamptz started_at
    timestamptz completed_at
    uuid confirmed_by FK
    timestamptz confirmed_at
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  ocr_extracted_fields {
    uuid ocr_job_id FK
    integer item_index
    varchar field_name
    text raw_value
    text normalized_value
    numeric confidence
    boolean is_missing
    boolean needs_review
    text corrected_value
    uuid corrected_by FK
    uuid matched_entity_id
    uuid id PK
    uuid organization_id FK
  }
  inspection_templates {
    varchar code
    varchar name
    varchar target_type
    boolean is_active
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  inspection_template_items {
    uuid template_id FK
    varchar item_code
    varchar item_name
    varchar value_type
    numeric min_value
    numeric max_value
    varchar criteria
    boolean is_required
    integer sort_order
    uuid id PK
  }
  quality_inspections {
    varchar inspection_number
    varchar target_type
    uuid target_id
    uuid template_id FK
    varchar source
    varchar overall_result
    text nonconformance_reason
    varchar disposition
    uuid inspected_by FK
    timestamptz inspected_at
    uuid approved_by FK
    timestamptz approved_at
    jsonb photo_file_ids
    text note
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  quality_inspection_results {
    uuid inspection_id FK
    varchar item_code
    varchar item_name
    varchar result
    numeric measured_value
    varchar text_value
    varchar standard
    boolean out_of_range
    text note
    uuid id PK
  }
  inspection_templates ||--o{ inspection_template_items : "template_id"
  inspection_templates ||--o{ quality_inspections : "template_id"
  material_lots ||--o{ material_packages : "material_lot_id"
  material_lots ||--o{ material_receipt_items : "material_lot_id"
  material_receipt_items ||--o{ material_lots : "receipt_item_id"
  material_receipts ||--o{ material_receipt_items : "receipt_id"
  material_receipts ||--o{ ocr_jobs : "receipt_id"
  ocr_jobs ||--o{ material_receipt_items : "ocr_job_id"
  ocr_jobs ||--o{ ocr_extracted_fields : "ocr_job_id"
  quality_inspections ||--o{ quality_inspection_results : "inspection_id"
  receipt_documents ||--o{ document_links : "document_id"
  receipt_documents ||--o{ ocr_jobs : "document_id"
```

## 재고

```mermaid
erDiagram
  inventory_balances {
    varchar item_type
    uuid material_id FK
    uuid product_id FK
    uuid lot_node_id FK
    uuid package_node_id FK
    uuid warehouse_id FK
    uuid storage_location_id FK
    numeric quantity
    numeric reserved_quantity
    uuid unit_id FK
    varchar lot_number
    varchar package_number
    varchar quality_status
    date expiry_date
    timestamptz last_movement_at
    integer version
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
  }
  inventory_transactions {
    varchar transaction_number
    varchar transaction_type
    varchar item_type
    uuid material_id FK
    uuid product_id FK
    uuid balance_id FK
    uuid trace_node_id FK
    uuid lot_node_id FK
    uuid material_lot_id FK
    uuid package_id
    uuid warehouse_id FK
    uuid storage_location_id FK
    varchar quality_status
    numeric quantity
    uuid base_unit_id FK
    varchar direction
    numeric balance_before
    numeric balance_after
    varchar reference_type
    uuid reference_id
    uuid transaction_group_id
    uuid reversal_of_id FK
    varchar idempotency_key
    uuid performed_by FK
    timestamptz performed_at
    text reason
    timestamptz created_at
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
  }
  inventory_reservations {
    uuid balance_id FK
    uuid production_order_id FK
    uuid production_order_item_id
    uuid shipment_id
    numeric quantity
    numeric consumed_quantity
    varchar status
    text reason
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  stock_transfers {
    varchar transfer_number
    uuid balance_id FK
    uuid to_warehouse_id FK
    uuid to_storage_location_id FK
    numeric quantity
    uuid transaction_group_id
    varchar status
    text reason
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  stock_counts {
    varchar count_number
    date count_date
    uuid warehouse_id FK
    jsonb lines
    varchar status
    text note
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  stock_adjustments {
    varchar adjustment_number
    uuid balance_id FK
    varchar direction
    varchar adjustment_kind
    numeric quantity
    text reason
    uuid stock_count_id FK
    varchar status
    uuid requested_by FK
    uuid approved_by FK
    timestamptz approved_at
    uuid transaction_group_id
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  inventory_balances ||--o{ inventory_reservations : "balance_id"
  inventory_balances ||--o{ inventory_transactions : "balance_id"
  inventory_balances ||--o{ stock_adjustments : "balance_id"
  inventory_balances ||--o{ stock_transfers : "balance_id"
  stock_counts ||--o{ stock_adjustments : "stock_count_id"
```

## BOM

```mermaid
erDiagram
  bom_headers {
    varchar bom_code
    uuid product_id FK
    varchar name
    boolean is_active
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  bom_versions {
    uuid bom_header_id FK
    integer version_no
    numeric base_quantity
    uuid base_unit_id FK
    numeric base_fg_quantity
    varchar status
    date effective_from
    date effective_to
    uuid approved_by FK
    timestamptz approved_at
    text change_note
    uuid copied_from_id FK
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  bom_items {
    uuid bom_version_id FK
    varchar item_type
    uuid material_id FK
    varchar quantity_basis
    numeric ratio_percent
    numeric standard_quantity
    uuid unit_id FK
    numeric tolerance_pct
    integer sequence
    varchar process_step_code
    varchar weighing_group
    boolean is_required
    boolean substitute_allowed
    jsonb substitute_material_ids
    text note
    uuid id PK
    uuid organization_id FK
  }
  bom_headers ||--o{ bom_versions : "bom_header_id"
  bom_versions ||--o{ bom_items : "bom_version_id"
```

## 생산/MES

```mermaid
erDiagram
  production_orders {
    varchar order_number
    date production_date
    uuid product_id FK
    uuid bom_version_id FK
    jsonb bom_snapshot
    uuid routing_version_id FK
    jsonb routing_snapshot
    numeric planned_quantity
    uuid planned_unit_id FK
    numeric target_fg_quantity
    integer batch_count
    numeric batch_size
    varchar line_code
    varchar work_center
    uuid assignee_id FK
    timestamptz scheduled_start
    timestamptz scheduled_end
    timestamptz actual_start
    timestamptz actual_end
    varchar status
    varchar status_before_hold
    uuid approved_by FK
    timestamptz approved_at
    uuid closed_by FK
    timestamptz closed_at
    jsonb mass_balance
    text note
    uuid qr_code_id FK
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  production_order_items {
    uuid production_order_id FK
    uuid bom_item_id FK
    varchar item_type
    uuid material_id FK
    varchar quantity_basis
    numeric ratio_percent
    numeric required_quantity
    numeric per_batch_quantity
    uuid unit_id FK
    numeric tolerance_pct
    integer sequence
    varchar weighing_group
    varchar process_step_code
    boolean substitute_allowed
    jsonb substitute_material_ids
    numeric issued_quantity
    uuid id PK
    uuid organization_id FK
  }
  production_batches {
    uuid production_order_id FK
    varchar batch_number
    integer batch_no
    numeric planned_quantity
    varchar status
    timestamptz started_at
    timestamptz completed_at
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  production_routings {
    varchar routing_code
    uuid product_id FK
    varchar name
    boolean is_active
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  routing_versions {
    uuid routing_id FK
    integer version_no
    varchar status
    uuid approved_by FK
    timestamptz approved_at
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  routing_steps {
    uuid routing_version_id FK
    uuid process_definition_id FK
    integer sequence
    varchar step_name
    boolean is_required
    uuid equipment_id FK
    jsonb parameter_specs
    varchar output_type
    uuid id PK
    uuid organization_id FK
  }
  process_definitions {
    varchar code
    varchar name
    varchar process_type
    integer default_sequence
    varchar work_center
    uuid equipment_id FK
    numeric standard_minutes
    numeric standard_temperature
    numeric standard_pressure
    numeric standard_speed
    jsonb parameter_specs
    text inspection_criteria
    jsonb required_records
    boolean approval_required
    boolean qr_required
    boolean is_ccp
    varchar output_type
    boolean is_active
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  weighing_batches {
    varchar lot_number
    uuid production_order_id FK
    uuid production_batch_id FK
    uuid product_id FK
    uuid bom_version_id FK
    varchar weighing_group
    uuid parent_id FK
    numeric total_quantity
    uuid unit_id FK
    uuid completed_by FK
    timestamptz completed_at
    uuid warehouse_id FK
    uuid storage_location_id FK
    timestamptz usable_until
    varchar quality_status
    varchar status
    varchar next_process_code
    uuid qr_code_id FK
    uuid trace_node_id FK
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  weighing_records {
    uuid weighing_batch_id FK
    uuid production_order_id FK
    uuid production_batch_id FK
    uuid production_order_item_id FK
    uuid material_id FK
    uuid material_lot_id FK
    uuid material_package_id FK
    boolean is_substitute
    numeric target_quantity
    numeric actual_quantity
    uuid unit_id FK
    numeric tolerance_min
    numeric tolerance_max
    numeric gross_weight
    numeric tare_weight
    boolean is_stable
    uuid scale_equipment_id FK
    varchar input_method
    uuid inventory_transaction_id FK
    uuid weighed_by FK
    timestamptz weighed_at
    varchar approval_status
    uuid approved_by FK
    varchar status
    uuid reversed_by FK
    timestamptz reversed_at
    text reversal_reason
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  wip_lots {
    varchar lot_number
    uuid production_order_id FK
    uuid production_batch_id FK
    uuid product_id FK
    uuid process_execution_id FK
    integer routing_step_sequence
    numeric quantity
    uuid unit_id FK
    varchar quality_status
    varchar status
    uuid qr_code_id FK
    uuid trace_node_id FK
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  process_executions {
    varchar execution_number
    uuid production_order_id FK
    uuid production_batch_id FK
    uuid routing_step_id FK
    integer routing_step_sequence
    uuid process_definition_id FK
    uuid equipment_id FK
    boolean is_rework
    uuid merge_approved_by FK
    timestamptz started_at
    timestamptz paused_at
    numeric paused_minutes
    timestamptz completed_at
    numeric total_input_quantity
    numeric good_output_quantity
    numeric defective_quantity
    numeric waste_quantity
    numeric process_loss_quantity
    numeric moisture_change_quantity
    uuid unit_id FK
    numeric process_yield
    varchar quality_status
    varchar inspection_result
    uuid operated_by FK
    uuid approved_by FK
    timestamptz approved_at
    varchar status
    text note
    text reason
    jsonb photo_file_ids
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  process_inputs {
    uuid process_execution_id FK
    varchar input_type
    uuid trace_node_id FK
    varchar lot_number
    uuid production_batch_id FK
    numeric quantity
    uuid unit_id FK
    uuid inventory_transaction_id FK
    uuid input_by FK
    timestamptz input_at
    uuid id PK
    uuid organization_id FK
  }
  process_outputs {
    uuid process_execution_id FK
    varchar output_type
    uuid trace_node_id FK
    varchar lot_number
    numeric quantity
    uuid unit_id FK
    uuid inventory_transaction_id FK
    uuid id PK
    uuid organization_id FK
  }
  process_parameter_records {
    uuid process_execution_id FK
    varchar parameter_code
    varchar parameter_name
    numeric value_numeric
    varchar value_text
    varchar unit
    numeric min_value
    numeric max_value
    boolean out_of_range
    varchar source
    boolean is_ccp
    uuid recorded_by FK
    timestamptz recorded_at
    uuid id PK
    uuid organization_id FK
  }
  packaging_consumptions {
    uuid process_execution_id FK
    uuid production_order_id FK
    uuid material_id FK
    uuid material_lot_id FK
    uuid material_package_id FK
    numeric issued_quantity
    numeric used_quantity
    numeric defective_quantity
    numeric waste_quantity
    numeric returned_quantity
    uuid unit_id FK
    uuid fg_lot_id FK
    uuid inventory_transaction_id FK
    uuid consumed_by FK
    timestamptz consumed_at
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
  }
  process_definitions ||--o{ process_executions : "process_definition_id"
  process_definitions ||--o{ routing_steps : "process_definition_id"
  process_executions ||--o{ packaging_consumptions : "process_execution_id"
  process_executions ||--o{ process_inputs : "process_execution_id"
  process_executions ||--o{ process_outputs : "process_execution_id"
  process_executions ||--o{ process_parameter_records : "process_execution_id"
  process_executions ||--o{ wip_lots : "process_execution_id"
  production_batches ||--o{ process_executions : "production_batch_id"
  production_batches ||--o{ process_inputs : "production_batch_id"
  production_batches ||--o{ weighing_batches : "production_batch_id"
  production_batches ||--o{ weighing_records : "production_batch_id"
  production_batches ||--o{ wip_lots : "production_batch_id"
  production_order_items ||--o{ weighing_records : "production_order_item_id"
  production_orders ||--o{ packaging_consumptions : "production_order_id"
  production_orders ||--o{ process_executions : "production_order_id"
  production_orders ||--o{ production_batches : "production_order_id"
  production_orders ||--o{ production_order_items : "production_order_id"
  production_orders ||--o{ weighing_batches : "production_order_id"
  production_orders ||--o{ weighing_records : "production_order_id"
  production_orders ||--o{ wip_lots : "production_order_id"
  production_routings ||--o{ routing_versions : "routing_id"
  routing_steps ||--o{ process_executions : "routing_step_id"
  routing_versions ||--o{ production_orders : "routing_version_id"
  routing_versions ||--o{ routing_steps : "routing_version_id"
  weighing_batches ||--o{ weighing_records : "weighing_batch_id"
```

## 완제품/출고

```mermaid
erDiagram
  finished_goods_lots {
    varchar lot_number
    uuid product_id FK
    uuid production_order_id FK
    uuid production_batch_id FK
    uuid process_execution_id FK
    date manufactured_date
    timestamptz production_started_at
    timestamptz production_ended_at
    date expiry_date
    date expiry_basis_date
    numeric content_quantity
    uuid content_unit_id FK
    numeric planned_quantity
    numeric total_produced_quantity
    numeric good_quantity
    numeric defective_quantity
    numeric waste_quantity
    numeric total_input_quantity
    numeric net_weight
    numeric loss_quantity
    uuid base_unit_id FK
    numeric production_yield
    varchar quality_status
    uuid released_by FK
    timestamptz released_at
    uuid responsible_id FK
    uuid warehouse_id FK
    varchar status
    uuid qr_code_id FK
    uuid trace_node_id FK
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  finished_goods_packages {
    uuid fg_lot_id FK
    uuid parent_package_id FK
    varchar package_level
    varchar package_number
    numeric quantity
    varchar status
    uuid qr_code_id FK
    uuid trace_node_id FK
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  shipment_headers {
    varchar shipment_number
    date shipment_date
    uuid customer_id FK
    varchar delivery_address
    jsonb delivery_info
    uuid handler_id FK
    uuid approved_by FK
    timestamptz confirmed_at
    varchar status
    uuid transaction_group_id
    uuid trace_node_id FK
    text note
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  shipment_items {
    uuid shipment_id FK
    integer line_no
    uuid product_id FK
    uuid fg_lot_id FK
    uuid fg_package_id FK
    uuid warehouse_id FK
    numeric quantity
    uuid unit_id FK
    numeric returned_quantity
    uuid inventory_transaction_id FK
    uuid id PK
    uuid organization_id FK
  }
  shipment_returns {
    varchar return_number
    uuid shipment_id FK
    uuid shipment_item_id FK
    uuid fg_lot_id FK
    numeric quantity
    text reason
    uuid quarantine_warehouse_id FK
    varchar status
    uuid decided_by FK
    timestamptz decided_at
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  finished_goods_lots ||--o{ finished_goods_packages : "fg_lot_id"
  finished_goods_lots ||--o{ shipment_items : "fg_lot_id"
  finished_goods_lots ||--o{ shipment_returns : "fg_lot_id"
  finished_goods_packages ||--o{ shipment_items : "fg_package_id"
  shipment_headers ||--o{ shipment_items : "shipment_id"
  shipment_headers ||--o{ shipment_returns : "shipment_id"
  shipment_items ||--o{ shipment_returns : "shipment_item_id"
```

## 추적/회수

```mermaid
erDiagram
  trace_nodes {
    varchar node_type
    uuid entity_id
    varchar lot_number
    varchar label
    timestamptz created_at
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
  }
  trace_events {
    varchar event_type
    uuid production_order_id FK
    uuid production_batch_id FK
    varchar reference_type
    uuid reference_id
    boolean is_reversed
    uuid reversal_of_id FK
    uuid performed_by FK
    timestamptz performed_at
    text note
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
  }
  trace_event_inputs {
    uuid trace_event_id FK
    uuid trace_node_id FK
    numeric quantity
    uuid unit_id FK
    uuid id PK
  }
  trace_event_outputs {
    uuid trace_event_id FK
    uuid trace_node_id FK
    numeric quantity
    uuid unit_id FK
    uuid id PK
  }
  recall_cases {
    varchar case_number
    varchar title
    text description
    varchar severity
    jsonb source_node_ids
    varchar status
    uuid opened_by FK
    timestamptz closed_at
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  recall_case_lots {
    uuid recall_case_id FK
    uuid trace_node_id FK
    varchar node_type
    varchar lot_number
    numeric stock_quantity
    boolean hold_applied
    varchar previous_quality_status
    uuid id PK
  }
  recall_case_shipments {
    uuid recall_case_id FK
    uuid shipment_id FK
    uuid shipment_item_id FK
    uuid customer_id FK
    uuid fg_lot_id FK
    numeric shipped_quantity
    numeric target_quantity
    numeric recovered_quantity
    varchar status
    uuid id PK
  }
  recall_cases ||--o{ recall_case_lots : "recall_case_id"
  recall_cases ||--o{ recall_case_shipments : "recall_case_id"
  trace_events ||--o{ trace_event_inputs : "trace_event_id"
  trace_events ||--o{ trace_event_outputs : "trace_event_id"
  trace_nodes ||--o{ recall_case_lots : "trace_node_id"
  trace_nodes ||--o{ trace_event_inputs : "trace_node_id"
  trace_nodes ||--o{ trace_event_outputs : "trace_node_id"
```

## 공통

```mermaid
erDiagram
  qr_codes {
    varchar qr_type
    uuid entity_id
    varchar lot_number
    varchar payload
    varchar status
    integer print_count
    timestamptz created_at
    uuid created_by FK
    uuid id PK
    uuid organization_id FK
  }
  qr_print_logs {
    uuid qr_code_id FK
    uuid printer_id FK
    varchar print_type
    varchar output_format
    varchar label_size
    integer copies
    varchar status
    text error_message
    text reason
    uuid printed_by FK
    timestamptz printed_at
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
  }
  printer_settings {
    varchar name
    varchar printer_type
    varchar host
    integer port
    integer label_width_mm
    integer label_height_mm
    integer dpi
    boolean auto_print
    boolean is_default
    boolean is_active
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  file_attachments {
    varchar original_name
    varchar storage_key
    varchar mime_type
    biginteger size_bytes
    varchar sha256
    varchar entity_type
    uuid entity_id
    boolean is_private
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  approval_requests {
    varchar request_type
    varchar target_type
    uuid target_id
    jsonb payload
    text reason
    varchar status
    uuid requested_by FK
    uuid decided_by FK
    timestamptz decided_at
    text decision_note
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  notifications {
    varchar category
    varchar severity
    varchar title
    text message
    varchar target_type
    uuid target_id
    varchar target_role
    varchar dedup_key
    boolean is_read
    uuid read_by FK
    timestamptz created_at
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
  }
  report_export_logs {
    varchar report_type
    varchar export_format
    jsonb filters
    integer row_count
    varchar status
    uuid file_id FK
    text error_message
    uuid requested_by FK
    timestamptz requested_at
    timestamptz completed_at
    uuid id PK
    uuid organization_id FK
    uuid facility_id FK
  }
  audit_logs {
    uuid organization_id FK
    uuid facility_id FK
    uuid user_id FK
    varchar username
    varchar action
    varchar target_type
    varchar target_id
    jsonb before_data
    jsonb after_data
    text reason
    varchar request_id
    varchar ip_address
    varchar user_agent
    timestamptz created_at
    uuid id PK
  }
  idempotency_keys {
    uuid organization_id FK
    uuid user_id FK
    varchar key
    varchar method
    varchar path
    varchar request_hash
    integer response_status
    jsonb response_body
    timestamptz created_at
    uuid id PK
  }
  number_sequences {
    uuid organization_id PK
    varchar prefix PK
    date seq_date PK
    biginteger last_value
  }
  system_settings {
    varchar key
    jsonb value
    varchar description
    uuid id PK
    uuid organization_id FK
    timestamptz created_at
    timestamptz updated_at
    uuid created_by FK
    uuid updated_by FK
  }
  file_attachments ||--o{ report_export_logs : "file_id"
  printer_settings ||--o{ qr_print_logs : "printer_id"
  qr_codes ||--o{ qr_print_logs : "qr_code_id"
```

## 영역 간 핵심 관계

```mermaid
erDiagram
  material_receipt_items ||--o| material_lots : "receipt_item_id"
  material_lots ||--o{ material_packages : "material_lot_id"
  material_packages ||--o{ weighing_records : "material_package_id"
  weighing_batches ||--o{ weighing_records : "weighing_batch_id"
  production_orders ||--o{ weighing_batches : "production_order_id"
  process_executions ||--o{ wip_lots : "process_execution_id"
  production_orders ||--o{ finished_goods_lots : "production_order_id"
  finished_goods_lots ||--o{ shipment_items : "fg_lot_id"
  trace_nodes ||--o{ inventory_balances : "lot_node_id"
  inventory_balances ||--o{ inventory_transactions : "balance_id"
  trace_nodes ||--o{ trace_event_inputs : "trace_node_id"
  trace_nodes ||--o{ trace_event_outputs : "trace_node_id"
  qr_codes ||--o{ qr_print_logs : "qr_code_id"
```
