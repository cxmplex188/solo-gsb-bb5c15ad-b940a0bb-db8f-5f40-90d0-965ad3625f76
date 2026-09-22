from fastapi import APIRouter, Depends
from sqlite3 import Connection

from .. import crud
from ..database import get_db
from ..schemas import SubmissionIn, SubmissionResult

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
