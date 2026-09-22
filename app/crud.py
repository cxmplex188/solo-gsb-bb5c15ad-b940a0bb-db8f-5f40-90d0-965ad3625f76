"""数据访问与写入期校验。"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from .schemas import CertificateIn, InstrumentIn, TraceEdgeIn
from .utils import (
    canonical_payload,
    compute_digest,
    fmt,
    to_utc_naive,
    utcnow_str,
)


class ValidationError(Exception):
    """输入数据业务校验失败（路由层映射为 HTTP 422）。"""


# ---------- 仪器 ----------

def upsert_instrument(conn: sqlite3.Connection, inst: InstrumentIn) -> str:
    conn.execute(
        """
        INSERT INTO instruments(code, name, manufacturer, model, serial_no,
                                is_national_standard, created_at)
        VALUES (:code, :name, :manufacturer, :model, :serial_no,
                :is_national_standard, :created_at)
        ON CONFLICT(code) DO UPDATE SET
            name=excluded.name,
            manufacturer=excluded.manufacturer,
            model=excluded.model,
            serial_no=excluded.serial_no,
            is_national_standard=excluded.is_national_standard
        """,
        {
            "code": inst.code,
            "name": inst.name,
            "manufacturer": inst.manufacturer,
            "model": inst.model,
            "serial_no": inst.serial_no,
            "is_national_standard": int(inst.is_national_standard),
            "created_at": utcnow_str(),
        },
    )
    return inst.code


def get_instrument(conn: sqlite3.Connection, code: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM instruments WHERE code = ?", (code,)
    ).fetchone()


def list_instruments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM instruments ORDER BY code").fetchall()


# ---------- 证书 ----------

def upsert_certificate(conn: sqlite3.Connection, cert: CertificateIn) -> str:
    inst = get_instrument(conn, cert.instrument_code)
    if inst is None:
        raise ValidationError(f"仪器不存在: {cert.instrument_code}（请先提交仪器）")

    d = cert.normalized()
    if not d["valid_from"] <= d["valid_until"]:
        raise ValidationError(
            f"证书 {cert.id} 的 valid_from 必须不晚于 valid_until"
        )

    payload = d["signed_payload"]
    if payload is None:
        payload = canonical_payload(d)
    digest = d["signature_digest"] or compute_digest(payload, d["digest_alg"])

    conn.execute(
        """
        INSERT INTO certificates(id, instrument_code, cert_number, issued_at,
                                 valid_from, valid_until, uncertainty, unit,
                                 digest_alg, signature_digest, signed_payload,
                                 created_at)
        VALUES (:id, :instrument_code, :cert_number, :issued_at,
                :valid_from, :valid_until, :uncertainty, :unit,
                :digest_alg, :signature_digest, :signed_payload, :created_at)
        ON CONFLICT(id) DO UPDATE SET
            instrument_code=excluded.instrument_code,
            cert_number=excluded.cert_number,
            issued_at=excluded.issued_at,
            valid_from=excluded.valid_from,
            valid_until=excluded.valid_until,
            uncertainty=excluded.uncertainty,
            unit=excluded.unit,
            digest_alg=excluded.digest_alg,
            signature_digest=excluded.signature_digest,
            signed_payload=excluded.signed_payload
        """,
        {
            **d,
            "signed_payload": payload,
            "signature_digest": digest,
            "created_at": utcnow_str(),
        },
    )
    return cert.id


def get_certificate(conn: sqlite3.Connection, cert_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM certificates WHERE id = ?", (cert_id,)
    ).fetchone()


def list_certificates(
    conn: sqlite3.Connection, instrument_code: Optional[str] = None
) -> list[sqlite3.Row]:
    if instrument_code is not None:
        return conn.execute(
            "SELECT * FROM certificates WHERE instrument_code = ? ORDER BY valid_from",
            (instrument_code,),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM certificates ORDER BY instrument_code, valid_from"
    ).fetchall()


# ---------- 溯源边 ----------

def upsert_edge(conn: sqlite3.Connection, edge: TraceEdgeIn) -> int:
    child = get_certificate(conn, edge.child_cert_id)
    if child is None:
        raise ValidationError(f"子证书不存在: {edge.child_cert_id}")
    parent = get_certificate(conn, edge.parent_cert_id)
    if parent is None:
        raise ValidationError(f"父证书不存在: {edge.parent_cert_id}")
    if edge.child_cert_id == edge.parent_cert_id:
        raise ValidationError("溯源边不能指向证书自身（自环）")

    vf = to_utc_naive(edge.valid_from) if edge.valid_from else None
    vu = to_utc_naive(edge.valid_until) if edge.valid_until else None
    # 未显式给出时，边的有效区间继承子证书区间
    vf_s = fmt(vf) if vf is not None else child["valid_from"]
    vu_s = fmt(vu) if vu is not None else child["valid_until"]
    if vf_s > vu_s:
        raise ValidationError(
            f"溯源边({edge.child_cert_id} -> {edge.parent_cert_id}) "
            "的 valid_from 必须不晚于 valid_until"
        )
    if vu_s < child["valid_from"] or vf_s > child["valid_until"]:
        raise ValidationError(
            f"溯源边({edge.child_cert_id} -> {edge.parent_cert_id}) "
            "有效区间与子证书区间完全不相交"
        )

    cur = conn.execute(
        """
        INSERT INTO trace_edges(child_cert_id, parent_cert_id, valid_from,
                                valid_until, note, created_at)
        VALUES (:child, :parent, :vf, :vu, :note, :created_at)
        ON CONFLICT(child_cert_id, parent_cert_id) DO UPDATE SET
            valid_from=excluded.valid_from,
            valid_until=excluded.valid_until,
            note=excluded.note
        """,
        {
            "child": edge.child_cert_id,
            "parent": edge.parent_cert_id,
            "vf": vf_s,
            "vu": vu_s,
            "note": edge.note,
            "created_at": utcnow_str(),
        },
    )
    row = conn.execute(
        "SELECT id FROM trace_edges WHERE child_cert_id=? AND parent_cert_id=?",
        (edge.child_cert_id, edge.parent_cert_id),
    ).fetchone()
    return int(row["id"])


def list_edges(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM trace_edges ORDER BY child_cert_id, parent_cert_id"
    ).fetchall()


# ---------- 批量提交 ----------

def submit(
    conn: sqlite3.Connection,
    instruments: list[InstrumentIn],
    certificates: list[CertificateIn],
    edges: list[TraceEdgeIn],
) -> dict[str, Any]:
    inst_ids = [upsert_instrument(conn, i) for i in instruments]
    cert_ids = [upsert_certificate(conn, c) for c in certificates]
    edge_ids = [upsert_edge(conn, e) for e in edges]
    return {
        "instruments_upserted": inst_ids,
        "certificates_upserted": cert_ids,
        "edges_upserted": edge_ids,
    }


def reset(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        DELETE FROM trace_edges;
        DELETE FROM certificates;
        DELETE FROM instruments;
        DELETE FROM sqlite_sequence WHERE name='trace_edges';
        """
    )
