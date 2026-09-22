from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlite3 import Connection

from .. import crud
from ..database import get_db
from ..engine import assess_deactivation, build_report
from ..schemas import DeactivationImpactReport, DeactivationIn, TraceReport

router = APIRouter(prefix="/api", tags=["trace"])


def _report(db: Connection, instrument: str, at: Optional[datetime]) -> dict:
    try:
        return build_report(db, instrument, at)
    except KeyError:
        raise HTTPException(404, f"仪器不存在: {instrument}")


@router.get("/trace/{instrument}", response_model=TraceReport)
def trace_chain(
    instrument: str,
    at: Optional[datetime] = None,
    db: Connection = Depends(get_db),
):
    """链路查询：按指定时点重建该仪器到国家基准的证书链。"""
    return _report(db, instrument, at)


@router.get("/reports/{instrument}", response_model=TraceReport)
def validation_report(
    instrument: str,
    at: Optional[datetime] = None,
    db: Connection = Depends(get_db),
):
    """校验报告：重建结果 + 区间/签名/不确定度的可解释冲突明细。"""
    return _report(db, instrument, at)


@router.post(
    "/assessments/certificate-deactivation",
    response_model=DeactivationImpactReport,
    tags=["impact-assessment"],
)
def certificate_deactivation_impact(
    body: DeactivationIn, db: Connection = Depends(get_db)
):
    """证书停用影响评估（dry-run）。

    在不写入数据库的前提下，模拟移除待停用证书及其关联溯源边，按查询时点
    重新评估受影响仪器的唯一链 / 多候选 / 断裂状态，给出停用前后状态、
    替代链摘要与可解释说明；可用 ``instrument_codes`` 限定评估仪器范围。
    """
    try:
        return assess_deactivation(
            db,
            body.at,
            body.certificate_ids,
            body.instrument_codes,
        )
    except crud.ValidationError as exc:
        raise HTTPException(422, str(exc))
