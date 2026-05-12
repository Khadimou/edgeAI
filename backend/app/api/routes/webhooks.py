import stripe
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update

from app.core.config import settings
from app.core.deps import get_db
from app.db.models import BankrollHistory, User

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

PLAN_MAP = {
    settings.stripe_price_pro: "PRO",
    settings.stripe_price_elite: "ELITE",
}


@router.post("/stripe")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, settings.stripe_webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError):
        raise HTTPException(status_code=400, detail="Signature Stripe invalide")

    evt = event["type"]
    data = event["data"]["object"]

    if evt == "customer.subscription.created":
        await _handle_sub_created(data, db)
    elif evt == "customer.subscription.updated":
        await _handle_sub_updated(data, db)
    elif evt == "customer.subscription.deleted":
        await _handle_sub_deleted(data, db)
    elif evt == "invoice.payment_succeeded":
        await _handle_payment_succeeded(data, db)

    return {"status": "ok"}


async def _handle_sub_created(sub: dict, db: AsyncSession):
    price_id = sub["items"]["data"][0]["price"]["id"]
    plan = PLAN_MAP.get(price_id, "FREE")
    await db.execute(
        update(User)
        .where(User.stripe_customer_id == sub["customer"])
        .values(plan=plan, stripe_subscription_id=sub["id"])
    )
    await db.commit()


async def _handle_sub_updated(sub: dict, db: AsyncSession):
    price_id = sub["items"]["data"][0]["price"]["id"]
    plan = PLAN_MAP.get(price_id, "FREE")
    await db.execute(
        update(User)
        .where(User.stripe_subscription_id == sub["id"])
        .values(plan=plan)
    )
    await db.commit()


async def _handle_sub_deleted(sub: dict, db: AsyncSession):
    await db.execute(
        update(User)
        .where(User.stripe_subscription_id == sub["id"])
        .values(plan="FREE", stripe_subscription_id=None)
    )
    await db.commit()


async def _handle_payment_succeeded(invoice: dict, db: AsyncSession):
    result = await db.execute(
        select(User).where(User.stripe_customer_id == invoice["customer"])
    )
    user = result.scalar_one_or_none()
    if user:
        db.add(BankrollHistory(
            user_id=user.id,
            amount=0,
            balance=user.bankroll,
            event_type="SUBSCRIPTION_CREDIT",
            reference_id=invoice["id"],
        ))
        await db.commit()
