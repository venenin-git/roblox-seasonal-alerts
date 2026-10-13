import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data or {}
        self.headers = headers or {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class SaleStatusTests(unittest.TestCase):
    def test_explicit_boolean_wins(self):
        self.assertEqual(bot.sale_status({"isForSale": True, "price": 0}), (True, 0))
        self.assertEqual(bot.sale_status({"isForSale": False, "price": 500}), (False, 500))

    def test_off_sale_beats_listed_price(self):
        self.assertEqual(bot.sale_status({"priceStatus": "Off Sale", "price": 500}), (False, 500))
        self.assertEqual(bot.sale_status({"itemStatus": ["OffSale"], "price": 500}), (False, 500))

    def test_sale_status_values(self):
        self.assertEqual(bot.sale_status({"itemStatus": ["Sale"], "price": 100}), (True, 100))
        self.assertEqual(bot.sale_status({"itemStatus": "ForSale", "price": 100}), (True, 100))

    def test_free_item(self):
        self.assertEqual(bot.sale_status({"priceStatus": "Free"}), (True, 0))

    def test_price_fallback_only_without_status_fields(self):
        self.assertEqual(bot.sale_status({"price": 75}), (True, 75))
        self.assertEqual(bot.sale_status({"price": 0}), (False, 0))
        self.assertEqual(bot.sale_status({"price": "abc"}), (False, "abc"))
        self.assertEqual(bot.sale_status({"itemStatus": ["New"], "price": 75}), (False, 75))

    def test_pascal_case_fields(self):
        self.assertEqual(bot.sale_status({"IsForSale": True, "Price": 10}), (True, 10))


class CsrfTests(unittest.TestCase):
    def test_retries_with_token_after_403(self):
        session = mock.MagicMock()
        session.headers = {}
        session.post.side_effect = [
            FakeResponse(403, headers={"x-csrf-token": "abc123"}),
            FakeResponse(200, {"data": []}),
        ]
        bot.post_with_csrf(session, "https://x", {})
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(session.headers["X-CSRF-TOKEN"], "abc123")

    def test_plain_403_without_token_raises(self):
        session = mock.MagicMock()
        session.headers = {}
        session.post.return_value = FakeResponse(403)
        with self.assertRaises(RuntimeError):
            bot.post_with_csrf(session, "https://x", {})


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state_path = root / "state.json"
        self.config_path = root / "items.json"
        self.config_path.write_text(json.dumps([
            {"name": "Item", "itemType": "Bundle", "id": 23}
        ]))
        for name, value in (("STATE_PATH", self.state_path), ("CONFIG_PATH", self.config_path)):
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _api(self, for_sale):
        entry = {"id": 23, "itemType": "Bundle", "name": "Item",
                 "priceStatus": None if for_sale else "Off Sale", "price": 100}
        if for_sale:
            entry["itemStatus"] = ["Sale"]
        return {(("bundle"), 23): entry}

    def _state(self):
        return json.loads(self.state_path.read_text())

    def test_first_run_on_sale_sends_startup_alert(self):
        with mock.patch.object(bot, "get_item_details", return_value=self._api(True)), \
             mock.patch.object(bot, "send_discord_alert") as alert:
            self.assertEqual(bot.run(), 0)
        alert.assert_called_once()
        self.assertTrue(alert.call_args.kwargs["first_run"])
        self.assertTrue(self._state()["bundle:23"]["is_for_sale"])

    def test_transition_sends_alert_only_once(self):
        self.state_path.write_text(json.dumps({"bundle:23": {"is_for_sale": False}}))
        with mock.patch.object(bot, "get_item_details", return_value=self._api(True)), \
             mock.patch.object(bot, "send_discord_alert") as alert:
            bot.run()
            bot.run()
        self.assertEqual(alert.call_count, 1)

    def test_no_alert_while_off_sale(self):
        with mock.patch.object(bot, "get_item_details", return_value=self._api(False)), \
             mock.patch.object(bot, "send_discord_alert") as alert:
            bot.run()
        alert.assert_not_called()
        self.assertFalse(self._state()["bundle:23"]["is_for_sale"])

    def test_failed_alert_is_retried_next_run(self):
        self.state_path.write_text(json.dumps({"bundle:23": {"is_for_sale": False}}))
        with mock.patch.object(bot, "get_item_details", return_value=self._api(True)), \
             mock.patch.object(bot, "send_discord_alert", side_effect=RuntimeError("boom")) as alert:
            bot.run()
            self.assertFalse(self._state()["bundle:23"]["is_for_sale"])
            alert.side_effect = None
            bot.run()
        self.assertEqual(alert.call_count, 2)
        self.assertTrue(self._state()["bundle:23"]["is_for_sale"])

    def test_dry_run_neither_alerts_nor_writes(self):
        with mock.patch.object(bot, "get_item_details", return_value=self._api(True)), \
             mock.patch.object(bot, "send_discord_alert") as alert:
            self.assertEqual(bot.run(dry_run=True), 0)
        alert.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_corrupt_state_does_not_crash(self):
        self.state_path.write_text("{ not json")
        with mock.patch.object(bot, "get_item_details", return_value=self._api(False)):
            self.assertEqual(bot.run(), 0)

    def test_fails_only_after_consecutive_api_errors(self):
        with mock.patch.object(bot, "get_item_details", side_effect=RuntimeError("down")):
            codes = [bot.run() for _ in range(4)]
        self.assertEqual(codes, [0, 0, 1, 1])
        with mock.patch.object(bot, "get_item_details", return_value=self._api(False)):
            self.assertEqual(bot.run(), 0)
        self.assertEqual(self._state()["_meta"]["consecutive_errors"], 0)

    def test_item_missing_from_api_counts_as_error(self):
        with mock.patch.object(bot, "get_item_details", return_value={}):
            bot.run()
        self.assertEqual(self._state()["_meta"]["consecutive_errors"], 1)


class WebhookTests(unittest.TestCase):
    def test_retries_on_429(self):
        responses = [FakeResponse(429, {"retry_after": 0}), FakeResponse(204)]
        with mock.patch.object(bot, "get_webhook_url", return_value="https://hook"), \
             mock.patch.object(bot.requests, "post", side_effect=responses) as post, \
             mock.patch.object(bot.time, "sleep"):
            bot.post_webhook({"content": "x"})
        self.assertEqual(post.call_count, 2)

    def test_missing_webhook_raises(self):
        with mock.patch.object(bot, "get_webhook_url", return_value=""):
            with self.assertRaises(RuntimeError):
                bot.post_webhook({})

    def test_price_format(self):
        self.assertEqual(bot.format_price(1500), "1.500 Robux")
        self.assertEqual(bot.format_price(0), "Grátis")
        self.assertEqual(bot.format_price(None), "Preço não informado")


if __name__ == "__main__":
    unittest.main()

