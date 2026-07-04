"""
ml_predictor.py — Self-learning signal win/loss predictor.

Trains an XGBoost classifier on the bot's own historical signal data
to predict whether a new signal will hit TP or SL.

Requires at least ML_MIN_TRAINING_SIGNALS closed signals before
it can be enabled. Retrains automatically on a weekly schedule.
"""
import os
import json
import numpy as np
from typing import Optional

from config.settings import ML_PREDICTOR_ENABLED, ML_MIN_TRAINING_SIGNALS
from config.logger import get_logger

logger = get_logger(__name__)

MODEL_PATH = "models/signal_predictor.joblib"
_model = None
_feature_names = None


FEATURE_KEYS = [
    "rsi", "macd_hist", "adx", "atr_pct", "vol_ratio",
    "stoch_k", "stoch_d", "bb_width", "bbw_pctile", "atr_pctile",
    "mfi", "cmf", "ema200_slope", "bb_pct",
]

CATEGORICAL_FEATURES = {
    "ema_bull": lambda ind: 1 if ind.get("ema_bull") else 0,
    "ema_bear": lambda ind: 1 if ind.get("ema_bear") else 0,
    "above_200": lambda ind: 1 if ind.get("above_200") is True else (0 if ind.get("above_200") is False else 0.5),
    "above_vwap": lambda ind: 1 if ind.get("above_vwap") is True else (0 if ind.get("above_vwap") is False else 0.5),
    "supertrend_bull": lambda ind: 1 if ind.get("supertrend_dir") == 1 else 0,
    "vol_spike": lambda ind: 1 if ind.get("vol_spike") else 0,
    "obv_rising": lambda ind: 1 if ind.get("obv_rising") is True else (0 if ind.get("obv_rising") is False else 0.5),
    "structure_bull": lambda ind: 1 if ind.get("structure_bull") else 0,
    "structure_bear": lambda ind: 1 if ind.get("structure_bear") else 0,
    "bull_engulf": lambda ind: 1 if ind.get("bull_engulf") else 0,
    "bear_engulf": lambda ind: 1 if ind.get("bear_engulf") else 0,
    "is_long": lambda ind: 1 if ind.get("_direction") == "LONG" else 0,
    "is_intraday": lambda ind: 1 if ind.get("_style") == "intraday" else 0,
}


def _extract_features(indicators: dict, direction: str = "", style: str = "") -> list:
    """Extract a feature vector from an indicator dict."""
    # Inject direction and style for categorical feature extraction
    ind = dict(indicators)
    ind["_direction"] = direction
    ind["_style"] = style

    features = []

    # Numeric features
    for key in FEATURE_KEYS:
        val = ind.get(key)
        features.append(float(val) if val is not None else 0.0)

    # Categorical features
    for name, fn in CATEGORICAL_FEATURES.items():
        features.append(float(fn(ind)))

    return features


def _get_feature_names() -> list:
    """Return ordered list of feature names."""
    return FEATURE_KEYS + list(CATEGORICAL_FEATURES.keys())


def train_model(min_signals: int = ML_MIN_TRAINING_SIGNALS) -> Optional[dict]:
    """
    Train the XGBoost model on historical signal data.

    Returns training stats dict or None if insufficient data.
    """
    global _model, _feature_names

    try:
        import xgboost as xgb
        from sklearn.model_selection import cross_val_score
        import joblib
    except ImportError:
        logger.warning("ML dependencies not installed (xgboost, scikit-learn, joblib)")
        return None

    from src.database.db_logger import SessionLocal, SignalRecord

    with SessionLocal() as db:
        records = db.query(SignalRecord).filter(
            SignalRecord.outcome.isnot(None),
            SignalRecord.outcome.notin_(("EXPIRED", "NOFILL")),
            SignalRecord.indicators_json.isnot(None),
        ).all()
        db.expunge_all()

    if len(records) < min_signals:
        logger.info(f"ML: Not enough data to train ({len(records)}/{min_signals} signals)")
        return None

    X = []
    y = []

    for rec in records:
        try:
            ind = json.loads(rec.indicators_json) if rec.indicators_json else {}
            if not ind:
                continue

            features = _extract_features(ind, rec.direction, rec.style)
            label = 1 if rec.outcome in ("TP1", "TP2", "TP3") else 0

            X.append(features)
            y.append(label)
        except Exception:
            continue

    if len(X) < min_signals:
        logger.info(f"ML: Not enough valid features ({len(X)}/{min_signals})")
        return None

    X = np.array(X)
    y = np.array(y)

    logger.info(f"ML: Training on {len(X)} signals ({sum(y)} wins, {len(y)-sum(y)} losses)")

    model = xgb.XGBClassifier(
        n_estimators=100,
        max_depth=4,
        learning_rate=0.1,
        min_child_weight=3,
        subsample=0.8,
        colsample_bytree=0.8,
        eval_metric="logloss",
        use_label_encoder=False,
        verbosity=0,
    )

    # Cross-validation
    scores = cross_val_score(model, X, y, cv=min(5, len(X) // 10), scoring="accuracy")
    cv_accuracy = scores.mean()

    # Train final model
    model.fit(X, y)

    # Save model
    os.makedirs("models", exist_ok=True)
    _feature_names = _get_feature_names()
    joblib.dump({"model": model, "feature_names": _feature_names}, MODEL_PATH)

    _model = model

    # Feature importance
    importances = dict(zip(_feature_names, model.feature_importances_))
    top_features = sorted(importances.items(), key=lambda x: x[1], reverse=True)[:5]

    stats = {
        "total_signals": len(X),
        "wins": int(sum(y)),
        "losses": int(len(y) - sum(y)),
        "cv_accuracy": round(cv_accuracy * 100, 1),
        "top_features": top_features,
    }

    logger.info(f"ML: Model trained — CV accuracy: {stats['cv_accuracy']}%")
    logger.info(f"ML: Top features: {', '.join(f'{k}={v:.3f}' for k, v in top_features)}")

    return stats


def predict_win_probability(indicators: dict, direction: str, style: str) -> float:
    """
    Predict the probability of a signal being a winner.

    Returns a float between 0.0 and 1.0.
    Returns 0.5 (neutral) if the model is not available.
    """
    global _model, _feature_names

    if not ML_PREDICTOR_ENABLED:
        return 0.5

    # Load model if not in memory
    if _model is None:
        try:
            import joblib
            if os.path.exists(MODEL_PATH):
                data = joblib.load(MODEL_PATH)
                _model = data["model"]
                _feature_names = data["feature_names"]
                logger.info("ML: Model loaded from disk")
            else:
                return 0.5
        except Exception as e:
            logger.debug(f"ML: Could not load model: {e}")
            return 0.5

    try:
        features = _extract_features(indicators, direction, style)
        prob = _model.predict_proba(np.array([features]))[0][1]  # P(win)
        logger.debug(f"ML: {direction} win probability = {prob:.2f}")
        return float(prob)
    except Exception as e:
        logger.debug(f"ML: Prediction error: {e}")
        return 0.5
