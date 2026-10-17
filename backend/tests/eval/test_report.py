from mayday.eval.report import build_report, percentile, to_markdown


def R(s, correct=True, **kw):
    d = dict(scenario=s, correct=correct, incident=True, rca_s=10.0, tool_calls=4,
             verifier_rejections=0, conclusion_attempts=1, twin_error_s=2.0, remediated=True, verifier_enabled=True)
    d.update(kw)
    return d


def test_percentile():
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert percentile([], 0.9) == 0.0


def test_report():
    res = [R("S1"), R("S1", correct=False, rca_s=30.0, verifier_rejections=1, conclusion_attempts=2),
           R("S3"), R("S3", correct=False), R("S5", remediated=False), R("S5", remediated=True),
           R("S0", incident=False, correct=True), R("S0", incident=True),
           R("S1", correct=False, verifier_enabled=False)]
    rep = build_report(res, model="m", generated="2026-01-01")
    rows = {r["scenario"]: r for r in rep["rows"]}
    assert rep["total_runs"] == 9 and rep["false_incident_rate"] == 0.5
    assert rows["S1"]["accuracy"] == 0.5 and rows["S1"]["median_rca_s"] == 20.0
    assert rows["S1"]["verifier_rejection_rate"] == round(1 / 3, 4)
    assert rows["S3"]["decoy_cleared_pct"] == 50.0
    assert rows["S5"]["correctly_not_remediated_pct"] == 50.0
    assert rep["ablations"][0]["name"] == "verifier on" and rep["ablations"][1]["accuracy"] == 0.0
    assert "| S1 |" in to_markdown(rep)
