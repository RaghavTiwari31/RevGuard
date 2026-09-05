from __future__ import annotations

import os

from fastapi import APIRouter, BackgroundTasks, Form, Request
from fastapi.responses import PlainTextResponse

from app.llm import generate_support_reply
from app.logging_config import get_logger
from app.replies import find_context_for_phone, record_inbound_reply

logger = get_logger(__name__)

router = APIRouter(
    prefix="/twilio",
    tags=["twilio"],
)

async def send_whatsapp(to_number: str, text: str) -> bool:
    """Send one WhatsApp message back through Twilio. Returns True if it went out."""
    try:
        from twilio.rest import Client

        account_sid = os.getenv("TWILIO_ACCOUNT_SID")
        auth_token = os.getenv("TWILIO_AUTH_TOKEN")
        sender = os.getenv("TWILIO_WHATSAPP_SENDER")

        if not (account_sid and auth_token and sender):
            logger.warning("twilio.webhook.missing_credentials")
            return False

        Client(account_sid, auth_token).messages.create(
            from_=sender, body=text, to=to_number
        )
        logger.info("twilio.webhook.reply_sent", extra={"to": to_number})
        return True
    except Exception as exc:
        logger.error("twilio.webhook.send_error", extra={"error": str(exc)})
        return False


async def process_and_reply(from_number: str, body: str) -> None:
    """
    Handle one inbound WhatsApp message.

    Compliance comes before conversation.  The reply is classified first, and a
    stop keyword or dispute freezes automation and cancels every armed retry —
    only then, and only if this was an ordinary message, do we generate an
    assistant reply.  Answering "please stop contacting me" with a cheerful
    prompt to retry the payment would be the worst thing this endpoint could do.
    """
    logger.info("twilio.webhook.processing", extra={"from": from_number})

    # 1. Classify and apply the freeze if warranted. Shared with the generic
    #    /webhook/reply transport so both reach the same conclusion.
    result = await record_inbound_reply(
        body,
        channel="whatsapp",
        phone=from_number,
    )

    if result.is_stop_keyword:
        # A brief human acknowledgement, not a dunning message and not an LLM
        # improvisation — the customer asked us to stop.
        await send_whatsapp(
            from_number,
            "Aapki request note kar li gayi hai. Hum aapko is payment ke baare mein "
            "aur messages nahi bhejenge. Hamari team jald hi aapse sampark karegi. 🙏",
        )
        logger.info("twilio.webhook.frozen", extra={
            "from": from_number,
            "reply_id": result.reply_id,
            "cancelled_retries": result.cancelled_retries,
        })
        return

    # 2. Ordinary message — look up this caller's own most recent failure.
    #    Scoped to the customer behind the number, so we never describe one
    #    person's payment to another.
    context = None
    try:
        context = await find_context_for_phone(from_number)
    except Exception as exc:
        logger.error("twilio.webhook.db_error", extra={"error": str(exc)})

    # 3. Generate and send the assistant reply.
    reply_text = await generate_support_reply(body, context)
    await send_whatsapp(from_number, reply_text)


from fastapi.responses import Response

@router.post("/webhook")
async def twilio_webhook(
    request: Request,
    From: str = Form(...),
    Body: str = Form(...)
):
    """
    Endpoint for Twilio to hit when a customer replies to our WhatsApp message.
    We process synchronously and return TwiML to bypass the new Twilio Content API strict sandbox rules.
    """
    logger.info("twilio.webhook.received", extra={"from": From})
    
    # Process synchronously to get the reply text
    logger.info("twilio.webhook.processing", extra={"from": From})
    
    result = await record_inbound_reply(
        Body,
        channel="whatsapp",
        phone=From,
    )
    
    if result.is_stop_keyword:
        reply_text = "Aapki request note kar li gayi hai. Hum aapko is payment ke baare mein aur messages nahi bhejenge. Hamari team jald hi aapse sampark karegi. 🙏"
        logger.info("twilio.webhook.frozen", extra={
            "from": From,
            "reply_id": result.reply_id,
            "cancelled_retries": result.cancelled_retries,
        })
    else:
        context = None
        try:
            context = await find_context_for_phone(From)
        except Exception as exc:
            logger.error("twilio.webhook.db_error", extra={"error": str(exc)})
            
        reply_text = await generate_support_reply(Body, context)
        
    logger.info("twilio.webhook.reply_generated", extra={"reply": reply_text})
    
    # Return TwiML
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Message>{reply_text}</Message>
</Response>"""
    return Response(content=twiml, media_type="text/xml")
