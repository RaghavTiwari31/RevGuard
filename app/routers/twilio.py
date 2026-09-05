from __future__ import annotations

import os
from fastapi import APIRouter, Form, Request, BackgroundTasks
from fastapi.responses import PlainTextResponse

from app.db import get_session_factory, Trace
from app.llm import generate_support_reply
from app.logging_config import get_logger
from sqlalchemy import select

logger = get_logger(__name__)

router = APIRouter(
    prefix="/twilio",
    tags=["twilio"],
)

async def process_and_reply(from_number: str, body: str):
    """Background task to process the incoming message, lookup context, and send reply."""
    logger.info("twilio.webhook.processing", extra={"from": from_number, "body": body})
    
    # 1. Clean the phone number to try and match what we have in the DB.
    # Twilio sends 'whatsapp:+918888888888'. We might have stored '8888888888' or '+918888888888'.
    clean_number = from_number.replace('whatsapp:', '')
    
    # Very naive lookup: grab the most recent Trace/Event for this customer.
    # In a real app we'd link phone -> Customer -> Event.
    # For the hackathon, we'll try to find any Trace where the guardrail checks contain this phone,
    # or just assume the last failed transaction is theirs if we can't do a clean phone match.
    # Actually, the Trace row doesn't store phone directly, but let's query the latest Trace 
    # to at least have *some* context if we can't join cleanly right now.
    
    context = None
    factory = get_session_factory()
    try:
        async with factory() as session:
            # Get the absolute most recent trace for the demo
            result = await session.execute(
                select(Trace).order_by(Trace.timestamp.desc()).limit(1)
            )
            latest_trace = result.scalar_one_or_none()
            if latest_trace:
                context = {
                    "amount_inr": latest_trace.amount_inr,
                    "error_code": "unknown", # We'd join with Event to get this in a full build
                    "error_reason": latest_trace.pre_flight_rejection_reason or "unknown",
                    "classification_rule": latest_trace.classification_rule or "unknown",
                    "timestamp": str(latest_trace.timestamp),
                }
    except Exception as e:
        logger.error("twilio.webhook.db_error", extra={"error": str(e)})

    # 2. Generate AI Reply
    reply_text = await generate_support_reply(body, context)
    
    # 3. Send back via Twilio
    try:
        from twilio.rest import Client
        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        sender = os.getenv("TWILIO_WHATSAPP_SENDER")
        
        if account_sid and auth_token and sender:
            client = Client(account_sid, auth_token)
            client.messages.create(
                from_=sender,
                body=reply_text,
                to=from_number
            )
            logger.info("twilio.webhook.reply_sent", extra={"to": from_number})
        else:
            logger.warning("twilio.webhook.missing_credentials")
    except Exception as e:
        logger.error("twilio.webhook.send_error", extra={"error": str(e)})


@router.post("/webhook")
async def twilio_webhook(
    background_tasks: BackgroundTasks,
    request: Request,
    From: str = Form(...),
    Body: str = Form(...)
):
    """
    Endpoint for Twilio to hit when a customer replies to our WhatsApp message.
    We return 200 OK immediately so Twilio doesn't timeout, and process the reply in the background.
    """
    logger.info("twilio.webhook.received", extra={"from": From})
    
    # Process in background
    background_tasks.add_task(process_and_reply, From, Body)
    
    # Twilio expects XML (TwiML) or just a 200 OK if we are sending out-of-band.
    # We will just return a simple OK since we use the REST API to reply asynchronously.
    return PlainTextResponse("OK")
