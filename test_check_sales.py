"""Tests for check_sales.py. Run with: python3 -m unittest -v"""

import argparse
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

import check_sales as cs

FIXTURES = Path(__file__).with_name("fixtures")


def load(name):
    with (FIXTURES / name).open(encoding="utf-8") as f:
        return json.load(f)["products"]


def product(pid, price="4.00", compare_at="20.00", available=True, title=None):
    return {
        "id": pid,
        "title": title or f"Product {pid}",
        "handle": f"product-{pid}",
        "tags": [],
        "images": [],
        "variants": [{"title": "Default Title", "price": price,
                      "compare_at_price": compare_at, "available": available}],
    }


D = dt.date(2026, 11, 4)  # a Wednesday


class MatchingRules(unittest.TestCase):
    def setUp(self):
        self.matches = cs.find_matches(load("rules.json"))

    def test_checks_every_variant(self):
        m = self.matches["1"]
        self.assertEqual(m["sizes"], ["6"])
        self.assertEqual(m["percent"], 75)
        self.assertEqual(str(m["price"]), "4.95")

    def test_requires_available(self):
        self.assertNotIn("2", self.matches)  # in-stock tag is ignored too

    def test_ignores_zero_price(self):
        self.assertNotIn("3", self.matches)

    def test_skips_missing_or_zero_compare_at(self):
        self.assertNotIn("4", self.matches)

    def test_exactly_sixty_percent_included(self):
        self.assertIn("5", self.matches)
        self.assertEqual(self.matches["5"]["percent"], 60)
        self.assertEqual(self.matches["5"]["sizes"], [])

    def test_just_under_sixty_excluded(self):
        self.assertNotIn("6", self.matches)
        self.assertNotIn("8", self.matches)

    def test_hidden_when_any_true_tag(self):
        self.assertTrue(self.matches["7"]["hidden"])
        self.assertFalse(self.matches["1"]["hidden"])

    def test_final_sale_tag(self):
        self.assertTrue(self.matches["1"]["final_sale"])
        self.assertFalse(self.matches["5"]["final_sale"])

    def test_excludes_girls_and_adult_clothing(self):
        self.assertNotIn("10", self.matches)  # girls' clothing
        self.assertNotIn("11", self.matches)  # adult clothing
        self.assertNotIn("12", self.matches)  # girl-and-doll matching set
        self.assertIn("13", self.matches)     # doll-only clothing still alerts

    def test_comma_string_tags(self):
        p = product(9)
        p["tags"] = "Foo, ExcludeFromSearch: true"
        self.assertTrue(cs.is_hidden(p))

    def test_real_fixture_parses(self):
        products = load("last_chance_2026-11-08.json")
        matches = cs.find_matches(products)
        self.assertGreater(len(products), 0)
        for pid, m in matches.items():
            self.assertGreaterEqual(m["percent"], 60, m["title"])
            self.assertTrue(m["url"].startswith(cs.STORE + "/products/"))


class Notifications(unittest.TestCase):
    def test_item_message_format(self):
        m = cs.find_matches(load("rules.json"))
        n = cs.item_notification(m["1"])
        self.assertEqual(n["title"], "75% off: Holiday Cheer PJ Pants for Girls")
        self.assertEqual(n["message"], "$4.95 (was $20.00). In stock: size 6. Final sale.")
        self.assertEqual(n["click"], cs.STORE + "/products/holiday-cheer-pj-pants")
        self.assertEqual(n["attach"], "https://cdn.example/pants.png")
        self.assertIn("Not on the sale page.", cs.item_notification(m["7"])["message"])

    def test_more_than_five_becomes_summary(self):
        ms = list(cs.find_matches([product(i) for i in range(6)]).values())
        notes = cs.build_notifications(ms)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["click"], cs.SALE_PAGE)
        self.assertEqual(len(cs.build_notifications(ms[:5])), 5)


class StateTracking(unittest.TestCase):
    def matches(self, *pids):
        return cs.find_matches([product(p) for p in pids])

    def test_new_product_alerts_once(self):
        state = {"items": {}}
        self.assertEqual(cs.update_state(state, self.matches(1), D), ["1"])
        self.assertEqual(cs.update_state(state, self.matches(1), D + dt.timedelta(1)), [])

    def test_drop_from_fifty_to_sixty_alerts(self):
        state = {"items": {}}
        cs.update_state(state, cs.find_matches([product(1, price="10.00")]), D)
        self.assertEqual(cs.update_state(state, self.matches(1), D + dt.timedelta(1)), ["1"])

    def test_flicker_within_seven_days_no_alert(self):
        state = {"items": {}}
        cs.update_state(state, self.matches(1), D)
        cs.update_state(state, {}, D + dt.timedelta(1))
        self.assertEqual(state["items"]["1"]["missing_since"], "2026-11-05")
        self.assertEqual(cs.update_state(state, self.matches(1), D + dt.timedelta(6)), [])
        self.assertIsNone(state["items"]["1"]["missing_since"])

    def test_back_after_seven_days_alerts(self):
        state = {"items": {}}
        cs.update_state(state, self.matches(1), D)
        cs.update_state(state, {}, D + dt.timedelta(1))
        self.assertEqual(cs.update_state(state, self.matches(1), D + dt.timedelta(8)), ["1"])

    def test_dropped_after_seven_days_missing(self):
        state = {"items": {}}
        cs.update_state(state, self.matches(1), D)
        cs.update_state(state, {}, D + dt.timedelta(1))
        cs.update_state(state, {}, D + dt.timedelta(8))
        self.assertNotIn("1", state["items"])

    def test_safety_check(self):
        cs.safety_check(300, {"last_product_count": 587})
        with self.assertRaises(cs.CheckError):
            cs.safety_check(200, {"last_product_count": 587})
        with self.assertRaises(cs.CheckError):
            cs.safety_check(0, None)


class Fetching(unittest.TestCase):
    def test_paginates_until_short_page_and_dedupes(self):
        calls = []

        def fake_get(url):
            calls.append(url)
            if "/sale/" in url and url.endswith("page=1"):
                return {"products": [product(i) for i in range(250)]}
            if "/sale/" in url:
                return {"products": [product(i) for i in range(250, 260)]}
            return {"products": [product(i) for i in range(5)]}

        products = cs.fetch_all(get=fake_get, pause=0)
        self.assertEqual(len(products), 260)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("sort_by" not in u for u in calls))

    def test_bad_response_raises(self):
        with self.assertRaises(cs.CheckError):
            cs.fetch_collection("sale", get=lambda url: {"oops": 1}, pause=0)


class RunFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name) / "state.json"
        self.sent = []
        self.args = argparse.Namespace(dry_run=False, state=str(self.state))
        cs.os.environ["NTFY_TOPIC"] = "test-topic"

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, products, today=D, send=None):
        return cs.run(self.args, today=today, fetch=lambda: products,
                      send=send or (lambda n, t, s: self.sent.append(n)))

    def test_first_run_sends_one_summary_and_saves(self):
        self.run_with([product(i) for i in range(10)])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("set up", self.sent[0]["title"])
        saved = json.loads(self.state.read_text())
        self.assertEqual(len(saved["items"]), 10)
        self.assertEqual(saved["last_product_count"], 10)

    def test_failed_send_does_not_save(self):
        self.run_with([product(1)])
        before = self.state.read_text()

        def boom(*a):
            raise cs.CheckError("down")

        with self.assertRaises(cs.CheckError):
            self.run_with([product(1), product(2)], send=boom)
        self.assertEqual(self.state.read_text(), before)

    def test_monday_heartbeat_once(self):
        self.run_with([product(1)])
        monday = dt.date(2026, 11, 9)
        self.sent.clear()
        self.run_with([product(1)], today=monday)
        self.run_with([product(1)], today=monday)
        self.assertEqual([n["title"] for n in self.sent], ["Sale alerts still running"])

    def test_dry_run_saves_nothing(self):
        self.args.dry_run = True
        self.run_with([product(1)])
        self.assertFalse(self.state.exists())
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
