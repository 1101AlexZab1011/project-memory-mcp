"""The page script and the page markup have to agree about element ids.

The UI has no build step and no JavaScript runtime in the test suite, which is a
deliberate trade - but it left 175 lines that nothing ever read except a browser.
A renamed or mistyped id shipped silently and was found by a person clicking.

This is the cheap check that catches that class of bug: every `$('#id')` the
script looks up must exist in the HTML, and every id the HTML defines for the
script's benefit must be looked up. It needs no node, no jsdom and no new
dependency.

What it deliberately does not do is test behaviour. Full DOM testing needs a
browser; for 175 lines that is the wrong trade, and this gets most of the value
for almost none of the cost.
"""

from __future__ import annotations

import re
import unittest

from project_memory_mcp import ui

#: `$('#name')` - the only way the script reaches an element.
LOOKUP = re.compile(r"""\$\(\s*['"]#([A-Za-z0-9_-]+)['"]\s*\)""")

#: `id="name"` in the markup.
DEFINED = re.compile(r"""\bid=["']([A-Za-z0-9_-]+)["']""")


class UiAssetTests(unittest.TestCase):
    def setUp(self):
        self.script = ui.script()
        self.page = ui.PAGE

    def test_every_element_the_script_looks_up_exists_in_the_page(self):
        looked_up = set(LOOKUP.findall(self.script))
        defined = set(DEFINED.findall(self.page))
        missing = sorted(looked_up - defined)
        self.assertEqual(
            [], missing,
            f"the script queries {missing}, which the page does not define - "
            "$(...) returns null and the handler dies silently")

    def test_every_id_the_page_defines_is_used_by_the_script(self):
        # The other direction: an id left behind after a rename is dead markup,
        # and dead markup is how the first direction gets broken later.
        looked_up = set(LOOKUP.findall(self.script))
        defined = set(DEFINED.findall(self.page))
        unused = sorted(defined - looked_up)
        self.assertEqual([], unused, f"the page defines {unused}, which nothing uses")

    def test_the_script_is_inlined_into_the_served_page(self):
        # The no-build promise: one self-contained document, nothing fetched.
        page = ui.app_page()
        self.assertIn("function publishState()", page)
        self.assertNotIn("%(script)s", page)
        self.assertNotIn("<script src=", page)

    def test_the_script_is_not_empty_and_is_balanced(self):
        # Not a parser, but it catches a truncated or half-written file, which
        # is the failure mode of editing a string out of Python by hand.
        self.assertGreater(len(self.script.splitlines()), 50)
        for opener, closer in (("{", "}"), ("(", ")"), ("[", "]")):
            self.assertEqual(self.script.count(opener), self.script.count(closer),
                             f"unbalanced {opener}{closer} in app.js")

    def test_the_login_page_needs_no_script(self):
        # It is a form post. Anything more would be a way to get JavaScript
        # wrong on the one page that has to work before you are signed in.
        self.assertNotIn("<script", ui.login_page())


if __name__ == "__main__":
    unittest.main()
