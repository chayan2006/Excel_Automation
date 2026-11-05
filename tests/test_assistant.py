"""The website assistant answers from the approved knowledge base; the Claude API is replaced by a recording fake."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import anthropic
except ImportError:
    anthropic = None

try:
    import httpx as httpx2
except ImportError:
    httpx2 = None

import app as web
from src.website_assistant import DECLINED_ANSWER, WebsiteAssistant, load_pages
from test_regressions import sign_in

RULES = 'Answer only from the website knowledge base.'
SHIPPED_CONFIG = Path(web.__file__).resolve().parent / 'config' / 'assistant'


class FakeMessages:
    """Stands in for client.beta.messages: records each request and returns (or raises) the queued replies."""
    def __init__(self):
        self.requests = []
        self.replies = []

    def create(self, **request):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def reply(text, stop_reason='end_turn'):
    content = [SimpleNamespace(type='thinking', thinking='', signature='signed')]
    if text:
        content.append(SimpleNamespace(type='text', text=text))
    return SimpleNamespace(stop_reason=stop_reason, content=content)


def api_error(error_class, status):
    request = httpx2.Request('POST', 'https://api.anthropic.com/v1/messages')
    return error_class('failed', response=httpx2.Response(status, request=request), body=None)


@unittest.skipIf(anthropic is None or httpx2 is None, "anthropic or httpx package not installed")
class AssistantTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for patcher in (patch.object(web, 'PROJECT_ROOT', self.root), patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'test-key'})):
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ('ASSISTANT_MODEL', 'ASSISTANT_EFFORT'):
            os.environ.pop(name, None)  # restored with the rest of the environment after the test

        self.config = self.root / 'assistant'
        (self.config / 'knowledge_base').mkdir(parents=True)
        (self.config / 'system_prompt.md').write_text(RULES)
        (self.config / 'knowledge_base' / '01-accounts.md').write_text(
            '---\ntitle: Accounts and signing in\nurl: /login\n---\nAsk your administrator for an account.\n')
        self.messages = FakeMessages()
        fake_client = SimpleNamespace(beta=SimpleNamespace(messages=self.messages))
        web.app.extensions['website_assistant'] = WebsiteAssistant(self.config, client=fake_client)
        self.addCleanup(web.app.extensions.pop, 'website_assistant', None)
        self.client = web.app.test_client()
        sign_in(self.client)

    def ask(self, question, conversation=None, client=None):
        return (client or self.client).post('/assistant/ask', json={'question': question, 'conversation': conversation})

    def test_question_is_answered_from_the_rules_and_knowledge_base(self):
        self.messages.replies = [reply('Ask your administrator. See [Accounts and signing in](/login).')]
        response = self.ask('  How do I get an account?  ')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['answer'], 'Ask your administrator. See [Accounts and signing in](/login).')

        request = self.messages.requests[0]
        self.assertEqual(request['model'], 'claude-opus-5-5')
        self.assertEqual(request['output_config'], {'effort': 'low'})
        self.assertEqual((request['betas'], request['fallbacks']), (['server-side-fallback-2026-07-01'], 'default'))
        self.assertNotIn('thinking', request)
        rules, knowledge_base = request['system']
        self.assertEqual(rules, {'type': 'text', 'text': RULES})
        self.assertIn('<page title="Accounts and signing in" link="/login">\nAsk your administrator for an account.\n</page>', knowledge_base['text'])
        self.assertEqual(knowledge_base['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(request['messages'], [{'role': 'user', 'content': 'How do I get an account?'}])

    def test_follow_up_questions_carry_the_last_three_exchanges(self):
        self.messages.replies = [reply(f'Answer {number}') for number in range(1, 6)]
        conversation = None
        for number in range(1, 6):
            conversation = self.ask(f'Question {number}', conversation).get_json()['conversation']
        sent = self.messages.requests[-1]['messages']
        self.assertEqual([message['content'] for message in sent], [
            'Question 2', 'Answer 2', 'Question 3', 'Answer 3', 'Question 4', 'Answer 4', 'Question 5'])
        self.assertEqual([message['role'] for message in sent], ['user', 'assistant'] * 3 + ['user'])

    def test_tampered_or_borrowed_conversations_are_not_used(self):
        self.messages.replies = [reply('First answer'), reply('Second'), reply('Third')]
        conversation = self.ask('First question').get_json()['conversation']

        self.ask('Tampered', conversation[:-1] + ('B' if conversation.endswith('A') else 'A'))
        other_client = web.app.test_client()
        sign_in(other_client, 'bob', 'Bob')
        self.ask('Borrowed', conversation, client=other_client)
        for request, question in zip(self.messages.requests[1:], ['Tampered', 'Borrowed']):
            self.assertEqual(request['messages'], [{'role': 'user', 'content': question}])

    def test_empty_long_and_form_posted_questions_are_refused(self):
        self.assertEqual(self.ask('   ').status_code, 400)
        self.assertEqual(self.ask('x' * 1001).status_code, 400)
        self.assertEqual(self.client.post('/assistant/ask', data={'question': 'Hello'}).status_code, 415)
        self.assertEqual(self.messages.requests, [])

    def test_assistant_needs_sign_in(self):
        response = self.ask('Hello', client=web.app.test_client())
        self.assertEqual(response.status_code, 401)
        self.assertIn('sign in again', response.get_json()['error'])
        self.assertEqual(self.messages.requests, [])

    def test_panel_appears_only_with_an_api_key(self):
        self.assertIn(b'class="assistant-launcher"', self.client.get('/').data)
        with patch.dict(os.environ, {'ANTHROPIC_API_KEY': ''}):
            self.assertNotIn(b'class="assistant-launcher"', self.client.get('/').data)
            response = self.ask('Hello')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.messages.requests, [])

    def test_declined_question_gets_a_polite_answer(self):
        self.messages.replies = [reply('', stop_reason='refusal')]
        self.assertEqual(self.ask('Something off-limits').get_json()['answer'], DECLINED_ANSWER)

    def test_failures_are_explained_and_logged(self):
        connection_failure = anthropic.APIConnectionError(request=httpx2.Request('POST', 'https://api.anthropic.com/v1/messages'))
        cases = [
            (api_error(anthropic.RateLimitError, 429), 503, 'busy'),
            (api_error(anthropic.AuthenticationError, 401), 503, 'not set up correctly'),
            (api_error(anthropic.InternalServerError, 500), 502, 'could not answer just now'),
            (connection_failure, 502, 'could not be reached'),
            (reply(''), 502, 'Try asking in a different way'),
        ]
        for failure, status, explanation in cases:
            self.messages.replies = [failure]
            with self.subTest(explanation), self.assertLogs(web.app.logger, level='WARNING'):
                response = self.ask('How do I sign out?')
            self.assertEqual(response.status_code, status)
            self.assertIn(explanation, response.get_json()['error'])

    def test_missing_rules_file_is_reported_without_calling_the_api(self):
        (self.config / 'system_prompt.md').unlink()
        with self.assertLogs(web.app.logger, level='WARNING'):
            response = self.ask('Hello')
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.messages.requests, [])


class KnowledgeBaseTests(unittest.TestCase):
    def test_pages_are_read_in_file_order_with_titles_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            pages_dir = Path(directory)
            (pages_dir / '02-review-workspace.md').write_text('No front matter here.')
            (pages_dir / '01-accounts.md').write_text('---\ntitle: Accounts\nurl: /login\n---\nBody text.\n')
            (pages_dir / 'notes.txt').write_text('Not a page.')
            self.assertEqual(load_pages(pages_dir), [
                {'title': 'Accounts', 'url': '/login', 'text': 'Body text.'},
                {'title': 'Review workspace', 'url': '', 'text': 'No front matter here.'},
            ])

    def test_shipped_rules_and_pages_load(self):
        pages = load_pages(SHIPPED_CONFIG / 'knowledge_base')
        self.assertTrue(all(page['title'] and page['url'] and page['text'] for page in pages))
        rules, knowledge_base = WebsiteAssistant(SHIPPED_CONFIG, client=object()).system_blocks()
        self.assertTrue(rules['text'].startswith('You are a website-specific AI assistant.'))
        for page in pages:
            self.assertIn(f'<page title="{page["title"]}" link="{page["url"]}">', knowledge_base['text'])


if __name__ == '__main__':
    unittest.main()
