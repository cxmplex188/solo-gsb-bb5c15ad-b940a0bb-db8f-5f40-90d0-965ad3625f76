"""批量提交预检（POST /api/submissions/preflight）测试。"""
from datetime import datetime

from app.utils import canonical_payload, compute_digest, fmt

AT = "2026-06-15T00:00:00"


def _instruments():
    return [
        {"code": "G", "name": "国家基准", "is_national_standard": True},
        {"code": "R", "name": "参考标准器"},
        {"code": "W", "name": "工作仪器"},
    ]


def _certs():
    return [
        {"id": "CG", "instrument_code": "G", "cert_number": "G-1",
         "issued_at": "2024-01-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00",
         "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.0001, "unit": "mm"},
        {"id": "CR", "instrument_code": "R", "cert_number": "R-1",
         "issued_at": "2025-06-01T00:00:00",
         "valid_from": "2025-06-01T00:00:00",
         "valid_until": "2027-06-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        {"id": "CW", "instrument_code": "W", "cert_number": "W-1",
         "issued_at": "2026-01-01T00:00:00",
         "valid_from": "2026-01-01T00:00:00",
         "valid_until": "2027-01-01T00:00:00",
         "uncertainty": 0.01, "unit": "mm"},
    ]


def _edges():
    return [
        {"child_cert_id": "CW", "parent_cert_id": "CR"},
        {"child_cert_id": "CR", "parent_cert_id": "CG"},
    ]


def _payload(cert: dict) -> str:
    def _ts(v):
        if isinstance(v, datetime):
            return fmt(v)
        return v if v.endswith(("Z", "+00:00")) else v + "Z"

    return canonical_payload(
        {
            "id": cert["id"],
            "instrument_code": cert["instrument_code"],
            "cert_number": cert["cert_number"],
            "issued_at": _ts(cert["issued_at"]),
            "valid_from": _ts(cert["valid_from"]),
            "valid_until": _ts(cert["valid_until"]),
            "uncertainty": cert["uncertainty"],
            "unit": cert.get("unit", ""),
        }
    )


def _preflight(client, instruments=None, certs=None, edges=None,
               strict_digest=False):
    return client.post(
        "/api/submissions/preflight",
        json={
            "instruments": _instruments() if instruments is None else instruments,
            "certificates": _certs() if certs is None else certs,
            "edges": _edges() if edges is None else edges,
            "strict_digest": strict_digest,
        },
    )


def test_preflight_valid_payload_counts_inserted(client):
    r = _preflight(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["strict_digest"] is False
    assert body["ok"] is True
    assert body["error_count"] == 0
    assert body["warning_count"] == 0
    assert body["issues"] == []
    wu = body["would_upsert"]
    assert wu["instruments"] == {"inserted": 3, "updated": 0, "total": 3}
    assert wu["certificates"] == {"inserted": 3, "updated": 0, "total": 3}
    assert wu["edges"] == {"inserted": 2, "updated": 0, "total": 2}
    assert wu["total"] == {"inserted": 8, "updated": 0, "total": 8}


def test_preflight_never_writes_to_database(client):
    r = _preflight(client)
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # 预检后库仍为空
    assert client.get("/api/instruments").json() == []
    # 即使载荷含错误，也不写库
    bad = _preflight(
        client,
        instruments=[],
        certs=[{"id": "CX", "instrument_code": "NOPE", "cert_number": "X",
                "issued_at": AT, "valid_from": AT,
                "valid_until": "2027-01-01T00:00:00",
                "uncertainty": 1.0, "unit": "mm"}],
        edges=[],
    )
    assert bad.json()["ok"] is False
    assert client.get("/api/instruments").json() == []
    assert client.get("/api/certificates").json() == []


def test_preflight_reports_missing_references_and_self_loop(client):
    certs = _certs() + [
        {"id": "CX", "instrument_code": "GHOST", "cert_number": "X-1",
         "issued_at": AT, "valid_from": AT,
         "valid_until": "2027-01-01T00:00:00",
         "uncertainty": 1.0, "unit": "mm"},
    ]
    edges = _edges() + [
        {"child_cert_id": "CW", "parent_cert_id": "CW"},       # 自环
        {"child_cert_id": "CW", "parent_cert_id": "NOPE"},     # 父证书缺失
    ]
    body = _preflight(client, certs=certs, edges=edges).json()
    codes = {i["code"] for i in body["issues"]}
    assert "CERT_INSTRUMENT_NOT_FOUND" in codes
    assert "EDGE_SELF_LOOP" in codes
    assert "EDGE_PARENT_CERT_NOT_FOUND" in codes
    assert body["ok"] is False
    assert body["error_count"] == 3


def test_preflight_certificate_and_edge_interval_errors(client):
    certs = _certs()
    next(c for c in certs if c["id"] == "CW").update(
        valid_from="2028-01-01T00:00:00",
        valid_until="2027-01-01T00:00:00",
    )
    edges = [
        # 边区间倒置
        {"child_cert_id": "CW", "parent_cert_id": "CR",
         "valid_from": "2027-06-01T00:00:00",
         "valid_until": "2026-06-01T00:00:00"},
        # 边区间与子证书区间完全不相交（CR 区间正常）
        {"child_cert_id": "CR", "parent_cert_id": "CG",
         "valid_from": "2029-01-01T00:00:00",
         "valid_until": "2030-01-01T00:00:00"},
    ]
    body = _preflight(client, certs=certs, edges=edges).json()
    codes = {i["code"] for i in body["issues"]}
    assert "CERTIFICATE_INVALID_INTERVAL" in codes
    assert "EDGE_INVALID_INTERVAL" in codes
    assert "EDGE_INTERVAL_DISJOINT" in codes
    assert body["ok"] is False
    # 每条 issue 都带定位路径
    assert all(i["path"] for i in body["issues"])


def test_preflight_digest_mismatch_warning_by_default(client):
    certs = _certs()
    next(c for c in certs if c["id"] == "CW")["signature_digest"] = "0" * 64
    body = _preflight(client, certs=certs).json()
    mismatch = [i for i in body["issues"]
                if i["code"] == "SIGNATURE_DIGEST_MISMATCH"]
    assert len(mismatch) == 1
    assert mismatch[0]["severity"] == "WARN"
    assert mismatch[0]["path"] == "certificates[2].id=CW"
    assert mismatch[0]["detail"]["submitted"] == "0" * 64
    assert mismatch[0]["detail"]["recomputed"] != "0" * 64
    # 非严格模式：仅警告，整体仍 ok
    assert body["ok"] is True
    assert body["warning_count"] == 1
    assert body["error_count"] == 0


def test_preflight_digest_mismatch_error_in_strict_mode(client):
    certs = _certs()
    next(c for c in certs if c["id"] == "CW")["signature_digest"] = "0" * 64
    body = _preflight(client, certs=certs, strict_digest=True).json()
    mismatch = [i for i in body["issues"]
                if i["code"] == "SIGNATURE_DIGEST_MISMATCH"]
    assert len(mismatch) == 1
    assert mismatch[0]["severity"] == "ERROR"
    assert body["ok"] is False
    assert body["error_count"] == 1
    assert body["warning_count"] == 0
    assert body["strict_digest"] is True


def test_preflight_explicit_correct_digest_passes(client):
    certs = _certs()
    cw = next(c for c in certs if c["id"] == "CW")
    cw["signature_digest"] = compute_digest(_payload(cw), "sha256")
    body = _preflight(client, certs=certs, strict_digest=True).json()
    assert body["ok"] is True
    assert body["issues"] == []


def test_preflight_missing_digest_is_server_recomputed_no_issue(client):
    # 全部证书均未给 signature_digest：服务端会重算，任何模式都不应报问题
    body = _preflight(client, strict_digest=True).json()
    assert body["error_count"] == 0
    assert body["warning_count"] == 0


def test_preflight_issues_are_stably_sorted_by_path(client):
    # 故意让边（段排序最靠后）与证书（中间段）同时出错，验证路径排序
    certs = _certs()
    next(c for c in certs if c["id"] == "CW").update(
        valid_from="2028-01-01T00:00:00",
        valid_until="2027-01-01T00:00:00",
    )
    edges = _edges() + [
        {"child_cert_id": "CW", "parent_cert_id": "CW"},
    ]
    body = _preflight(client, certs=certs, edges=edges).json()
    paths = [i["path"] for i in body["issues"]]
    assert paths == sorted(
        paths,
        key=lambda p: (
            {"instruments": 0, "certificates": 1, "edges": 2}[
                p.split("[", 1)[0]
            ],
            int(p.split("[", 1)[1].split("]", 1)[0]),
        ),
    )
    # 重复请求结果完全一致（稳定）
    body2 = _preflight(client, certs=certs, edges=edges).json()
    assert [i["path"] for i in body2["issues"]] == paths


def test_preflight_counts_updated_after_real_submission(client):
    assert _preflight(client).json()["would_upsert"]["total"]["inserted"] == 8
    r = client.post(
        "/api/submissions",
        json={"instruments": _instruments(),
              "certificates": _certs(), "edges": _edges()},
    )
    assert r.status_code == 201

    # 同样的载荷再预检：全部计为 updated，重复项只算一行
    body = _preflight(
        client,
        instruments=_instruments() + _instruments()[:1],  # 含一个重复仪器
        edges=_edges() + [_edges()[0]],                   # 含一条重复边
    ).json()
    wu = body["would_upsert"]
    assert wu["instruments"] == {"inserted": 0, "updated": 3, "total": 3}
    assert wu["certificates"] == {"inserted": 0, "updated": 3, "total": 3}
    assert wu["edges"] == {"inserted": 0, "updated": 2, "total": 2}
    assert wu["total"] == {"inserted": 0, "updated": 8, "total": 8}


def test_preflight_sees_in_batch_references(client):
    # 仪器/证书都在同一批次内按顺序提交，引用应能被识别（即使库为空）
    body = _preflight(client).json()
    assert not any(
        i["code"] in {"CERT_INSTRUMENT_NOT_FOUND",
                      "EDGE_CHILD_CERT_NOT_FOUND",
                      "EDGE_PARENT_CERT_NOT_FOUND"}
        for i in body["issues"]
    )
