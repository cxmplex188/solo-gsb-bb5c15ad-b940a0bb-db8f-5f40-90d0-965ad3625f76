from fastapi import APIRouter, Depends
from sqlite3 import Connection

from .. import crud, preflight
from ..database import get_db, get_readonly_db
from ..schemas import (
    PreflightResult,
    SubmissionIn,
    SubmissionPreflightIn,
    SubmissionResult,
)

router = APIRouter(prefix="/api/submissions", tags=["submissions"])


@router.post("", response_model=SubmissionResult, status_code=201)
def submit_batch(body: SubmissionIn, db: Connection = Depends(get_db)):
    """实验室一次提交仪器、证书与父子溯源边；全部按唯一编码 upsert。"""
    try:
        return crud.submit(db, body.instruments, body.certificates, body.edges)
    except crud.ValidationError as exc:
        # 在路由层转换，以便 422 响应体带中文原因
        from fastapi import HTTPException

        raise HTTPException(422, str(exc))


@router.post(
    "/preflight",
    response_model=PreflightResult,
    status_code=200,
    summary="批量提交预检（dry-run，不写库）",
)
def preflight_submission(
    body: SubmissionPreflightIn,
    db: Connection = Depends(get_readonly_db),
):
    """接收与 ``/api/submissions`` 相同的载荷，另加 ``strict_digest`` 选项。

    在独立只读事务中按提交顺序（仪器 → 证书 → 溯源边）执行引用、区间、
    自环与签名摘要校验，**绝不写入数据库**。返回稳定排序的错误/警告
    （路径、错误码、细节）以及若正式提交将 upsert 的行数；
    ``strict_digest=true`` 时显式摘要不匹配记为 ERROR，否则记为 WARNING。
    """
    return preflight.precheck_submission(
        db,
        body.instruments,
        body.certificates,
        body.edges,
        body.strict_digest,
    )
