"""Sign-in is required for every page, file and API call; accounts live in a temporary project root."""
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app as web
from test_regressions import sign_in

PASSWORD = 'correct horse battery'


class SignInTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(web, 'PROJECT_ROOT', self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = web.app.test_client()

    def csrf(self):
        page = self.client.get('/login').get_data(as_text=True)
        return re.search(r'name="csrf" value="([^"]+)"', page).group(1)

    def sign_in_with_form(self, username, password, next_url=''):
        return self.client.post('/login', data={'username': username, 'password': password, 'next': next_url, 'csrf': self.csrf()})

    def test_pages_and_files_redirect_to_sign_in(self):
        for path in ['/', '/download-master', '/review/documents', '/review/documents/1/original', '/backups/x.xlsx/download']:
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)
            self.assertTrue(response.location.startswith('/login?next='), response.location)

    def test_form_posts_are_refused_without_sign_in(self):
        (self.root / 'Master.xlsx').write_bytes(b'original')
        response = self.client.post('/upload-files', data={})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/login')
        self.assertFalse((self.root / 'input').exists())

    def test_review_api_answers_401_without_sign_in(self):
        response = self.client.patch('/review/api/documents/1/fields', json={'path': 'x'})
        self.assertEqual(response.status_code, 401)
        self.assertIn('sign in again', response.get_json()['error'])

    def test_health_check_and_static_files_stay_open(self):
        self.assertEqual(self.client.get('/healthz').status_code, 200)
        with self.client.get('/static/styles.css') as response:
            self.assertEqual(response.status_code, 200)

    def test_sign_in_returns_to_the_requested_page(self):
        web.user_store().add('alice', PASSWORD, 'Alice Rao')
        response = self.sign_in_with_form('ALICE', PASSWORD, '/?run=x')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, '/?run=x')
        page = self.client.get('/').get_data(as_text=True)
        self.assertIn('Signed in as <b>Alice Rao</b>', page)
        self.assertIsNotNone(web.user_store().get('alice')['last_login_at'])

    def test_wrong_password_and_unknown_user_get_the_same_answer(self):
        web.user_store().add('alice', PASSWORD)
        with self.assertLogs(web.app.logger, level='WARNING'):
            wrong = self.sign_in_with_form('alice', 'not the password')
            unknown = self.sign_in_with_form('bob', PASSWORD)
        for response in (wrong, unknown):
            self.assertEqual(response.status_code, 401)
            self.assertIn(b'do not match an account', response.data)
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_sign_in_never_redirects_to_another_website(self):
        web.user_store().add('alice', PASSWORD)
        for target in ['//evil.example', 'https://evil.example', '/\\evil.example', '/\t/evil.example']:
            self.client = web.app.test_client()
            response = self.sign_in_with_form('alice', PASSWORD, target)
            self.assertEqual(response.location, '/', target)

    def test_sign_in_form_needs_csrf_token(self):
        web.user_store().add('alice', PASSWORD)
        response = self.client.post('/login', data={'username': 'alice', 'password': PASSWORD})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_sign_out_ends_the_session(self):
        sign_in(self.client)
        page = self.client.get('/').get_data(as_text=True)
        csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
        self.assertEqual(self.client.post('/logout', data={'csrf': csrf}).location, '/login')
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_password_change_and_removal_sign_the_account_out(self):
        sign_in(self.client, 'alice')
        self.assertEqual(self.client.get('/').status_code, 200)
        web.user_store().set_password('alice', 'a brand new password')
        self.assertEqual(self.client.get('/').status_code, 302)

        sign_in(self.client, 'bob')
        web.user_store().remove('bob')
        self.assertEqual(self.client.get('/').status_code, 302)

    def test_login_page_explains_when_no_accounts_exist(self):
        page = self.client.get('/login').get_data(as_text=True)
        self.assertIn('No accounts have been set up yet', page)
        self.assertNotIn('name="password"', page)

    def test_command_line_manages_accounts(self):
        runner = web.app.test_cli_runner()
        result = runner.invoke(args=['users', 'add', 'alice', '--name', 'Alice Rao', '--password', PASSWORD])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(web.user_store().authenticate('alice', PASSWORD)['name'], 'Alice Rao')

        self.assertNotEqual(runner.invoke(args=['users', 'add', 'alice', '--password', PASSWORD]).exit_code, 0)
        self.assertNotEqual(runner.invoke(args=['users', 'add', 'carol', '--password', 'short']).exit_code, 0)
        self.assertIn('alice', runner.invoke(args=['users', 'list']).output)

        result = runner.invoke(args=['users', 'password', 'alice', '--password', 'a brand new password'])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertIsNone(web.user_store().authenticate('alice', PASSWORD))

        self.assertEqual(runner.invoke(args=['users', 'remove', 'alice']).exit_code, 0)
        self.assertIsNone(web.user_store().get('alice'))
        self.assertNotEqual(runner.invoke(args=['users', 'remove', 'alice']).exit_code, 0)


if __name__ == '__main__':
    unittest.main()
