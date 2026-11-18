"""Offline plumbing demonstration. All market values are synthetic."""
from datetime import datetime, timedelta
import json

from banknifty.core import Candle, Session, prepare_windows, validate_series, label_returns
from banknifty.agents.luna import assess
from banknifty.groww import IST
from banknifty.technical_features import build_technical_snapshot


def demo():
    session = Session(open=datetime(2026, 10, 1, 9, 15, tzinfo=IST),
                      close=datetime(2026, 10, 1, 15, 30, tzinfo=IST),
                      source="synthetic example; not an exchange calendar")
    candles = []
    for index in range(180):
        start = session.open + timedelta(minutes=index)
        price = 50000 + index
        candles.append(Candle(symbol="DEMO-BANKNIFTY", kind="index", start=start,
            end=start + timedelta(minutes=1), open=price, high=price+2, low=price-1,
            close=price+1, volume=None, open_interest=None, source="synthetic"))
    as_of = datetime(2026, 10, 1, 10, 30, tzinfo=IST)
    observed = validate_series([c for c in candles if c.end <= as_of], as_of)
    windows = prepare_windows(as_of, session)
    technical_snapshot = build_technical_snapshot(observed, as_of, generated_at=as_of)
    for window in windows:
        window["forecast_status"] = "not_implemented"
        window["probability"] = None
        window["technical_assessment"] = assess(technical_snapshot, window["horizon_minutes"]).to_dict()
    return {"mode": "SYNTHETIC DEMONSTRATION - not a forecast or accuracy test",
            "observed_candle_count": len(observed), "technical_features": technical_snapshot.to_dict(), "windows": windows,
            "offline_evaluation_example": label_returns(candles, as_of, session)}


if __name__ == "__main__":
    print(json.dumps(demo(), indent=2))
