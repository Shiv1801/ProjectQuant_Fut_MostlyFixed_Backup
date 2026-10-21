from config import db


def test_model_score_persists_production_evaluation_id(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "snapshot.db")
    db.init_db()

    evaluation_id = "2026-10-18T12:00:00+05:30"
    score_id = db.insert_model_score(
        index_id="NIFTY50",
        fired=False,
        direction="long_call",
        veto_reason="chart_not_confirmed",
        quant_confidence=0.8,
        chart_confirmed=False,
        vwap_confirmed=None,
        feature_vector={"zscore": 2.0},
        evaluation_id=evaluation_id,
    )

    row = db.get_connection().execute(
        "SELECT evaluation_id, feature_vector_json FROM model_scores WHERE id = ?",
        (score_id,),
    ).fetchone()
    assert row["evaluation_id"] == evaluation_id
    assert '"zscore": 2.0' in row["feature_vector_json"]
