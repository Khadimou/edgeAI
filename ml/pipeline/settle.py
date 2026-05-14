"""
Settlement automatique des paris : après chaque cycle du scheduler,
règle les paris PENDING dont le match est passé en FINISHED.
"""
from datetime import datetime, timezone, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

log = structlog.get_logger()


def _actual_outcome(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "HOME"
    elif away_score > home_score:
        return "AWAY"
    return "DRAW"


async def settle_finished_bets(session: AsyncSession) -> int:
    """
    Règle tous les paris PENDING dont le match est FINISHED depuis < 48h.
    Retourne le nombre de paris réglés.
    """
    # Récupérer les matchs FINISHED avec au moins un pari PENDING
    r = await session.execute(text("""
        SELECT DISTINCT m.id, m.home_score, m.away_score, m.status
        FROM matches m
        JOIN bets b ON b.match_id = m.id
        WHERE b.status = 'PENDING'
          AND m.status IN ('FINISHED', 'CANCELLED', 'POSTPONED')
          AND m.match_date >= NOW() - interval '48 hours'
    """))
    finished_matches = r.fetchall()

    if not finished_matches:
        return 0

    settled_count = 0

    for match_id, home_score, away_score, match_status in finished_matches:
        # Déterminer l'outcome réel
        if match_status == "FINISHED" and home_score is not None and away_score is not None:
            actual = _actual_outcome(home_score, away_score)
            is_void = False
        else:
            # Match annulé ou reporté → remboursement
            actual = None
            is_void = True

        # Récupérer tous les paris PENDING sur ce match
        r = await session.execute(text("""
            SELECT b.id, b.user_id, b.outcome, b.amount, b.odds
            FROM bets b
            WHERE b.match_id = :match_id AND b.status = 'PENDING'
        """), {"match_id": match_id})
        bets = r.fetchall()

        for bet_id, user_id, outcome, amount, odds in bets:
            try:
                async with session.begin_nested():
                    await _settle_one_bet(
                        session, bet_id, user_id,
                        outcome, amount, odds,
                        actual, is_void,
                    )
                settled_count += 1
            except Exception as e:
                log.error("settle_bet_error", bet_id=bet_id, error=str(e))

    if settled_count > 0:
        log.info("bets_settled", count=settled_count)

    return settled_count


async def _settle_one_bet(
    session: AsyncSession,
    bet_id: str,
    user_id: str,
    outcome: str,
    amount: float,
    odds: float,
    actual_outcome: str | None,
    is_void: bool,
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if is_void:
        new_status = "VOID"
        profit_loss = 0.0
        credit = amount  # remboursement
        event_type = "BET_VOID"
    elif outcome == actual_outcome:
        new_status = "WON"
        profit_loss = round(amount * (odds - 1), 2)
        credit = amount + profit_loss
        event_type = "BET_WON"
    else:
        new_status = "LOST"
        profit_loss = -amount
        credit = 0.0
        event_type = "BET_LOST"

    # Mettre à jour le pari
    await session.execute(text("""
        UPDATE bets
        SET status = :status,
            profit_loss = :profit_loss,
            settled_at = :now,
            updated_at = :now
        WHERE id = :bet_id
    """), {
        "status": new_status,
        "profit_loss": profit_loss,
        "now": now,
        "bet_id": bet_id,
    })

    # Mettre à jour la bankroll de l'utilisateur
    r = await session.execute(
        text("SELECT bankroll FROM users WHERE id = :uid"),
        {"uid": user_id},
    )
    row = r.fetchone()
    if not row:
        return
    new_bankroll = round(row[0] + credit, 2)

    await session.execute(text("""
        UPDATE users SET bankroll = :bankroll, updated_at = :now
        WHERE id = :uid
    """), {"bankroll": new_bankroll, "now": now, "uid": user_id})

    # Écrire dans l'historique bankroll (seulement si credit > 0 ou VOID)
    if credit > 0 or is_void:
        import secrets as _sec
        await session.execute(text("""
            INSERT INTO bankroll_history
                (id, user_id, amount, balance, event_type, reference_id, timestamp)
            VALUES
                (:id, :uid, :amount, :balance, :event_type, :ref, :ts)
        """), {
            "id": _sec.token_urlsafe(16),
            "uid": user_id,
            "amount": credit,
            "balance": new_bankroll,
            "event_type": event_type,
            "ref": bet_id,
            "ts": now,
        })

    log.info(
        "bet_settled",
        bet_id=bet_id,
        status=new_status,
        profit_loss=profit_loss,
        new_bankroll=new_bankroll,
    )
