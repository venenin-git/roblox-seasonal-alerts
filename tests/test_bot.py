import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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


class NormalizeItemTests(unittest.TestCase):
    def test_bundle_url_string(self):
        item = bot.normalize_item("https://www.roblox.com/bundles/201/Headless-Horseman")
        self.assertEqual((item["itemType"], item["id"]), ("Bundle", 201))
        self.assertEqual(item["url"], "https://www.roblox.com/bundles/201/")
        self.assertIsNone(item["name"])
        self.assertFalse(item["notify_ended"])

    def test_localized_catalog_url(self):
        item = bot.normalize_item({"url": "https://www.roblox.com/pt/catalog/123456/Chapeu"})
        self.assertEqual((item["itemType"], item["id"]), ("Asset", 123456))

    def test_old_object_format_still_works(self):
        item = bot.normalize_item({"name": "X", "itemType": "bundle", "id": "201",
                                   "url": "https://www.roblox.com/bundles/201/X"})
        self.assertEqual((item["name"], item["itemType"], item["id"]), ("X", "Bundle", 201))

    def test_id_and_type_without_url(self):
        item = bot.normalize_item({"itemType": "Asset", "id": 9, "notify_ended": True})
        self.assertEqual(item["url"], "https://www.roblox.com/catalog/9/")
        self.assertTrue(item["notify_ended"])

    def test_invalid_entries(self):
        for bad in ("https://example.com/foo", {"name": "sem id"}, 42,
                    {"itemType": "Gear", "id": 1}, {"id": "abc"}):
            with self.assertRaises(ValueError):
                bot.normalize_item(bad)

    def test_load_items_dedupes_and_reports_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "items.json"
            path.write_text(json.dumps([
                "https://www.roblox.com/bundles/201/A",
                {"itemType": "Bundle", "id": 201},
                "lixo",
                "https://www.roblox.com/catalog/5/B",
            ]))
            items, errors = bot.load_items(path)
        self.assertEqual([i["id"] for i in items], [201, 5])
        self.assertEqual(len(errors), 1)


class EmbedTests(unittest.TestCase):
    def setUp(self):
        self.item = bot.normalize_item("https://www.roblox.com/bundles/201/Headless-Horseman")
        future = (datetime.now(timezone.utc) + timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        self.entry = {
            "name": "Headless Horseman",
            "description": "Um cavaleiro  sem\ncabeça. " * 40,
            "creatorName": "Roblox",
            "itemRestrictions": ["ThirteenPlus", "Collectible"],
            "offSaleDeadline": future,
            "unitsAvailableForConsumption": 1500,
            "totalQuantity": 5000,
            "bundledItems": [{"name": f"Peça {n}"} for n in range(10)],
        }

    def fields(self, embed):
        return {f["name"]: f["value"] for f in embed["fields"]}

    def test_available_embed_has_image_and_extras(self):
        embed = bot.build_embed(self.item, self.entry, 31000, "available", "https://img/x.png")
        fields = self.fields(embed)
        self.assertEqual(embed["image"]["url"], "https://img/x.png")
        self.assertIn("Headless Horseman", embed["title"])
        self.assertEqual(embed["url"], "https://www.roblox.com/bundles/201/")
        self.assertEqual(fields["💰 Preço"], "31.000 Robux")
        self.assertIn("Colecionável", fields["🏷️ Tipo"])
        self.assertEqual(fields["👤 Criador"], "Roblox")
        self.assertEqual(fields["📦 Estoque"], "1.500 de 5.000 restantes")
        self.assertIn("<t:", fields["⏳ Sai de venda"])
        self.assertIn("… e mais 2", fields["🧩 Contém"])
        self.assertLessEqual(len(embed["description"]), 2000)

    def test_embed_without_optional_data_is_minimal(self):
        embed = bot.build_embed(self.item, {}, None, "start")
        self.assertNotIn("image", embed)
        self.assertEqual(set(self.fields(embed)), {"💰 Preço", "🏷️ Tipo"})

    def test_past_deadline_is_hidden(self):
        self.entry["offSaleDeadline"] = "2020-01-01T00:00:00Z"
        embed = bot.build_embed(self.item, self.entry, 100, "available")
        self.assertNotIn("⏳ Sai de venda", self.fields(embed))

    def test_ended_event_and_preview_titles(self):
        ended = bot.build_embed(self.item, self.entry, 0, "ended")
        self.assertIn("saiu de venda", ended["title"])
        self.assertNotIn("💰 Preço", self.fields(ended))
        preview = bot.build_embed(self.item, self.entry, 100, "available", preview=True)
        self.assertTrue(preview["title"].startswith("👀 PRÉVIA"))


class ThumbnailTests(unittest.TestCase):
    def test_bundle_thumbnail_ok(self):
        item = bot.normalize_item("https://www.roblox.com/bundles/201/X")
        resp = FakeResponse(200, {"data": [{"state": "Completed", "imageUrl": "https://tr.rbxcdn.com/a"}]})
        with mock.patch.object(bot.requests, "get", return_value=resp) as get:
            self.assertEqual(bot.get_thumbnail_url(item), "https://tr.rbxcdn.com/a")
        self.assertIn("bundles", get.call_args.args[0])
        self.assertEqual(get.call_args.kwargs["params"]["bundleIds"], 201)

    def test_asset_uses_asset_endpoint(self):
        item = bot.normalize_item("https://www.roblox.com/catalog/5/X")
        resp = FakeResponse(200, {"data": [{"state": "Completed", "imageUrl": "u"}]})
        with mock.patch.object(bot.requests, "get", return_value=resp) as get:
            bot.get_thumbnail_url(item)
        self.assertEqual(get.call_args.kwargs["params"]["assetIds"], 5)

    def test_pending_then_none(self):
        item = bot.normalize_item("https://www.roblox.com/bundles/201/X")
        resp = FakeResponse(200, {"data": [{"state": "Pending", "imageUrl": ""}]})
        with mock.patch.object(bot.requests, "get", return_value=resp), \
             mock.patch.object(bot.time, "sleep"):
            self.assertIsNone(bot.get_thumbnail_url(item))


class NotifyTests(unittest.TestCase):
    def setUp(self):
        self.item = bot.normalize_item("https://www.roblox.com/bundles/201/X")

    def test_alert_still_sent_when_thumbnail_fails(self):
        with mock.patch.object(bot, "get_thumbnail_url", side_effect=RuntimeError("x")), \
             mock.patch.object(bot, "post_webhook") as post:
            bot.notify(self.item, {"name": "X"}, 100, "available")
        payload = post.call_args.args[0]
        self.assertNotIn("image", payload["embeds"][0])

    def test_mention_only_for_real_alerts(self):
        env = {"DISCORD_MENTION": "<@&123>"}
        with mock.patch.dict("os.environ", env), \
             mock.patch.object(bot, "get_thumbnail_url", return_value=None), \
             mock.patch.object(bot, "post_webhook") as post:
            bot.notify(self.item, {}, 1, "available")
            real = post.call_args.args[0]
            bot.notify(self.item, {}, 1, "available", preview=True)
            preview = post.call_args.args[0]
            bot.notify(self.item, {}, 0, "ended")
            ended = post.call_args.args[0]
        self.assertEqual(real["content"], "<@&123>")
        self.assertIn("roles", real["allowed_mentions"]["parse"])
        for payload in (preview, ended):
            self.assertNotIn("content", payload)
            self.assertEqual(payload["allowed_mentions"], {"parse": []})


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

    def test_details_are_sent_in_batches(self):
        items = [bot.normalize_item({"itemType": "Asset", "id": n}) for n in range(1, 251)]
        session = mock.MagicMock()
        session.headers = {}
        session.post.return_value = FakeResponse(200, {"data": []})
        bot.get_item_details(items, session=session)
        self.assertEqual(session.post.call_count, 3)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state_path = root / "state.json"
        self.config_path = root / "items.json"
        self.write_config(["https://www.roblox.com/bundles/201/Headless-Horseman"])
        for name, value in (("STATE_PATH", self.state_path), ("CONFIG_PATH", self.config_path)):
            patcher = mock.patch.object(bot, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_config(self, data):
        self.config_path.write_text(json.dumps(data))

    def _entry(self, item_id, for_sale, name="Item"):
        entry = {"id": item_id, "itemType": "Bundle", "name": name, "price": 100,
                 "priceStatus": None if for_sale else "Off Sale"}
        if for_sale:
            entry["itemStatus"] = ["Sale"]
        return entry

    def api(self, **sale_by_id):
        return {("bundle", int(k[1:])): self._entry(int(k[1:]), v) for k, v in sale_by_id.items()}

    def _state(self):
        return json.loads(self.state_path.read_text())

    def test_first_run_on_sale_sends_start_event(self):
        with mock.patch.object(bot, "get_item_details", return_value=self.api(i201=True)), \
             mock.patch.object(bot, "notify") as notify:
            self.assertEqual(bot.run(), 0)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[3], "start")
        self.assertTrue(self._state()["bundle:201"]["is_for_sale"])
        self.assertEqual(self._state()["bundle:201"]["name"], "Item")

    def test_transition_sends_alert_only_once(self):
        self.state_path.write_text(json.dumps({"bundle:201": {"is_for_sale": False}}))
        with mock.patch.object(bot, "get_item_details", return_value=self.api(i201=True)), \
             mock.patch.object(bot, "notify") as notify:
            bot.run()
            bot.run()
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(notify.call_args.args[3], "available")

    def test_no_alert_while_off_sale(self):
        with mock.patch.object(bot, "get_item_details", return_value=self.api(i201=False)), \
             mock.patch.object(bot, "notify") as notify:
            bot.run()
        notify.assert_not_called()
        self.assertFalse(self._state()["bundle:201"]["is_for_sale"])

    def test_ended_alert_only_when_enabled(self):
        self.state_path.write_text(json.dumps({"bundle:201": {"is_for_sale": True}}))
        with mock.patch.object(bot, "get_item_details", return_value=self.api(i201=False)), \
             mock.patch.object(bot, "notify") as notify:
            bot.run()
        notify.assert_not_called()

        self.write_config([{"url": "https://www.roblox.com/bundles/201/X", "notify_ended": True}])
        self.state_path.write_text(json.dumps({"bundle:201": {"is_for_sale": True}}))
        with mock.patch.object(bot, "get_item_details", return_value=self.api(i201=False)), \
             mock.patch.object(bot, "notify") as notify:
            bot.run()
        self.assertEqual(notify.call_args.args[3], "ended")

    def test_multiple_items_alert_independently(self):
        self.write_config([
            "https://www.roblox.com/bundles/201/A",
            "https://www.roblox.com/bundles/202/B",
            "https://www.roblox.com/bundles/203/C",
        ])
        self.state_path.write_text(json.dumps({
            "bundle:201": {"is_for_sale": True},
            "bundle:202": {"is_for_sale": False},
        }))
        api = self.api(i201=True, i202=True, i203=False)
        with mock.patch.object(bot, "get_item_details", return_value=api), \
             mock.patch.object(bot, "notify") as notify:
            bot.run()
        alerted = {(c.args[0]["id"], c.args[3]) for c in notify.call_args_list}
        self.assertEqual(alerted, {(202, "available")})
        state = self._state()
        self.assertTrue(state["bundle:202"]["is_for_sale"])
        self.assertFalse(state["bundle:203"]["is_for_sale"])

    def test_new_item_added_later_gets_start_alert(self):
        self.write_config([
            "https://www.roblox.com/bundles/201/A",
            "https://www.roblox.com/bundles/202/B",
        ])
        self.state_path.write_text(json.dumps({"bundle:201": {"is_for_sale": True}}))
        api = self.api(i201=True, i202=True)
        with mock.patch.object(bot, "get_item_details", return_value=api), \
             mock.patch.object(bot, "notify") as notify:
            bot.run()
        self.assertEqual([(c.args[0]["id"], c.args[3]) for c in notify.call_args_list], [(202, "start")])

    def test_removed_items_are_pruned_from_state(self):
        self.state_path.write_text(json.dumps({
            "bundle:23": {"is_for_sale": False},
            "bundle:201": {"is_for_sale": False},
        }))
        with mock.patch.object(bot, "get_item_details", return_value=self.api(i201=False)), \
             mock.patch.object(bot, "notify"):
            bot.run()
        self.assertNotIn("bundle:23", self._state())

    def test_failed_alert_is_retried_next_run(self):
        self.state_path.write_text(json.dumps({"bundle:201": {"is_for_sale": False}}))
        with mock.patch.object(bot, "get_item_details", return_value=self.api(i201=True)), \
             mock.patch.object(bot, "notify", side_effect=RuntimeError("boom")) as notify:
            bot.run()
            self.assertFalse(self._state()["bundle:201"]["is_for_sale"])
            notify.side_effect = None
            bot.run()
        self.assertEqual(notify.call_count, 2)
        self.assertTrue(self._state()["bundle:201"]["is_for_sale"])

    def test_dry_run_neither_alerts_nor_writes(self):
        with mock.patch.object(bot, "get_item_details", return_value=self.api(i201=True)), \
             mock.patch.object(bot, "notify") as notify:
            self.assertEqual(bot.run(dry_run=True), 0)
        notify.assert_not_called()
        self.assertFalse(self.state_path.exists())

    def test_corrupt_state_does_not_crash(self):
        self.state_path.write_text("{ not json")
        with mock.patch.object(bot, "get_item_details", return_value=self.api(i201=False)):
            self.assertEqual(bot.run(), 0)

    def test_fails_only_after_consecutive_api_errors(self):
        with mock.patch.object(bot, "get_item_details", side_effect=RuntimeError("down")):
            codes = [bot.run() for _ in range(4)]
        self.assertEqual(codes, [0, 0, 1, 1])
        with mock.patch.object(bot, "get_item_details", return_value=self.api(i201=False)):
            self.assertEqual(bot.run(), 0)
        self.assertEqual(self._state()["_meta"]["consecutive_errors"], 0)

    def test_item_missing_from_api_counts_as_error(self):
        with mock.patch.object(bot, "get_item_details", return_value={}):
            bot.run()
        self.assertEqual(self._state()["_meta"]["consecutive_errors"], 1)

    def test_bad_config_entry_fails_immediately_but_valid_items_run(self):
        self.write_config(["https://www.roblox.com/bundles/201/A", "link quebrado"])
        with mock.patch.object(bot, "get_item_details", return_value=self.api(i201=False)):
            self.assertEqual(bot.run(), 1)
        self.assertIn("bundle:201", self._state())

    def test_empty_config_fails(self):
        self.write_config([])
        self.assertEqual(bot.run(), 1)

    def test_preview_sends_without_touching_state(self):
        with mock.patch.object(bot, "get_item_details", return_value=self.api(i201=True)), \
             mock.patch.object(bot, "notify") as notify:
            self.assertEqual(bot.run_preview(), 0)
        self.assertTrue(notify.call_args.kwargs["preview"])
        self.assertFalse(self.state_path.exists())


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
        self.assertEqual(bot.format_price(31000), "31.000 Robux")
        self.assertEqual(bot.format_price(0), "Grátis")
        self.assertEqual(bot.format_price(None), "Preço não informado")


if __name__ == "__main__":
    unittest.main()
