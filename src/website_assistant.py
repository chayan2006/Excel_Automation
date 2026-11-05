"""The website assistant: answers questions about this website using only its approved knowledge base.

The rules it follows are in config/assistant/system_prompt.md and the pages it may answer from are the Markdown files in
config/assistant/knowledge_base/. Both are read for every question, so edits take effect without a restart.
"""
import html
import os
import re
from pathlib import Path

try:
    import anthropic
except ImportError:
    anthropic = None


DEFAULT_MODEL = "claude-opus-5-5"
# The model always thinks before answering; low effort keeps that short so a chat answer arrives quickly.
DEFAULT_EFFORT = "low"
# Thinking counts toward this limit as well, so it leaves room for both; it is not a target length for the answer.
MAX_TOKENS = 16000
MAX_QUESTION_LENGTH = 1000
# The last three questions and answers go with each question, so follow-ups such as "and for Jio?" make sense.
MAX_HISTORY_MESSAGES = 6
# If a safety classifier declines a question, the API re-runs it on the model Anthropic recommends for that case.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
DECLINED_ANSWER = "I can't help with that. Please ask me about this website's services and features."
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)

ANSWER_FORMAT = """\
The approved website knowledge base follows. It is the retrieved website content that the rules above refer to: answer \
only from it. Each page has a title and the link to that page on this website.

Your answer is shown in a small chat panel on the website. Write short paragraphs and "- " lists, without headings, \
tables or code blocks. Link to a page as [page title](link), using only the links in the knowledge base."""


class AssistantUnavailable(Exception):
    """The question could not be answered; the message can be shown to the person who asked."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def is_configured() -> bool:
    return anthropic is not None and bool(os.environ.get("ANTHROPIC_API_KEY"))


def split_front_matter(text: str) -> tuple[dict, str]:
    """Read the `key: value` lines between the leading `---` lines of a page, and return them with the rest."""
    match = FRONT_MATTER.match(text)
    if not match:
        return {}, text
    fields = {}
    for line in match.group(1).splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip().lower()] = value.strip()
    return fields, text[match.end():]


def load_pages(directory: Path) -> list[dict]:
    """The knowledge base pages in file name order, each with a title, its link on the website and its text."""
    pages = []
    for path in sorted(directory.glob("*.md")):
        fields, body = split_front_matter(path.read_text(encoding="utf-8"))
        title = fields.get("title") or path.stem.split("-", 1)[-1].replace("-", " ").capitalize()
        pages.append({"title": title, "url": fields.get("url", ""), "text": body.strip()})
    return pages


class WebsiteAssistant:
    def __init__(self, config_dir: Path, client=None):
        self.prompt_path = config_dir / "system_prompt.md"
        self.pages_dir = config_dir / "knowledge_base"
        self.model = os.environ.get("ASSISTANT_MODEL") or DEFAULT_MODEL
        self.effort = os.environ.get("ASSISTANT_EFFORT") or DEFAULT_EFFORT
        if client is not None:
            self.client = client
        elif anthropic is not None:
            # One retry within 50 seconds each keeps a question inside the web server's 120-second request timeout.
            self.client = anthropic.Anthropic(timeout=50.0, max_retries=1)
        else:
            self.client = None

    def system_blocks(self) -> list[dict]:
        pages = "\n\n".join(
            f'<page title="{html.escape(page["title"])}" link="{html.escape(page["url"])}">\n{page["text"]}\n</page>'
            for page in load_pages(self.pages_dir)
        )
        return [
            {"type": "text", "text": self.prompt_path.read_text(encoding="utf-8").strip()},
            # The rules and pages are the same for every question, so they are cached and later questions read them cheaply.
            {
                "type": "text",
                "text": f"{ANSWER_FORMAT}\n\n<website_knowledge_base>\n{pages}\n</website_knowledge_base>",
                "cache_control": {"type": "ephemeral"},
            },
        ]

    def ask(self, question: str, history: list[dict]) -> str:
        """Answer a question, given the earlier questions and answers of the conversation, oldest first."""
        if self.client is None:
            raise AssistantUnavailable("Anthropic library is not installed.", 503)
        try:
            system = self.system_blocks()
        except OSError as exc:
            raise AssistantUnavailable("The assistant is not set up correctly. Please tell your administrator.", 503) from exc
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                output_config={"effort": self.effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
                system=system,
                messages=[*history, {"role": "user", "content": question}],
            )
        except anthropic.RateLimitError as exc:
            raise AssistantUnavailable("The assistant is busy. Try again in a minute.", 503) from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.NotFoundError, anthropic.BadRequestError) as exc:
            # The API key, model name or effort setting is wrong; asking again will not help.
            raise AssistantUnavailable("The assistant is not set up correctly. Please tell your administrator.", 503) from exc
        except anthropic.APIStatusError as exc:
            raise AssistantUnavailable("The assistant could not answer just now. Try again in a minute.", 502) from exc
        except anthropic.APIConnectionError as exc:
            raise AssistantUnavailable("The assistant could not be reached. Try again in a minute.", 502) from exc

        if response.stop_reason == "refusal":
            return DECLINED_ANSWER
        answer = "".join(block.text for block in response.content if block.type == "text").strip()
        if not answer:
            raise AssistantUnavailable("The assistant could not answer that. Try asking in a different way.", 502)
        return answer
