"""批量提交预检（dry-run）：在独立只读事务中复放提交，绝不写库。

与 :func:`app.crud.submit` 的写入顺序保持一致：先仪器、再证书、最后溯源边，
同段内按请求列表顺序处理，重复编码的后项覆盖前项（与 upsert 语义相同）。

校验项对应写入期校验：

- 引用：证书 -> 仪器、溯源边 -> 子/父证书；
- 区间：证书与溯源边自身区间合法、边区间需与子证书区间相交；
- 自环：溯源边不得指向证书自身；
- 摘要：仅检查显式提交的 ``signature_digest``（留空由服务端重算，
  不存在不匹配），``strict_digest=true`` 时记为 ERROR，否则记为 WARNING。
"""
from __future__ import annotations

import sqlite3
from typing import Any

from .schemas import CertificateIn, InstrumentIn, TraceEdgeIn
from .utils import canonical_payload, compute_digest, fmt, to_utc_naive

# 问题路径排序权重：instruments < certificates < edges，保证稳定排序
_SECTION_RANK = {"instruments": 0, "certificates": 1, "edges": 2}


def _issue(path: str, code: str, severity: str, message: str, **detail: Any) -> dict:
    return {
        "path": path,
        "code": code,
        "severity": severity,
        "message": message,
        "detail": detail,
    }


def _normalized_cert(cert: CertificateIn) -> dict[str, Any]:
    """与 crud.upsert_certificate 相同的字段规范化，仅用于内存复放。"""
    d = cert.normalized()
    payload = d["signed_payload"]
    if payload is None:
        payload = canonical_payload(d)
    d["signed_payload"] = payload
    d["interval_valid"] = d["valid_from"] <= d["valid_until"]
    return d


def _instrument_exists(view: dict[str, Any], code: str) -> bool:
    return code in view["instruments"]


def _check_certificate(
    cert: CertificateIn,
    d: dict[str, Any],
    idx: int,
    view: dict[str, Any],
    strict_digest: bool,
) -> list[dict]:
    """证书级校验：仪器引用、自身区间、显式摘要复核。"""
    issues: list[dict] = []
    path = f"certificates[{idx}].id={cert.id}"

    if not _instrument_exists(view, cert.instrument_code):
        issues.append(
            _issue(
                path,
                "CERT_INSTRUMENT_NOT_FOUND",
                "ERROR",
                f"仪器不存在: {cert.instrument_code}（请先提交仪器）",
                instrument_code=cert.instrument_code,
            )
        )

    if not d["interval_valid"]:
        issues.append(
            _issue(
                path,
                "CERTIFICATE_INVALID_INTERVAL",
                "ERROR",
                f"证书 {cert.id} 的 valid_from 必须不晚于 valid_until",
                valid_from=d["valid_from"],
                valid_until=d["valid_until"],
            )
        )

    # 仅对显式提交的摘要复核；留空时服务端会重算，无“不匹配”可言
    if cert.signature_digest:
        expected = compute_digest(d["signed_payload"], d["digest_alg"])
        if expected != cert.signature_digest:
            issues.append(
                _issue(
                    path,
                    "SIGNATURE_DIGEST_MISMATCH",
                    "ERROR" if strict_digest else "WARN",
                    f"证书 {cert.id}（{cert.cert_number}）签名摘要校验失败："
                    f"提交 {cert.signature_digest}，重算 {expected}",
                    digest_alg=d["digest_alg"],
                    submitted=cert.signature_digest,
                    recomputed=expected,
                )
            )
    return issues


def _check_edge(
    edge: TraceEdgeIn,
    idx: int,
    view: dict[str, Any],
) -> list[dict]:
    """溯源边级校验：引用、自环、自身区间、与子证书区间相交。"""
    issues: list[dict] = []
    path = (
        f"edges[{idx}].{{child_cert_id={edge.child_cert_id}, "
        f"parent_cert_id={edge.parent_cert_id}}}"
    )

    child = view["certificates"].get(edge.child_cert_id)
    parent = view["certificates"].get(edge.parent_cert_id)

    if child is None:
        issues.append(
            _issue(
                path,
                "EDGE_CHILD_CERT_NOT_FOUND",
                "ERROR",
                f"子证书不存在: {edge.child_cert_id}",
                child_cert_id=edge.child_cert_id,
            )
        )
    if parent is None:
        issues.append(
            _issue(
                path,
                "EDGE_PARENT_CERT_NOT_FOUND",
                "ERROR",
                f"父证书不存在: {edge.parent_cert_id}",
                parent_cert_id=edge.parent_cert_id,
            )
        )

    if edge.child_cert_id == edge.parent_cert_id:
        issues.append(
            _issue(
                path,
                "EDGE_SELF_LOOP",
                "ERROR",
                "溯源边不能指向证书自身（自环）",
                child_cert_id=edge.child_cert_id,
                parent_cert_id=edge.parent_cert_id,
            )
        )

    # 区间检查需要可解析的子证书；引用缺失/自环之外的区间问题在此检查
    if child is not None:
        vf_s, vu_s, edge_interval_valid = _edge_interval(edge, child)
        if not edge_interval_valid:
            issues.append(
                _issue(
                    path,
                    "EDGE_INVALID_INTERVAL",
                    "ERROR",
                    f"溯源边({edge.child_cert_id} -> {edge.parent_cert_id}) "
                    "的 valid_from 必须不晚于 valid_until",
                    valid_from=vf_s,
                    valid_until=vu_s,
                )
            )
        elif child["interval_valid"] and (
            vu_s < child["valid_from"] or vf_s > child["valid_until"]
        ):
            issues.append(
                _issue(
                    path,
                    "EDGE_INTERVAL_DISJOINT",
                    "ERROR",
                    f"溯源边({edge.child_cert_id} -> {edge.parent_cert_id}) "
                    "有效区间与子证书区间完全不相交",
                    edge_valid_from=vf_s,
                    edge_valid_until=vu_s,
                    child_valid_from=child["valid_from"],
                    child_valid_until=child["valid_until"],
                )
            )
    return issues


def _edge_interval(
    edge: TraceEdgeIn, child: dict[str, Any]
) -> tuple[str, str, bool]:
    """与 crud.upsert_edge 相同的区间继承规则。"""
    vf = to_utc_naive(edge.valid_from) if edge.valid_from else None
    vu = to_utc_naive(edge.valid_until) if edge.valid_until else None
    vf_s = fmt(vf) if vf is not None else child["valid_from"]
    vu_s = fmt(vu) if vu is not None else child["valid_until"]
    return vf_s, vu_s, vf_s <= vu_s


def precheck_submission(
    conn: sqlite3.Connection,
    instruments: list[InstrumentIn],
    certificates: list[CertificateIn],
    edges: list[TraceEdgeIn],
    strict_digest: bool = False,
) -> dict[str, Any]:
    """在只读连接上按提交顺序复放批量提交，返回问题列表与将 upsert 的数量。

    本函数只执行 SELECT；调用方连接应处于 query_only/只读事务中。
    """
    # 已在库中的行 -> 将被更新；同批次重复编码只计一次
    existing_insts: set[str] = {
        r["code"] for r in conn.execute("SELECT code FROM instruments")
    }
    existing_certs: set[str] = {
        r["id"] for r in conn.execute("SELECT id FROM certificates")
    }
    existing_edges: set[tuple[str, str]] = {
        (r["child_cert_id"], r["parent_cert_id"])
        for r in conn.execute(
            "SELECT child_cert_id, parent_cert_id FROM trace_edges"
        )
    }

    # 内存合并视图（库内现状 + 本次提交后项覆盖前项）
    view: dict[str, Any] = {
        "instruments": {code: None for code in existing_insts},
        "certificates": {
            r["id"]: {
                "valid_from": r["valid_from"],
                "valid_until": r["valid_until"],
                "interval_valid": r["valid_from"] <= r["valid_until"],
            }
            for r in conn.execute(
                "SELECT id, valid_from, valid_until FROM certificates"
            )
        },
    }

    issues: list[dict] = []

    # 1) 仪器：仪器字段级校验已由 Pydantic 完成，此处仅合并视图
    for inst in instruments:
        view["instruments"][inst.code] = inst

    # 2) 证书：引用仪器、自身区间、显式摘要
    for idx, cert in enumerate(certificates):
        d = _normalized_cert(cert)
        issues.extend(
            _check_certificate(cert, d, idx, view, strict_digest)
        )
        view["certificates"][cert.id] = d

    # 3) 溯源边：引用证书、自环、区间
    for idx, edge in enumerate(edges):
        issues.extend(_check_edge(edge, idx, view))

    # 将 upsert 的唯一行数（同批次重复项与 upsert 一样只影响一行）
    inst_keys = [inst.code for inst in instruments]
    cert_keys = [cert.id for cert in certificates]
    edge_keys = [
        (edge.child_cert_id, edge.parent_cert_id) for edge in edges
    ]

    def _counts(keys: list, existing: set) -> dict[str, int]:
        unique = set(keys)
        inserted = len(unique - existing)
        updated = len(unique & existing)
        return {
            "inserted": inserted,
            "updated": updated,
            "total": inserted + updated,
        }

    would_upsert = {
        "instruments": _counts(inst_keys, existing_insts),
        "certificates": _counts(cert_keys, existing_certs),
        "edges": _counts(edge_keys, existing_edges),
    }
    total = {
        "inserted": sum(v["inserted"] for v in would_upsert.values()),
        "updated": sum(v["updated"] for v in would_upsert.values()),
    }
    total["total"] = total["inserted"] + total["updated"]
    would_upsert["total"] = total

    # 稳定排序：段（仪器/证书/边）→ 提交下标 → 代码
    def _sort_key(item: dict) -> tuple:
        path = item["path"]
        section = path.split("[", 1)[0]
        idx = int(path.split("[", 1)[1].split("]", 1)[0])
        return (_SECTION_RANK.get(section, 99), idx, item["code"])

    issues.sort(key=_sort_key)

    errors = sum(1 for i in issues if i["severity"] == "ERROR")
    warnings = sum(1 for i in issues if i["severity"] == "WARN")
    return {
        "dry_run": True,
        "strict_digest": strict_digest,
        "ok": errors == 0,
        "error_count": errors,
        "warning_count": warnings,
        "issues": issues,
        "would_upsert": would_upsert,
    }
