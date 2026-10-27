"""Only a literal-aware trailing marker can request a musical TTV score."""
from __future__ import annotations

import unittest

from cogs.tts.performance import singing_request


class SingingSuffixTests(unittest.TestCase):
    def test_attached_or_spaced_final_marker_requests_singing(self):
        for text in ('olá mundo~', 'olá mundo ~', 'olá mundo~   ', 'olá mundo ~\n\t'):
            with self.subTest(text=text):
                self.assertEqual(singing_request(text), (' '.join(text.split()), True))

    def test_cantar_prefix_is_removed_only_when_the_suffix_requests_singing(self):
        for text, expected in (('/cantar olá mundo~', 'olá mundo~'),
                               ('/CANTAR olá mundo ~', 'olá mundo ~'),
                               ('/cantar\tolá mundo ~', 'olá mundo ~')):
            with self.subTest(text=text):
                self.assertEqual(singing_request(text), (expected, True))

    def test_plain_text_and_cantar_without_suffix_do_not_request_singing(self):
        for text in ('', 'olá mundo', '/cantar olá mundo', '/CANTAR olá', '/cantar',
                     'cantar no chuveiro', '  olá\nmundo  '):
            with self.subTest(text=text):
                self.assertEqual(singing_request(text), (text, False))

    def test_internal_tilde_never_requests_a_melody(self):
        for text in ('olá~ mundo', 'olá ~ mundo', 'olá ~ ~ mundo', 'olá~!', 'olá ~.'):
            with self.subTest(text=text):
                self.assertEqual(singing_request(text), (text, False))

    def test_escaped_final_tilde_is_literal(self):
        for text in (r'olá mundo\~', r'/cantar olá\~', r'olá \~   '):
            with self.subTest(text=text):
                self.assertEqual(singing_request(text), (text, False))

    def test_code_and_quotes_protect_their_tildes(self):
        for text in ('olá `mundo~`', 'olá ```mundo~', 'olá `mundo~',
                     'olá "mundo~"', 'olá “mundo~”', '"~"', '`~`'):
            with self.subTest(text=text):
                self.assertEqual(singing_request(text), (text, False))

    def test_url_final_tilde_is_literal(self):
        for text in ('https://example.org/mundo~', 'olá http://example.org/~',
                     'www.example.org/~', '<https://example.org/~>'):
            with self.subTest(text=text):
                self.assertEqual(singing_request(text), (text, False))

    def test_marker_outside_literal_content_still_requests_singing(self):
        for text in (r'olá \~ mundo ~', 'olá `mundo~` ~',
                     'https://example.org/~ ~', 'olá "mundo~" ~'):
            with self.subTest(text=text):
                self.assertEqual(singing_request(text), (text, True))

    def test_keeps_the_last_marker_and_all_internal_singing_holds(self):
        self.assertEqual(singing_request('olá ~ ~ ~'), ('olá ~ ~ ~', True))
        self.assertEqual(singing_request('olá~~'), ('olá~~', True))

    def test_escaped_backslash_does_not_escape_a_following_active_marker(self):
        text = 'olá \\\\~'
        self.assertEqual(singing_request(text), (text, True))


if __name__ == '__main__':
    unittest.main()
