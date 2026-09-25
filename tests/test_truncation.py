"""候选链枚举达到上限（500 条）截断时的“结果未定”语义测试。

构造 23×23 全连接两级分支图：CW -> CP00..CP22 -> CQ00..CQ22 -> CG，
共 529 条候选链，超过枚举上限 500（截断后样本为 DFS 顺序先得到的
前 500 条）。核心断言：

* 样本中合格链 >= 2 条 —— 仍可判 ``ambiguous``（截断附 WARN）；
* 样本中合格链不足两条 —— 必须判 ``indeterminate``（结果未定），
  不得把已枚举样本误报为 ``unique`` 或 ``broken``，且不提供有效主链；
* 共同溯源核查、测量相容性评估与预约链路门禁均以专用原因码
  ``TOO_MANY_CANDIDATES`` 拒绝“结果未定”的报告。
"""
from app.engine import MAX_PATHS

AT = "2026-06-15T00:00:00"
N = 23  # 23 x 23 = 529 > MAX_PATHS(500)


def _wide_graph(*, p_ok=None, q_ok=None, q_active=True):
    """构造 529 条候选链的批量提交载荷。

    ``p_ok`` / ``q_ok``：不确定度合规（不沿链收缩）的父级/祖级证书下标，
    缺省全部合规；不合规证书不确定度取 0.2（大于其下级，触发
    ``UNCERTAINTY_REGRESSION`` 使该分支校验失败）。
    ``q_active=False`` 时全部祖级证书在查询时点前到期（``PARENT_CERT_NOT_ACTIVE``）。
    """
    p_ok = set(range(N)) if p_ok is None else set(p_ok)
    q_ok = set(range(N)) if q_ok is None else set(q_ok)

    instruments = [
        {"code": "G", "name": "国家基准", "is_national_standard": True},
        {"code": "W", "name": "工作仪器"},
    ]
    certs = [
        {"id": "CG", "instrument_code": "G", "cert_number": "G-1",
         "issued_at": "2024-06-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00",
         "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.001, "unit": "mm"},
        {"id": "CW", "instrument_code": "W", "cert_number": "W-1",
         "issued_at": "2024-06-01T00:00:00",
         "valid_from": "2024-01-01T00:00:00",
         "valid_until": "2030-01-01T00:00:00",
         "uncertainty": 0.1, "unit": "mm"},
    ]
    edges = []
    for i in range(N):
        instruments.append({"code": f"P{i:02d}", "name": f"参考标准{i}"})
        certs.append({
            "id": f"CP{i:02d}", "instrument_code": f"P{i:02d}",
            "cert_number": f"P-{i}",
            "issued_at": "2024-06-01T00:00:00",
            "valid_from": "2024-01-01T00:00:00",
            "valid_until": "2030-01-01T00:00:00",
            "uncertainty": 0.05 if i in p_ok else 0.2,
            "unit": "mm",
        })
        edges.append({"child_cert_id": "CW", "parent_cert_id": f"CP{i:02d}"})
    for j in range(N):
        instruments.append({"code": f"Q{j:02d}", "name": f"上级标准{j}"})
        certs.append({
            "id": f"CQ{j:02d}", "instrument_code": f"Q{j:02d}",
            "cert_number": f"Q-{j}",
            "issued_at": "2024-06-01T00:00:00",
            "valid_from": "2024-01-01T00:00:00",
            "valid_until": (
                "2030-01-01T00:00:00" if q_active else "2025-01-01T00:00:00"
            ),
            "uncertainty": 0.01 if j in q_ok else 0.2,
            "unit": "mm",
        })
        edges.append({"child_cert_id": f"CQ{j:02d}", "parent_cert_id": "CG"})
    for i in range(N):
        for j in range(N):
            edges.append(
                {"child_cert_id": f"CP{i:02d}", "parent_cert_id": f"CQ{j:02d}"}
            )
    return {"instruments": instruments, "certificates": certs, "edges": edges}


def _submit(client, payload):
    r = client.post("/api/submissions", json=payload)
    assert r.status_code == 201, r.text
    return r


def _report(client, instrument="W", at=AT):
    return client.get(f"/api/reports/{instrument}?at={at}").json()


def test_truncated_ambiguous_still_decidable(client):
    """截断但样本中已有 500 条合格链：多候选结论成立（截断仅 WARN）。"""
    _submit(client, _wide_graph())

    report = _report(client)
    assert report["resolution"] == "ambiguous"
    assert report["valid"] is False
    assert report["enumeration_truncated"] is True
    assert report["active_chain"] is None
    assert len(report["candidates"]) == MAX_PATHS

    stats = report["statistics"]
    assert stats["paths_enumerated"] == MAX_PATHS
    assert stats["max_paths"] == MAX_PATHS
    assert stats["enumeration_truncated"] is True
    assert stats["valid_root_chains"] == MAX_PATHS

    assert any(c["code"] == "MULTIPLE_VALID_CHAINS" for c in report["conflicts"])
    tmc = next(c for c in report["conflicts"] if c["code"] == "TOO_MANY_CANDIDATES")
    assert tmc["severity"] == "WARN"  # 已见两条以上合格链，多候选结论不受影响
    assert tmc["detail"]["max_paths"] == MAX_PATHS


def test_truncated_zero_valid_chains_is_indeterminate_not_broken(client):
    """截断且样本中 0 条合格链：结果未定，不得误报为断裂。"""
    _submit(client, _wide_graph(q_active=False))  # 全部样本链在祖级断档

    report = _report(client)
    assert report["resolution"] == "indeterminate"
    assert report["valid"] is False
    assert report["enumeration_truncated"] is True
    assert report["active_chain"] is None  # 不提供有效主链

    stats = report["statistics"]
    assert stats["paths_enumerated"] >= MAX_PATHS
    assert stats["enumeration_truncated"] is True
    assert stats["root_reaching_chains"] == 0
    assert stats["valid_root_chains"] == 0

    codes = {c["code"] for c in report["conflicts"]}
    # 保留样本链与截断说明，但不得下“断裂”定论
    assert "TOO_MANY_CANDIDATES" in codes
    assert "CHAIN_BROKEN" not in codes
    assert "ALL_CHAINS_FAILED_CHECKS" not in codes
    tmc = next(c for c in report["conflicts"] if c["code"] == "TOO_MANY_CANDIDATES")
    assert tmc["severity"] == "ERROR"
    assert tmc["detail"]["valid_root_chains_seen"] == 0
    # 样本链仍完整保留在报告中
    assert report["candidates"] == []
    assert len(report["broken_chains"]) == stats["paths_enumerated"]


def test_truncated_single_valid_chain_is_indeterminate_not_unique(client):
    """截断且样本中恰 1 条合格链：结果未定，不得误报为唯一链。"""
    # 仅 CP00 -> CQ00 一条链校验通过，其余 528 条均沿链收缩失败
    _submit(client, _wide_graph(p_ok={0}, q_ok={0}))

    report = _report(client)
    assert report["resolution"] == "indeterminate"
    assert report["valid"] is False
    assert report["enumeration_truncated"] is True
    assert report["active_chain"] is None  # 样本中的合格链不得升任主链

    stats = report["statistics"]
    assert stats["paths_enumerated"] == MAX_PATHS
    assert stats["valid_root_chains"] == 1

    codes = {c["code"] for c in report["conflicts"]}
    assert "TOO_MANY_CANDIDATES" in codes
    assert "MULTIPLE_VALID_CHAINS" not in codes
    tmc = next(c for c in report["conflicts"] if c["code"] == "TOO_MANY_CANDIDATES")
    assert tmc["severity"] == "ERROR"
    assert tmc["detail"]["valid_root_chains_seen"] == 1

    # 样本中唯一合格链作为样本保留在 candidates，但不构成结论
    assert len(report["candidates"]) == 1
    sample = report["candidates"][0]
    assert [n["certificate_id"] for n in sample["nodes"]] == [
        "CW", "CP00", "CQ00", "CG",
    ]
    assert sample["reaches_national_standard"] is True
    assert sample["errors"] == 0


def _submit_unique_chain_w2(client):
    """追加一台链唯一且校验通过的仪器 W2（CW2 -> CQ00 -> CG）。"""
    payload = {
        "instruments": [{"code": "W2", "name": "第二工作仪器"}],
        "certificates": [
            {"id": "CW2", "instrument_code": "W2", "cert_number": "W2-1",
             "issued_at": "2024-06-01T00:00:00",
             "valid_from": "2024-01-01T00:00:00",
             "valid_until": "2030-01-01T00:00:00",
             "uncertainty": 0.1, "unit": "mm"},
        ],
        "edges": [{"child_cert_id": "CW2", "parent_cert_id": "CQ00"}],
    }
    r = client.post("/api/submissions", json=payload)
    assert r.status_code == 201, r.text


def test_gates_reject_indeterminate_with_dedicated_reason_code(client):
    """共同溯源/测量相容性/空档查询/预约确认均以 TOO_MANY_CANDIDATES 拒绝。"""
    _submit(client, _wide_graph(p_ok={0}, q_ok={0}))
    _submit_unique_chain_w2(client)

    # ① 批次共同溯源核查：W 结果未定，不得参与；W2 正常参与
    r = client.post(
        "/api/assessments/common-traceability",
        json={"at": AT, "instrument_codes": ["W", "W2"]},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["participants"] == ["W2"]
    np_w = next(n for n in body["nonparticipants"] if n["instrument"] == "W")
    assert np_w["reason_code"] == "TOO_MANY_CANDIDATES"
    assert np_w["resolution"] == "indeterminate"
    assert body["common_certificate_found"] is False
    assert body["reason_code"] == "INSUFFICIENT_PARTICIPANTS"

    # ② 测量相容性评估：W 的读数不得参与 En 计算
    r = client.post(
        "/api/assessments/measurement-compatibility",
        json={
            "at": AT,
            "target_unit": "mm",
            "readings": [
                {"instrument_code": "W", "value": 10.0},
                {"instrument_code": "W2", "value": 10.001},
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["participants"] == ["W2"]
    np_w = next(n for n in body["nonparticipants"] if n["instrument"] == "W")
    assert np_w["reason_code"] == "TOO_MANY_CANDIDATES"
    assert body["pairs"] == []

    # ③ 校准工位空档查询：链不可用，细分原因码 TOO_MANY_CANDIDATES
    r = client.get(
        "/api/calibration-slots",
        params={
            "instrument": "W",
            "start": "2026-06-20T00:00:00Z",
            "end": "2026-06-21T00:00:00Z",
            "duration_seconds": 3600,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["traceability_available"] is False
    assert body["slot"] is None
    assert body["reason_code"] == "TRACEABILITY_CHAIN_UNAVAILABLE"
    assert body["chain_reason_code"] == "TOO_MANY_CANDIDATES"
    assert body["trace_report"]["resolution"] == "indeterminate"

    # ④ 确认预约：409，绝不把结果未定的样本链当作预约依据
    r = client.post(
        "/api/calibration-bookings",
        json={
            "instrument_code": "W",
            "start": "2026-06-20T09:00:00Z",
            "duration_seconds": 3600,
        },
    )
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail["code"] == "TRACEABILITY_CHAIN_UNAVAILABLE"
    assert detail["detail"]["reason_code"] == "TOO_MANY_CANDIDATES"
    # 未写入任何预约
    assert client.get("/api/calibration-bookings").json() == []


def test_inspection_marks_booking_invalid_when_chain_becomes_indeterminate(client):
    """预约确认后溯源图变为结果未定：巡标记 invalid（TOO_MANY_CANDIDATES）。"""
    # 先以唯一链确认预约（CW -> CR -> CG）
    simple = {
        "instruments": [
            {"code": "G", "name": "国家基准", "is_national_standard": True},
            {"code": "R", "name": "参考标准"},
            {"code": "W", "name": "工作仪器"},
        ],
        "certificates": [
            {"id": "CG", "instrument_code": "G", "cert_number": "G-1",
             "issued_at": "2024-06-01T00:00:00",
             "valid_from": "2024-01-01T00:00:00",
             "valid_until": "2030-01-01T00:00:00",
             "uncertainty": 0.001, "unit": "mm"},
            {"id": "CR", "instrument_code": "R", "cert_number": "R-1",
             "issued_at": "2024-06-01T00:00:00",
             "valid_from": "2024-01-01T00:00:00",
             "valid_until": "2030-01-01T00:00:00",
             "uncertainty": 0.05, "unit": "mm"},
            {"id": "CW", "instrument_code": "W", "cert_number": "W-1",
             "issued_at": "2024-06-01T00:00:00",
             "valid_from": "2024-01-01T00:00:00",
             "valid_until": "2030-01-01T00:00:00",
             "uncertainty": 0.1, "unit": "mm"},
        ],
        "edges": [
            {"child_cert_id": "CW", "parent_cert_id": "CR"},
            {"child_cert_id": "CR", "parent_cert_id": "CG"},
        ],
    }
    _submit(client, simple)
    r = client.post(
        "/api/calibration-bookings",
        json={
            "instrument_code": "W",
            "start": "2026-06-20T09:00:00Z",
            "duration_seconds": 3600,
        },
    )
    assert r.status_code == 201, r.text

    # 溯源图膨胀为 529+1 条候选（DFS 先枚举 CP 分支，CR 支路被截断在外），
    # 样本中仅 1 条合格链 -> 结果未定（不得判唯一，也不得判断裂）
    wide = _wide_graph(p_ok={0}, q_ok={0})
    # 复用已存在的 G/W 仪器与 CG/CW 证书（upsert 同内容不增版）
    _submit(client, wide)

    report = _report(client)
    assert report["resolution"] == "indeterminate"

    r = client.get(
        "/api/calibration-bookings/inspection",
        params={
            "start": "2026-06-20T00:00:00Z",
            "end": "2026-06-21T00:00:00Z",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == {"total": 1, "valid": 0, "changed": 0, "invalid": 1}
    item = body["items"][0]
    assert item["status"] == "invalid"
    assert item["reason_code"] == "TOO_MANY_CANDIDATES"
    assert item["current_certificate_id"] is None


def test_deactivation_note_for_indeterminate_after_state(client):
    """停用评估：模拟后仍结果未定时给出 SIMULATED_RESULT_INDETERMINATE，
    不得声称“不存在唯一有效替代链”。"""
    _submit(client, _wide_graph(p_ok={0}, q_ok={0}))

    r = client.post(
        "/api/assessments/certificate-deactivation",
        json={"at": AT, "certificate_ids": ["CQ00"], "instrument_codes": ["W"]},
    )
    assert r.status_code == 200, r.text
    result = r.json()["results"][0]
    assert result["before"]["resolution"] == "indeterminate"
    assert result["before"]["enumeration_truncated"] is True
    assert result["after"]["resolution"] == "indeterminate"
    assert result["after"]["enumeration_truncated"] is True
    assert result["after"]["active_chain"] is None
    assert result["alternative_chain"] is None
    note_codes = {n["code"] for n in result["simulation_notes"]}
    assert "SIMULATED_RESULT_INDETERMINATE" in note_codes
    assert "SIMULATED_NO_ALTERNATIVE_CHAIN" not in note_codes


def test_coverage_segment_marks_indeterminate_window(client):
    """时间窗覆盖查询：结果未定的区段如实标记并带截断标记。"""
    _submit(client, _wide_graph(q_active=False))

    r = client.get(
        "/api/coverage/W",
        params={
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-12-31T23:59:59Z",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["segment_count"] == 1
    segment = body["segments"][0]
    assert segment["resolution"] == "indeterminate"
    assert segment["valid"] is False
    assert segment["enumeration_truncated"] is True
    assert "TOO_MANY_CANDIDATES" in segment["conflict_codes"]
    assert "CHAIN_BROKEN" not in segment["conflict_codes"]
