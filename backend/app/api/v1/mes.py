"""MES: 계량 / 계량반제품(WB) / 공정 실행 / 포장재."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.deps import CurrentUser, require
from app.core.errors import NotFound
from app.core.idempotency import Idem, idempotency
from app.core.responses import ok
from app.models import ProcessExecution, WeighingBatch, WeighingRecord
from app.repositories.common import ser
from app.services.production import process as psvc
from app.services.weighing import service as wsvc

router = APIRouter(tags=["MES"])


# ================================================================ 계량

class ValidateIn(BaseModel):
    production_order_id: uuid.UUID
    production_batch_id: uuid.UUID
    raw: str | None = None
    package_id: uuid.UUID | None = None
    actual_quantity: Decimal | None = None


class RecordIn(BaseModel):
    production_order_id: uuid.UUID
    production_batch_id: uuid.UUID
    raw: str | None = None
    package_id: uuid.UUID | None = None
    actual_quantity: Decimal = Field(gt=0)
    gross_weight: Decimal | None = None
    tare_weight: Decimal | None = None
    is_stable: bool | None = None
    scale_equipment_id: uuid.UUID | None = None
    override_approved_by: uuid.UUID | None = None


class ReverseIn(BaseModel):
    reason: str = Field(min_length=1)
    contaminated: bool = False


class CompleteWbIn(BaseModel):
    usable_hours: int | None = Field(default=None, ge=1, le=720)


@router.post("/weighing/validate-qr")
def validate_qr(body: ValidateIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("weighing:execute"))):
    _, info = wsvc.evaluate(db, cu, body.production_order_id, body.production_batch_id, body.raw, body.package_id, body.actual_quantity)
    return ok(info, "사용 가능한 원료입니다." if info.get("valid") else "투입할 수 없는 원료입니다.")


@router.post("/weighing/records")
def create_record(body: RecordIn, idem: Idem = Depends(idempotency), db: Session = Depends(get_db),
                  cu: CurrentUser = Depends(require("weighing:execute"))):
    if (replay := idem.begin()) is not None:
        return replay
    rec = wsvc.record(db, cu, body.model_dump(), idem.key)
    wb = db.get(WeighingBatch, rec.weighing_batch_id)
    _, info = wsvc.evaluate(db, cu, body.production_order_id, body.production_batch_id, package_id=rec.material_package_id)
    resp = idem.store(ok({"record": ser(rec), "weighing_batch": wsvc.wb_detail(db, wb), "after": info}, "계량이 확정되었습니다."))
    db.commit()
    return resp


@router.get("/weighing/batches")
def list_wbs(production_order_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("production:read"))):
    rows = db.execute(select(WeighingBatch).where(WeighingBatch.production_order_id == production_order_id,
                                                  WeighingBatch.organization_id == cu.organization_id).order_by(WeighingBatch.created_at)).scalars()
    return ok([wsvc.wb_detail(db, w) for w in rows])


@router.get("/weighing/batches/{wb_id}")
def get_wb(wb_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("production:read"))):
    wb = db.get(WeighingBatch, wb_id)
    if not wb or wb.organization_id != cu.organization_id:
        raise NotFound("계량 반제품")
    return ok(wsvc.wb_detail(db, wb))


@router.post("/weighing/batches/{wb_id}/complete")
def complete_wb(wb_id: uuid.UUID, body: CompleteWbIn | None = None, idem: Idem = Depends(idempotency), db: Session = Depends(get_db),
                cu: CurrentUser = Depends(require("weighing:execute"))):
    if (replay := idem.begin()) is not None:
        return replay
    wb = wsvc.complete_wb(db, cu, wb_id, body.usable_hours if body else None)
    resp = idem.store(ok(wsvc.wb_detail(db, wb), f"계량 반제품 {wb.lot_number} 이(가) 생성되었습니다."))
    db.commit()
    return resp


@router.post("/weighing/records/{rec_id}/reverse")
def reverse_record(rec_id: uuid.UUID, body: ReverseIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("weighing:execute"))):
    res = wsvc.reverse_record(db, cu, rec_id, body.reason, body.contaminated)
    db.commit()
    return ok(res, "계량 정정(역거래)이 완료되었습니다." if res["status"] == "REVERSED" else "정정 승인 요청이 등록되었습니다.")


@router.post("/weighing/records/{rec_id}/return")
def return_record(rec_id: uuid.UUID, body: ReverseIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("weighing:execute"))):
    """반납: 반납 상태(오염 여부) 확인 필수. 오염/혼합 원료는 폐기 처리되어 정상 재고로 돌아가지 않는다."""
    res = wsvc.reverse_record(db, cu, rec_id, body.reason, body.contaminated)
    db.commit()
    return ok(res, "반납 처리되었습니다." if not body.contaminated else "오염 원료로 폐기 처리되었습니다.")


@router.get("/weighing/records")
def list_records(production_order_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("production:read"))):
    rows = db.execute(select(WeighingRecord).where(WeighingRecord.production_order_id == production_order_id,
                                                   WeighingRecord.organization_id == cu.organization_id).order_by(WeighingRecord.weighed_at)).scalars()
    return ok([ser(r) for r in rows])


# ================================================================ 공정 실행

class ExecIn(BaseModel):
    production_order_id: uuid.UUID
    production_batch_id: uuid.UUID | None = None
    routing_step_sequence: int | None = None
    process_definition_id: uuid.UUID | None = None
    equipment_id: uuid.UUID | None = None
    is_rework: bool = False
    note: str | None = None


class InputIn(BaseModel):
    raw: str | None = None
    trace_node_id: uuid.UUID | None = None
    quantity: Decimal = Field(gt=0)
    merge_approved_by: uuid.UUID | None = None


class PackagingIn(BaseModel):
    raw: str | None = None
    package_id: uuid.UUID | None = None
    used_quantity: Decimal = Field(ge=0)
    defective_quantity: Decimal = Field(default=Decimal(0), ge=0)
    waste_quantity: Decimal = Field(default=Decimal(0), ge=0)


class ParamRec(BaseModel):
    parameter_code: str
    parameter_name: str | None = None
    value_numeric: Decimal | None = None
    value_text: str | None = None
    unit: str | None = None
    source: Literal["MANUAL", "SENSOR", "PLC"] = "MANUAL"


class ParamsIn(BaseModel):
    records: list[ParamRec] = Field(min_length=1)


class OutputSpec(BaseModel):
    quantity: Decimal = Field(gt=0)


class FgSpec(BaseModel):
    good_quantity: Decimal = Field(gt=0)
    defective_quantity: Decimal = Field(default=Decimal(0), ge=0)
    waste_quantity: Decimal = Field(default=Decimal(0), ge=0)


class CompleteIn(BaseModel):
    outputs: list[OutputSpec] = []
    defective_quantity: Decimal = Field(default=Decimal(0), ge=0)
    waste_quantity: Decimal = Field(default=Decimal(0), ge=0)
    process_loss_quantity: Decimal = Field(default=Decimal(0), ge=0)
    moisture_change_quantity: Decimal = Decimal(0)
    inspection_result: Literal["PASS", "FAIL", "HOLD", "NOT_APPLICABLE"] | None = None
    note: str | None = None
    photo_file_ids: list[uuid.UUID] | None = None
    fg: FgSpec | None = None


class ReasonIn(BaseModel):
    reason: str = Field(min_length=1)


class NoteIn(BaseModel):
    note: str | None = None


@router.post("/process-executions")
def create_exec(body: ExecIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:execute"))):
    e = psvc.create(db, cu, body.model_dump())
    db.commit()
    return ok(psvc.detail(db, e), "공정 작업이 생성되었습니다.")


@router.get("/process-executions")
def list_execs(production_order_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:read"))):
    rows = db.execute(select(ProcessExecution).where(ProcessExecution.production_order_id == production_order_id,
                                                     ProcessExecution.organization_id == cu.organization_id)
                      .order_by(ProcessExecution.routing_step_sequence, ProcessExecution.created_at)).scalars()
    return ok([psvc.detail(db, e) for e in rows])


@router.get("/process-executions/{exec_id}")
def get_exec(exec_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:read"))):
    return ok(psvc.detail(db, psvc.get_exec(db, cu, exec_id)))


@router.post("/process-executions/{exec_id}/start")
def start_exec(exec_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:execute"))):
    e = psvc.get_exec(db, cu, exec_id, lock=True)
    psvc.start(db, cu, e)
    db.commit()
    return ok(psvc.detail(db, e), "공정이 시작되었습니다.")


@router.post("/process-executions/{exec_id}/inputs")
def add_input(exec_id: uuid.UUID, body: InputIn, idem: Idem = Depends(idempotency), db: Session = Depends(get_db),
              cu: CurrentUser = Depends(require("process:execute"))):
    if (replay := idem.begin()) is not None:
        return replay
    e = psvc.get_exec(db, cu, exec_id, lock=True)
    psvc.add_input(db, cu, e, body.model_dump(), idem.key)
    resp = idem.store(ok(psvc.detail(db, e), "투입되었습니다."))
    db.commit()
    return resp


@router.post("/process-executions/{exec_id}/packaging")
def add_packaging(exec_id: uuid.UUID, body: PackagingIn, idem: Idem = Depends(idempotency), db: Session = Depends(get_db),
                  cu: CurrentUser = Depends(require("process:execute"))):
    if (replay := idem.begin()) is not None:
        return replay
    e = psvc.get_exec(db, cu, exec_id, lock=True)
    psvc.add_packaging(db, cu, e, body.model_dump(), idem.key)
    resp = idem.store(ok(psvc.detail(db, e), "포장재 사용이 기록되었습니다."))
    db.commit()
    return resp


@router.post("/process-executions/{exec_id}/parameters")
def add_params(exec_id: uuid.UUID, body: ParamsIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:execute"))):
    e = psvc.get_exec(db, cu, exec_id, lock=True)
    res = psvc.record_parameters(db, cu, e, [r.model_dump() for r in body.records])
    db.commit()
    warn = next((x for x in res if "warnings" in x), None)
    return ok({"records": [x for x in res if "warnings" not in x], "warnings": warn["warnings"] if warn else [],
               "held": warn["held"] if warn else False, "execution": psvc.detail(db, e)},
              "공정 조건이 기록되었습니다." + (" (기준 이탈 경고)" if warn else ""))


@router.post("/process-executions/{exec_id}/pause")
def pause_exec(exec_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:execute"))):
    e = psvc.get_exec(db, cu, exec_id, lock=True)
    psvc.pause(db, cu, e)
    db.commit()
    return ok(psvc.detail(db, e), "일시정지되었습니다.")


@router.post("/process-executions/{exec_id}/resume")
def resume_exec(exec_id: uuid.UUID, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:execute"))):
    e = psvc.get_exec(db, cu, exec_id, lock=True)
    psvc.pause(db, cu, e, resume=True)
    db.commit()
    return ok(psvc.detail(db, e), "재개되었습니다.")


@router.post("/process-executions/{exec_id}/complete")
def complete_exec(exec_id: uuid.UUID, body: CompleteIn, idem: Idem = Depends(idempotency), db: Session = Depends(get_db),
                  cu: CurrentUser = Depends(require("process:execute"))):
    if (replay := idem.begin()) is not None:
        return replay
    e = psvc.get_exec(db, cu, exec_id, lock=True)
    res = psvc.complete(db, cu, e, body.model_dump())
    resp = idem.store(ok({**res, "execution": psvc.detail(db, e)}, "공정이 완료되었습니다."))
    db.commit()
    return resp


@router.post("/process-executions/{exec_id}/hold")
def hold_exec(exec_id: uuid.UUID, body: ReasonIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:execute"))):
    e = psvc.get_exec(db, cu, exec_id, lock=True)
    psvc.hold(db, cu, e, body.reason)
    db.commit()
    return ok(psvc.detail(db, e), "공정이 보류되었습니다.")


@router.post("/process-executions/{exec_id}/release-hold")
def release_exec(exec_id: uuid.UUID, body: ReasonIn, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:approve"))):
    e = psvc.get_exec(db, cu, exec_id, lock=True)
    psvc.release_hold(db, cu, e, body.reason)
    db.commit()
    return ok(psvc.detail(db, e), "보류가 해제되었습니다.")


@router.post("/process-executions/{exec_id}/approve")
def approve_exec(exec_id: uuid.UUID, body: NoteIn | None = None, db: Session = Depends(get_db), cu: CurrentUser = Depends(require("process:approve"))):
    e = psvc.get_exec(db, cu, exec_id, lock=True)
    psvc.approve(db, cu, e, body.note if body else None)
    db.commit()
    return ok(psvc.detail(db, e), "공정이 승인되었습니다.")
