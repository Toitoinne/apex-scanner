from apex.supervision.corrections import candidates_for
from apex.supervision.detector import REGRESSION_TYPE
from apex.supervision.stats import PLATEAU


def test_claude_actions_skipped_when_claude_paused():
    assert "request_claude_features" in candidates_for(PLATEAU, {})
    assert "request_claude_targeted_feature" in candidates_for(REGRESSION_TYPE, {})
    plateau = candidates_for(PLATEAU, {}, claude_available=False)
    assert "request_claude_features" not in plateau
    assert plateau == ["add_diverse_competitor", "early_lgbm_retrain"]
    assert candidates_for(REGRESSION_TYPE, {}, claude_available=False) == ["upweight_error_type"]
