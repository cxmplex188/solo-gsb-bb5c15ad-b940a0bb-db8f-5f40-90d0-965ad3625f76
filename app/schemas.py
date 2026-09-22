"""请求/响应 Pydantic 模型。时间均按 UTC 解释。"""
from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from .utils import SUPPORTED_ALGS, compute_digest, fmt


# ---------- 输入 ----------

class InstrumentIn(BaseModel):
    code: str = Field(..., min_length=1, description="仪器唯一编码")
    name: str
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    serial_no: Optional[str] = None
    is_national_standard: bool = Field(
        False, description="是否为国家基准（溯源根节点）"
    )


class CertificateIn(BaseModel):
    id: str = Field(..., min_length=1)
    instrument_code: str
    cert_number: str
    issued_at: datetime
    valid_from: datetime
    valid_until: datetime
    uncertainty: float = Field(..., ge=0)
    unit: str = ""
    digest_alg: str = "sha256"
    signature_digest: Optional[str] = Field(
        None, description="证书签名摘要；留空则由服务端按规范化载荷重算"
    )
    signed_payload: Optional[str] = Field(
        None, description="被签名的原始载荷；留空则按证书字段规范化生成"
    )

    @field_validator("digest_alg")
    @classmethod
    def _check_alg(cls, v: str) -> str:
        v = v.lower()
        if v not in SUPPORTED_ALGS:
            raise ValueError(f"不支持的摘要算法: {v}")
        return v

    def normalized(self) -> dict:
        data = self.model_dump()
        data["issued_at"] = fmt(self.issued_at)
        data["valid_from"] = fmt(self.valid_from)
        data["valid_until"] = fmt(self.valid_until)
        data["digest_alg"] = self.digest_alg.lower()
        if not data["signed_payload"]:
            data["signed_payload"] = None
        if not data["signature_digest"]:
            data["signature_digest"] = None
        return data


class TraceEdgeIn(BaseModel):
    child_cert_id: str
    parent_cert_id: str
    valid_from: Optional[datetime] = Field(
        None, description="边有效起点；留空取子证书 valid_from"
    )
    valid_until: Optional[datetime] = Field(
        None, description="边有效终点；留空取子证书 valid_until"
    )
    note: str = ""


class SubmissionIn(BaseModel):
    """实验室一次提交：仪器 + 证书 + 父子溯源边（全部按编码 upsert）。"""
    instruments: List[InstrumentIn] = Field(default_factory=list)
    certificates: List[CertificateIn] = Field(default_factory=list)
    edges: List[TraceEdgeIn] = Field(default_factory=list)


class DeactivationIn(BaseModel):
    """证书停用影响评估请求（dry-run，不写入数据库）。"""
    at: Optional[datetime] = Field(
        None, description="查询时点（UTC）；留空取服务端当前时间"
    )
    certificate_ids: List[str] = Field(
        ..., min_length=1, description="待停用证书 id 列表（至少 1 张）"
    )
    instrument_codes: Optional[List[str]] = Field(
        None,
        description="仅评估指定仪器；留空则自动覆盖全部受影响仪器",
    )

    @field_validator("certificate_ids")
    @classmethod
    def _check_certificate_ids(cls, v: List[str]) -> List[str]:
        if any(not cid or not cid.strip() for cid in v):
            raise ValueError("证书 id 不能为空字符串")
        return v

    @field_validator("instrument_codes")
    @classmethod
    def _check_instrument_codes(cls, v):
        if v is not None:
            if len(v) == 0:
                raise ValueError("仪器编码筛选列表不能为空")
            if any(not c or not c.strip() for c in v):
                raise ValueError("仪器编码不能为空字符串")
        return v


# ---------- 输出 ----------

class InstrumentOut(InstrumentIn):
    created_at: datetime


class CertificateOut(BaseModel):
    id: str
    instrument_code: str
    cert_number: str
    issued_at: datetime
    valid_from: datetime
    valid_until: datetime
    uncertainty: float
    unit: str
    digest_alg: str
    signature_digest: str
    signed_payload: str
    created_at: datetime


class TraceEdgeOut(BaseModel):
    id: int
    child_cert_id: str
    parent_cert_id: str
    valid_from: datetime
    valid_until: datetime
    note: str


class SubmissionResult(BaseModel):
    instruments_upserted: List[str]
    certificates_upserted: List[str]
    edges_upserted: List[int]


class Conflict(BaseModel):
    code: str
    severity: str = Field(..., description="ERROR | WARN")
    node: Optional[str] = Field(None, description="冲突所在证书 id（若适用）")
    instrument: Optional[str] = Field(None, description="冲突所在仪器编码（若适用）")
    path_index: Optional[int] = Field(
        None, description="冲突所属候选链下标（节点级检查为 None）"
    )
    message: str
    detail: dict = Field(default_factory=dict)


class ChainNode(BaseModel):
    certificate_id: str
    cert_number: str
    instrument_code: str
    instrument_name: str
    is_national_standard: bool
    valid_from: datetime
    valid_until: datetime
    issued_at: datetime
    uncertainty: float
    unit: str


class ChainEdge(BaseModel):
    edge_id: int
    child_cert_id: str
    parent_cert_id: str
    valid_from: datetime
    valid_until: datetime
    note: str


class Chain(BaseModel):
    nodes: List[ChainNode]
    edges: List[ChainEdge]
    reaches_national_standard: bool
    cycle: bool
    errors: int = 0
    warnings: int = 0


class TraceReport(BaseModel):
    instrument: str
    at: datetime
    resolution: str = Field(
        ..., description="unique | ambiguous | broken（唯一/多候选/断裂）"
    )
    valid: bool = Field(..., description="唯一成链且无 ERROR 级问题时为 true")
    starts: List[str]
    active_chain: Optional[Chain] = None
    candidates: List[Chain] = Field(
        default_factory=list, description="到达国家基准的候选链（ambiguous 时 >=2）"
    )
    broken_chains: List[Chain] = Field(
        default_factory=list, description="未能到达国家基准的部分链"
    )
    conflicts: List[Conflict] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    statistics: dict = Field(default_factory=dict)


# ---------- 证书停用影响评估输出 ----------

class ChainSummary(BaseModel):
    role: str = Field(
        ...,
        description="链在评估中的角色：active=唯一有效链 | candidate=候选链 | broken=断裂分支",
    )
    certificate_ids: List[str]
    node_count: int
    reaches_national_standard: bool
    contains_disabled_certificate: bool
    disabled_certificates_on_path: List[str]
    errors: int = 0
    warnings: int = 0
    chain: Chain = Field(..., description="完整链（节点 + 边）摘要")


class TraceStateSnapshot(BaseModel):
    """停用前/后任一时刻的完整评估快照（与 TraceReport 同构）。"""
    resolution: str
    valid: bool
    starts: List[str]
    active_chain: Optional[Chain] = None
    candidates: List[Chain] = Field(default_factory=list)
    broken_chains: List[Chain] = Field(default_factory=list)
    conflicts: List[Conflict] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    statistics: dict = Field(default_factory=dict)


class InstrumentDeactivationResult(BaseModel):
    instrument: str
    impacted: bool = Field(..., description="停用前后评估结果是否发生变化")
    transition: str = Field(
        ...,
        description=(
            "状态迁移：unchanged | unique_to_broken | unique_to_ambiguous | "
            "ambiguous_to_unique | ambiguous_to_broken | broken_to_unique | "
            "broken_to_ambiguous"
        ),
    )
    before: TraceStateSnapshot = Field(..., description="停用前状态")
    after: TraceStateSnapshot = Field(..., description="停用后（模拟）状态")
    alternative_chain: Optional[ChainSummary] = Field(
        None, description="停用后的唯一有效替代链（若存在）"
    )
    alternative_chains: List[ChainSummary] = Field(
        default_factory=list,
        description="停用后仍存在的全部到基准候选链（多候选时 >=2）",
    )
    severed_chains: List[ChainSummary] = Field(
        default_factory=list,
        description="停用前存在、因途经被停用证书而被切断的链",
    )
    simulation_notes: List[Conflict] = Field(
        default_factory=list,
        description="模拟过程的可解释说明（severity=INFO，非真实数据冲突）",
    )


class DeactivationImpactReport(BaseModel):
    at: datetime
    dry_run: bool = Field(
        ..., description="恒为 true：模拟不写入数据库"
    )
    disabled_certificate_ids: List[str]
    unknown_certificate_ids: List[str] = Field(
        default_factory=list, description="不存在于库中的待停用证书 id"
    )
    instruments_filter: Optional[List[str]] = Field(
        None, description="请求指定的仪器筛选范围；未筛选为 null"
    )
    affected_owner_instruments: List[str] = Field(
        default_factory=list, description="被停用证书直接所属的仪器编码"
    )
    removed_edge_count: int = Field(
        ..., description="模拟中一并移除的溯源边数量"
    )
    affected_instrument_count: int
    evaluated_instrument_count: int
    transition_counts: dict = Field(
        default_factory=dict, description="按状态迁移类型计数"
    )
    results: List[InstrumentDeactivationResult]
    statistics: dict = Field(default_factory=dict)
