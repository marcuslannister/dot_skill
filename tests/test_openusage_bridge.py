import unittest
from datetime import datetime, timezone

from dot_mcp.canvas import validate_canvas_payload
from dot_mcp.openusage_bridge import (
    PROVIDERS,
    SourceDataError,
    _reset_label,
    build_canvas_payload,
    prepare_provider,
    stale_provider,
)


def envelope(resources, *, stale=False, errors=None, providers=None):
    return {
        "schema": "openusage.limits.v1",
        "providers": providers
        or {
            "provider": {
                "fetchedAt": "2026-09-03T16:00:00Z",
                "stale": stale,
                "resources": resources,
            }
        },
        "errors": errors or [],
    }


def percent(value, resets_at=None):
    result = {
        "kind": "consumption",
        "unit": "percent",
        "used": value * 100,
        "limit": 100,
        "utilization": value,
    }
    if resets_at:
        result["resetsAt"] = resets_at
    return result


class OpenUsageBridgeTests(unittest.TestCase):
    def test_maps_zero_and_rounds_visible_percent(self):
        provider, invalid = prepare_provider(
            "claude",
            envelope(
                {
                    "session": percent(0),
                    "weekly": percent(0.425, "2026-09-04T00:00:00Z"),
                }
            ),
        )

        self.assertFalse(invalid)
        self.assertEqual(provider["metrics"]["session"], {"percent": 0})
        self.assertEqual(provider["metrics"]["weekly"]["percent"], 43)
        self.assertIn("reset", provider["metrics"]["weekly"])

    def test_retains_only_missing_metric_and_marks_provider_stale(self):
        previous = {
            "metrics": {
                "session": {"percent": 20},
                "weekly": {"percent": 30, "reset": "Sep 8, 4:00 PM"},
            },
            "stale": False,
            "fetched_at": "2026-09-03T15:00:00Z",
        }
        provider, invalid = prepare_provider(
            "codex",
            envelope(
                {"session": percent(0.4)},
                errors=[{"providerId": "codex", "message": "redacted"}],
            ),
            previous,
        )

        self.assertFalse(invalid)
        self.assertTrue(provider["stale"])
        self.assertEqual(provider["metrics"]["session"], {"percent": 40})
        self.assertEqual(provider["metrics"]["weekly"], previous["metrics"]["weekly"])

    def test_ambiguous_family_uses_stale_cache(self):
        previous = {"metrics": {"weekly": {"percent": 55}}, "stale": False}
        response = envelope(
            {},
            providers={
                "claude": {"resources": {}, "stale": False},
                "claude@12345678": {"resources": {}, "stale": False},
            },
        )

        with self.assertRaises(SourceDataError):
            prepare_provider("claude", response, previous)
        self.assertEqual(stale_provider("claude", previous)["metrics"]["weekly"]["percent"], 55)
        self.assertTrue(stale_provider("claude", previous)["stale"])

    def test_canvas_omits_unavailable_rows_and_passes_validator(self):
        providers = {
            "claude": {
                "metrics": {"session": {"percent": 71}},
                "stale": True,
                "fetched_at": None,
            }
        }
        payload = build_canvas_payload(providers, "canvas-task")

        self.assertTrue(validate_canvas_payload(payload)["valid"])
        self.assertIn("claude_session_percent", payload["data"])
        self.assertNotIn("claude_weekly_percent", payload["data"])
        self.assertIn("STALE", str(payload["windowData"]))

    def test_each_canvas_contains_one_provider_and_grok_is_removed(self):
        provider = {
            "metrics": {"weekly": {"percent": 71}},
            "stale": False,
            "fetched_at": None,
        }

        self.assertEqual(set(PROVIDERS), {"claude", "codex"})
        with self.assertRaises(ValueError):
            build_canvas_payload({"claude": provider, "codex": provider})

    def test_canvas_uses_demo_header_and_metric_cards(self):
        provider = {
            "metrics": {
                "session": {"percent": 5, "reset": "Sep 4, 3:39 AM"},
                "weekly": {"percent": 20, "reset": "Sep 8, 4:00 PM"},
            },
            "stale": False,
            "fetched_at": None,
        }
        root = build_canvas_payload({"claude": provider})["windowData"]["default"][0]
        cards = root["props"]["children"][0]

        self.assertIn("flex-row", cards["props"].get("tw", ""))
        self.assertEqual(len(cards["props"]["children"]), 2)
        for card, label in zip(
            cards["props"]["children"], ("Session", "Weekly")
        ):
            self.assertIn("border", card["props"].get("tw", ""))
            self.assertIn("rounded", card["props"].get("tw", ""))
            title, reset, percent, _ = card["props"]["children"]
            icon, metric = title["props"]["children"]
            self.assertEqual(icon["type"], "img")
            self.assertTrue(icon["props"]["src"].startswith("data:image/png;base64,"))
            self.assertEqual(metric["props"]["children"], label)
            self.assertIn("_reset", reset["props"]["children"])
            self.assertIn("text-14", reset["props"].get("tw", ""))
            self.assertEqual(reset["props"].get("style", {}).get("lineHeight"), "20px")
            self.assertEqual(percent["props"].get("style", {}).get("lineHeight"), "40px")
            self.assertEqual(percent["props"].get("style", {}).get("marginTop"), -4)
            self.assertEqual(len(card["props"]["children"]), 4)

    def test_formats_relative_reset_time(self):
        now = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(
            _reset_label("2026-09-04T15:12:00Z", now=now), "in 3h 12m"
        )
        self.assertEqual(_reset_label("2026-09-06T15:00:00Z", now=now), "in 2d 3h")

    def test_codex_titles_use_embedded_icon(self):
        provider = {
            "metrics": {
                "session": {"percent": 85, "reset": "in 3h 12m"},
                "weekly": {"percent": 68, "reset": "in 2d 3h"},
            },
            "stale": False,
            "fetched_at": None,
        }
        root = build_canvas_payload({"codex": provider})["windowData"]["default"][0]
        cards = root["props"]["children"][0]["props"]["children"]

        for card, label in zip(cards, ("Session", "Weekly")):
            icon, metric = card["props"]["children"][0]["props"]["children"]
            self.assertEqual(icon["type"], "img")
            self.assertTrue(icon["props"]["src"].startswith("data:image/png;base64,"))
            self.assertEqual(metric["props"]["children"], label)


if __name__ == "__main__":
    unittest.main()
