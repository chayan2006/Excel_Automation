"""Website assistant: a chat panel on the dashboards that answers questions about this website.

Answers come only from the approved knowledge base in config/assistant/; the logic is in src/website_assistant.py.
The conversation so far travels with each question as a signed token, so the server keeps no chat state and nobody can
put words in the assistant's mouth.
"""
import threading
from pathlib import Path

from flask import Blueprint, current_app, g, jsonify, request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from src.website_assistant import (
    MAX_HISTORY_MESSAGES,
    MAX_QUESTION_LENGTH,
    AssistantUnavailable,
    WebsiteAssistant,
    is_configured,
)


PROJECT_ROOT = Path(__file__).resolve().parent
assistant_bp = Blueprint("assistant", __name__, url_prefix="/assistant")
_assistant_lock = threading.Lock()


def assistant() -> WebsiteAssistant:
    with _assistant_lock:
        if "website_assistant" not in current_app.extensions:
            current_app.extensions["website_assistant"] = WebsiteAssistant(PROJECT_ROOT / "config" / "assistant")
        return current_app.extensions["website_assistant"]


def conversation_tokens() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(current_app.config["SECRET_KEY"], salt="assistant-conversation")


def earlier_messages(token) -> list[dict]:
    """The conversation so far, if the token was issued to the signed-in account and has not expired."""
    if not isinstance(token, str) or not token:
        return []
    try:
        conversation = conversation_tokens().loads(token, max_age=current_app.permanent_session_lifetime.total_seconds())
    except BadSignature:
        return []
    return conversation["messages"] if conversation.get("user") == g.user["username"] else []


@assistant_bp.app_context_processor
def assistant_template_helpers():
    return {"assistant_enabled": is_configured, "assistant_max_question": MAX_QUESTION_LENGTH}


@assistant_bp.post("/ask")
def ask():
    # Requiring a JSON body means another website cannot trigger this with a plain form post.
    if not request.is_json:
        return jsonify(error="Send the question as JSON."), 415
    payload = request.get_json(silent=True)
    question = payload.get("question") if isinstance(payload, dict) else None
    if not isinstance(question, str) or not question.strip():
        return jsonify(error="Type a question first."), 400
    question = question.strip()
    if len(question) > MAX_QUESTION_LENGTH:
        return jsonify(error=f"Keep the question to {MAX_QUESTION_LENGTH} characters or fewer."), 400
    if not is_configured():
        return jsonify(error="The assistant is not set up yet. Please tell your administrator."), 503

    history = earlier_messages(payload.get("conversation"))
    try:
        answer = assistant().ask(question, history)
    except AssistantUnavailable as exc:
        current_app.logger.warning("Website assistant could not answer %s: %r", g.user["username"], exc.__cause__ or exc)
        return jsonify(error=str(exc)), exc.status
    messages = [*history, {"role": "user", "content": question}, {"role": "assistant", "content": answer}]
    conversation = conversation_tokens().dumps({"user": g.user["username"], "messages": messages[-MAX_HISTORY_MESSAGES:]})
    return jsonify(answer=answer, conversation=conversation)
