"""
Endpoint backtest : lit le dernier run de backtest.py depuis Redis.
Le calcul est fait par le ml_worker (CLI) ; ici on sert seulement le résultat.
"""
import json
from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.deps import get_current_user
from app.core.redis import get_redis
from app.db.models import User

router = APIRouter(prefix="/backtest", tags=["backtest"])


REDIS_KEYS = {
    "FOOTBALL_1X2": "backtest:latest",
    "FOOTBALL_OU": "backtest:ou:latest",
    "FOOTBALL_AH": "backtest:ah:latest",
    "NBA": "backtest:nba:latest",
    "NBA_TOTALS": "backtest:nba_totals:latest",
}

SCRIPTS = {
    "FOOTBALL_1X2": "backtest.py",
    "FOOTBALL_OU": "ou_backtest.py",
    "FOOTBALL_AH": "ah_pipeline.py",
    "NBA": "nba_backtest.py",
    "NBA_TOTALS": "nba_totals_pipeline.py",
}


@router.get("/latest")
async def get_latest_backtest(
    market: str = Query("FOOTBALL_1X2", pattern="^(FOOTBALL_1X2|FOOTBALL_OU|FOOTBALL_AH|NBA|NBA_TOTALS)$"),
    # Compat ascendante avec l'ancien param ?sport=FOOTBALL|NBA
    sport: str | None = Query(None),
    redis=Depends(get_redis),
    _user: User = Depends(get_current_user),
):
    if sport == "NBA":
        market = "NBA"
    elif sport == "FOOTBALL":
        market = "FOOTBALL_1X2"
    key = REDIS_KEYS.get(market, REDIS_KEYS["FOOTBALL_1X2"])
    raw = await redis.get(key)
    if not raw:
        script = SCRIPTS.get(market, "backtest.py")
        raise HTTPException(
            status_code=404,
            detail=f"Aucun backtest {market} exécuté. Lancez `python {script}` dans le ml_worker.",
        )
    return json.loads(raw)
