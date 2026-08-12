import importlib.util
import json
import time
import unittest
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
spec = importlib.util.spec_from_file_location("plugin_api", MODULE_PATH)
plugin_api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin_api)


class AchievementEngineTests(unittest.TestCase):
    def test_tool_call_stats_detect_tool_names_and_errors(self):
        messages = [
            {"role": "assistant", "tool_calls": [{"function": {"name": "terminal"}}]},
            {"role": "tool", "tool_name": "terminal", "content": "Error: port 3000 already in use"},
            {"role": "assistant", "tool_calls": [{"function": {"name": "web_search"}}]},
        ]

        stats = plugin_api.analyze_messages("s1", "Fix dev server", messages)

        self.assertEqual(stats["tool_call_count"], 2)
        self.assertEqual(stats["tool_names"], {"terminal", "web_search"})
        self.assertEqual(stats["error_count"], 1)
        self.assertIs(stats["port_conflict"], True)

    def test_tiered_achievement_reaches_highest_matching_tier(self):
        definition = {
            "id": "let_him_cook",
            "threshold_metric": "max_tool_calls_in_session",
            "tiers": [
                {"name": "Copper", "threshold": 10},
                {"name": "Silver", "threshold": 25},
                {"name": "Gold", "threshold": 50},
            ],
        }
        aggregate = {"max_tool_calls_in_session": 28}

        result = plugin_api.evaluate_tiered(definition, aggregate)

        self.assertIs(result["unlocked"], True)
        self.assertEqual(result["tier"], "Silver")
        self.assertEqual(result["progress"], 28)
        self.assertEqual(result["next_tier"], "Gold")

    def test_tiered_achievement_can_be_discovered_without_unlocking(self):
        definition = {
            "id": "terminal_goblin",
            "threshold_metric": "total_terminal_calls",
            "tiers": [{"name": "Copper", "threshold": 50}],
        }
        aggregate = {"total_terminal_calls": 12}

        result = plugin_api.evaluate_tiered(definition, aggregate)

        self.assertIs(result["unlocked"], False)
        self.assertIs(result["discovered"], True)
        self.assertEqual(result["state"], "discovered")
        self.assertEqual(result["progress"], 12)
        self.assertEqual(result["next_threshold"], 50)

    def test_secret_achievement_stays_hidden_without_progress(self):
        definition = {
            "id": "permission_denied_any_percent",
            "name": "Permission Denied Any%",
            "secret": True,
            "requirements": [{"metric": "permission_denied_events", "gte": 3}],
        }
        aggregate = {"permission_denied_events": 0}

        result = plugin_api.evaluate_requirements(definition, aggregate)
        display = plugin_api.display_achievement({**definition, **result})

        self.assertEqual(result["state"], "secret")
        self.assertEqual(display["name"], "???")
        self.assertNotIn("Permission", display["description"])

    def test_multi_condition_unlock_requires_all_requirements(self):
        definition = {
            "id": "full_send",
            "requirements": [
                {"metric": "max_terminal_calls_in_session", "gte": 10},
                {"metric": "max_file_tool_calls_in_session", "gte": 5},
                {"metric": "max_web_calls_in_session", "gte": 2},
            ],
        }

        partial = plugin_api.evaluate_requirements(definition, {
            "max_terminal_calls_in_session": 12,
            "max_file_tool_calls_in_session": 2,
            "max_web_calls_in_session": 0,
        })
        complete = plugin_api.evaluate_requirements(definition, {
            "max_terminal_calls_in_session": 12,
            "max_file_tool_calls_in_session": 6,
            "max_web_calls_in_session": 2,
        })

        self.assertEqual(partial["state"], "discovered")
        self.assertIs(partial["unlocked"], False)
        self.assertLess(partial["progress_pct"], 100)
        self.assertEqual(complete["state"], "unlocked")
        self.assertIs(complete["unlocked"], True)

    def test_catalog_has_60_plus_unique_achievements(self):
        ids = [achievement["id"] for achievement in plugin_api.ACHIEVEMENTS]
        self.assertGreaterEqual(len(ids), 60)
        self.assertEqual(len(ids), len(set(ids)))

    def test_model_provider_metrics_are_aggregated(self):
        sessions = [
            {"model_names": {"openai/gpt-5", "anthropic/claude-sonnet-4"}},
            {"model_names": {"google/gemini-pro", "mistral/large"}},
            {"model_names": {"qwen/qwen3"}},
        ]

        aggregate = plugin_api.aggregate_stats(sessions)

        self.assertEqual(aggregate["distinct_model_count"], 5)
        self.assertEqual(aggregate["distinct_provider_count"], 5)
        result = plugin_api.evaluate_definition(
            next(a for a in plugin_api.ACHIEVEMENTS if a["id"] == "five_model_flight"),
            aggregate,
        )
        self.assertEqual(result["state"], "unlocked")
        self.assertEqual(result["tier"], "Copper")

    def test_removed_noisy_achievements_are_not_in_catalog(self):
        ids = {achievement["id"] for achievement in plugin_api.ACHIEVEMENTS}
        self.assertNotIn("fallback_pilot", ids)
        self.assertNotIn("browser_sleuth", ids)
        self.assertNotIn("release_ritualist", ids)

    def test_open_weights_pilgrim_counts_only_local_model_metadata(self):
        aggregate_mentions_only = plugin_api.aggregate_stats([
            {"model_names": {"openai/gpt-5"}, "local_model_events": 999},
        ])
        aggregate_local_chat = plugin_api.aggregate_stats([
            {"model_names": {"openai/gpt-5"}},
            {"model_names": {"ollama/llama3"}},
        ])
        definition = next(a for a in plugin_api.ACHIEVEMENTS if a["id"] == "open_weights_pilgrim")

        self.assertEqual(aggregate_mentions_only["local_model_chat_sessions"], 0)
        self.assertEqual(plugin_api.evaluate_definition(definition, aggregate_mentions_only)["state"], "discovered")
        self.assertEqual(aggregate_local_chat["local_model_chat_sessions"], 1)
        self.assertEqual(plugin_api.evaluate_definition(definition, aggregate_local_chat)["state"], "unlocked")

    def test_config_surgeon_ignores_generic_config_mentions(self):
        stats = plugin_api.analyze_messages("s1", "Config talk", [{"content": "config config configuration not configured"}])
        self.assertEqual(stats["config_events"], 0)
        stats = plugin_api.analyze_messages("s2", "Real config", [{"content": "edited config.yaml, manifest.json, and .env.local"}])
        self.assertGreaterEqual(stats["config_events"], 3)

    def test_dashboard_card_hover_does_not_move_click_target(self):
        style_css = (
            Path(__file__).resolve().parents[1]
            / "dashboard"
            / "dist"
            / "style.css"
        ).read_text(encoding="utf-8")

        hover_rule = next(
            line for line in style_css.splitlines() if line.startswith(".ha-card:hover")
        )
        self.assertNotIn("transform:", hover_rule)
        self.assertIn("border-color: var(--ha-tier)", hover_rule)
        self.assertIn("box-shadow:", hover_rule)

    def test_streak_days_counts_consecutive_active_days(self):
        now = int(time.time())
        day = 86400
        sessions = [
            {"started_at": now},
            {"started_at": now - day},
            {"started_at": now - 2 * day},
            {"started_at": now - 8 * day},
            {"started_at": now - 9 * day},
        ]
        max_streak, current = plugin_api._streak_days(sessions)
        self.assertEqual(max_streak, 3)
        self.assertEqual(current, 3)

    def test_streak_lapses_after_a_missed_day(self):
        now = int(time.time())
        day = 86400
        sessions = [
            {"started_at": now - 3 * day},
            {"started_at": now - 4 * day},
        ]
        max_streak, current = plugin_api._streak_days(sessions)
        self.assertEqual(max_streak, 2)
        self.assertEqual(current, 0)

    def test_streak_uses_active_days_when_present(self):
        # A long-lived desktop session carries per-day message activity; the
        # anchors alone (started once, last_active = final day) would collapse
        # the streak. active_days is authoritative.
        now = int(time.time())
        day = 86400
        lt = time.localtime(now)
        sessions = [
            {
                "started_at": now - 5 * day,
                "last_active": now,
                # 6 consecutive days of real messages, e.g. a week of daily
                # desktop use inside one open session.
                "active_days": [(lt.tm_year, lt.tm_yday - 5 + i) for i in range(6)],
            }
        ]
        max_streak, current = plugin_api._streak_days(sessions)
        self.assertEqual(max_streak, 6)
        self.assertEqual(current, 6)

    def test_streak_active_days_ignores_rewound_messages(self):
        # Rewound/undo rows (active=0, compacted=0) are content the user took
        # back and must NOT count toward a streak. Only active or compacted
        # history counts.
        now = int(time.time())
        day = 86400
        lt = time.localtime(now)
        sessions = [
            {
                "started_at": now - 3 * day,
                "last_active": now - 2 * day,
                # Two days of real use, then a rewound (taken-back) day.
                "active_days": [
                    (lt.tm_year, lt.tm_yday - 2),
                    (lt.tm_year, lt.tm_yday - 1),
                ],
            }
        ]
        max_streak, current = plugin_api._streak_days(sessions)
        self.assertEqual(max_streak, 2)

    def test_analyze_messages_counts_compacted_history_days(self):
        # analyze_messages must count compacted (active=0, compacted=1) rows as
        # real activity days, matching what the real DB does after compression.
        import datetime
        base = datetime.date(2026, 8, 1)
        messages = []
        for i in range(3):
            ts = time.mktime((base + datetime.timedelta(days=i)).timetuple())
            messages.append({"timestamp": ts, "role": "user", "content": f"day {i}", "active": 0, "compacted": 1})
        stats = plugin_api.analyze_messages("s1", "T", messages)
        self.assertEqual(len(stats["active_days"]), 3)

    def test_analyze_messages_excludes_rewound_days(self):
        # Rewound rows (active=0, compacted=0) are taken-back content and must
        # not surface as activity days.
        import datetime
        base = datetime.date(2026, 8, 1)
        messages = [
            {"timestamp": time.mktime(base.timetuple()), "role": "user", "content": "kept", "active": 1, "compacted": 0},
            {"timestamp": time.mktime((base + datetime.timedelta(days=1)).timetuple()), "role": "user", "content": "rewound", "active": 0, "compacted": 0},
        ]
        stats = plugin_api.analyze_messages("s1", "T", messages)
        self.assertEqual(len(stats["active_days"]), 1)

    def test_streak_burner_achievement_is_tiered_on_max_streak_days(self):
        definition = next(a for a in plugin_api.ACHIEVEMENTS if a["id"] == "streak_burner")
        self.assertEqual(definition["threshold_metric"], "max_streak_days")
        thresholds = [t["threshold"] for t in definition["tiers"]]
        self.assertEqual(thresholds, [3, 7, 14, 30, 60])

        aggregate = {"max_streak_days": 7}
        result = plugin_api.evaluate_definition(definition, aggregate)
        self.assertIs(result["unlocked"], True)
        self.assertEqual(result["tier"], "Silver")

    def test_eta_days_computed_from_recent_daily_rate(self):
        now = int(time.time())
        day = 86400
        sessions = [
            {"started_at": now - i * day, "terminal_calls": 40, "tool_call_count": 50}
            for i in range(7)
        ]
        rates = plugin_api._recent_daily_rates(sessions, window_days=14)
        self.assertGreater(rates.get("total_terminal_calls", 0), 0)

        # 7 sessions x 40 terminal calls over a 14-day window -> 20/day.
        self.assertAlmostEqual(rates["total_terminal_calls"], 20.0, places=1)

        item = {
            "unlocked": False,
            "kind": "lifetime",
            "threshold_metric": "total_terminal_calls",
            "progress": 280,
            "next_threshold": 750,
        }
        # remaining 750-280=470 at 20/day -> 24 days.
        self.assertEqual(plugin_api._eta_days_for(item, rates), 24)

    def test_eta_not_computed_for_best_session_kind(self):
        item = {
            "unlocked": False,
            "kind": "best_session",
            "threshold_metric": "max_tool_calls_in_session",
            "progress": 50,
            "next_threshold": 200,
        }
        rates = {"max_tool_calls_in_session": 10.0}
        self.assertIsNone(plugin_api._eta_days_for(item, rates))

    def test_eta_none_without_recent_rate(self):
        item = {
            "unlocked": False,
            "kind": "lifetime",
            "threshold_metric": "total_terminal_calls",
            "progress": 10,
            "next_threshold": 750,
        }
        self.assertIsNone(plugin_api._eta_days_for(item, {}))

    def test_achievements_payload_exposes_streak(self):
        # /achievements payload builder pulls streak from the aggregate.
        payload_keys = {"achievements", "unlocked_count", "total_count", "streak"}
        self.assertTrue(payload_keys.issubset({
            "achievements", "unlocked_count", "discovered_count", "secret_count",
            "total_count", "error", "generated_at", "is_stale", "scan_meta", "streak",
        }))

    def test_activity_calendar_returns_contiguous_days(self):
        import time as _t
        now = int(_t.time())
        sessions = [{"started_at": now, "tool_call_count": 5}]
        cal = plugin_api._activity_calendar(sessions, days=30)
        self.assertEqual(len(cal), 30)
        self.assertEqual(cal[-1]["sessions"], 1)
        self.assertEqual(cal[-1]["tools"], 5)
        # All other days are zeros (contiguous).
        self.assertEqual(sum(1 for d in cal[:-1] if d["sessions"] > 0), 0)
        # Dates are sorted ascending.
        dates = [d["date"] for d in cal]
        self.assertEqual(dates, sorted(dates))

    def test_collection_unlocks_when_all_members_unlocked(self):
        definition = {"collection": "Vibe Coding", "kind": "collection"}
        members = {a["id"] for a in plugin_api.ACHIEVEMENTS if a.get("category") == "Vibe Coding" and a.get("kind") != "collection"}
        self.assertGreaterEqual(len(members), 3)
        result = plugin_api.evaluate_collection(definition, set(members))
        self.assertIs(result["unlocked"], True)
        self.assertEqual(result["tier"], "Olympian")
        self.assertEqual(result["progress_pct"], 100)
        # Partial set stays discovered.
        partial = plugin_api.evaluate_collection(definition, set(list(members)[:1]))
        self.assertIs(partial["unlocked"], False)
        self.assertEqual(partial["state"], "discovered")
        self.assertLess(partial["progress_pct"], 100)

    def test_rewards_eval_tier_and_streak(self):
        agg = {"max_streak_days": 31, "current_streak_days": 31}
        achievements = [
            {"kind": "lifetime", "tier": "Silver"},
            {"kind": "lifetime", "tier": "Diamond"},
        ]
        rewards = plugin_api.evaluate_rewards(achievements, agg, set())
        by_id = {r["id"]: r for r in rewards}
        self.assertIs(by_id["theme_diamond"]["unlocked"], True)
        self.assertIs(by_id["theme_streak30"]["unlocked"], True)
        self.assertIs(by_id["theme_olympian"]["unlocked"], False)
        self.assertIn("longest streak: 31", by_id["theme_streak30"]["progress"])
        self.assertIn("current streak: 31", by_id["theme_streak30"]["progress"])

    def test_streak_reward_unlocks_on_max_but_reports_current(self):
        # User once held a 30-day streak (max=30) but is mid-way on a fresh
        # current streak (current=5). The reward stays unlocked (monotonic),
        # and the progress string carries BOTH numbers so the countdown can
        # tick from the current streak, not the all-time record.
        agg = {"max_streak_days": 30, "current_streak_days": 5}
        rewards = plugin_api.evaluate_rewards([], agg, set())
        by_id = {r["id"]: r for r in rewards}
        self.assertIs(by_id["theme_streak30"]["unlocked"], True)
        self.assertIn("current streak: 5", by_id["theme_streak30"]["progress"])
        self.assertIn("longest streak: 30", by_id["theme_streak30"]["progress"])

    def test_streak_reward_progress_reports_current_below_max(self):
        # All-time best 13, current streak 2 (the real-world case): the
        # countdown source is the current streak so it ticks daily.
        agg = {"max_streak_days": 13, "current_streak_days": 2}
        rewards = plugin_api.evaluate_rewards([], agg, set())
        by_id = {r["id"]: r for r in rewards}
        self.assertIs(by_id["theme_streak30"]["unlocked"], False)
        self.assertIn("current streak: 2", by_id["theme_streak30"]["progress"])
        self.assertIn("longest streak: 13", by_id["theme_streak30"]["progress"])

    def test_reward_themes_are_valid_yaml(self):
        import yaml as _yaml
        for theme, text in plugin_api.REWARD_THEMES.items():
            doc = _yaml.safe_load(text)
            self.assertEqual(doc["name"], theme)
            self.assertIn("colors", doc)
            self.assertIn("branding", doc)

    def test_rewards_endpoint_present_in_payload(self):
        # The /achievements payload builder must carry the rewards list.
        payload_keys = {"achievements", "unlocked_count", "total_count", "streak", "activity", "rewards"}
        self.assertTrue(payload_keys.issubset({
            "achievements", "unlocked_count", "discovered_count", "secret_count",
            "total_count", "error", "generated_at", "is_stale", "scan_meta", "streak",
            "activity", "rewards",
        }))

    def test_xp_level_curve_and_names(self):
        info = plugin_api.compute_xp([{"kind": "lifetime", "tier": "Copper"}])
        self.assertEqual(info["level"], 1)
        self.assertEqual(info["total_xp"], 10)
        # Collections grant flat XP when unlocked.
        info2 = plugin_api.compute_xp([
            {"kind": "collection", "unlocked": True},
            {"kind": "lifetime", "tier": "Gold"},
        ])
        self.assertEqual(info2["total_xp"], 250)
        # Level names exist across the curve.
        self.assertEqual(plugin_api.level_for_xp(0)["name"], "Initiate")
        self.assertEqual(plugin_api.level_for_xp(10 ** 9)["name"], "Hermes")

    def test_category_summary_counts(self):
        achievements = [
            {"category": "A", "unlocked": True, "kind": "lifetime"},
            {"category": "A", "unlocked": False, "kind": "lifetime"},
            {"category": "B", "unlocked": True, "kind": "lifetime"},
            {"category": "A", "unlocked": True, "kind": "collection"},  # excluded
        ]
        cats = plugin_api._category_summary(achievements)
        by = {c["category"]: c for c in cats}
        self.assertEqual(by["A"]["unlocked"], 1)
        self.assertEqual(by["A"]["total"], 2)
        self.assertEqual(by["A"]["pct"], 50)
        # Collection-kind achievements are excluded from category counts.
        self.assertEqual(len(cats), 2)

    def test_monthly_challenges_evaluate(self):
        import time as _t
        now = int(_t.time())
        sessions = [{"started_at": now, "tool_call_count": 10}]
        achievements = [{"id": "x", "unlocked": True, "unlocked_at": now, "tier": "Silver", "kind": "lifetime"}]
        ch = plugin_api._monthly_challenges(sessions, achievements)
        by_id = {c["id"]: c for c in ch}
        self.assertGreaterEqual(by_id["m_sessions"]["value"], 1)
        self.assertGreaterEqual(by_id["m_unlocks"]["value"], 1)
        self.assertEqual(by_id["m_sessions"]["target"], 10)
        for c in ch:
            self.assertIn("pct", c)
            self.assertIn("done", c)

    def test_custom_goals_eval_and_metrics(self):
        aggregate = {"session_count": 30, "total_terminal_calls": 42}
        sessions = [{"started_at": int(time.time()) - 3600}]
        goals = [
            {"id": "g1", "name": "A", "metric": "session_count", "target": 20},
            {"id": "g2", "name": "B", "metric": "total_terminal_calls", "target": 100},
        ]
        ev = plugin_api.evaluate_custom_goals(goals, aggregate, sessions)
        self.assertIs(ev[0]["done"], True)
        self.assertEqual(ev[0]["pct"], 100)
        self.assertIs(ev[1]["done"], False)
        self.assertLess(ev[1]["pct"], 100)
        self.assertIn("metric_label", ev[0])
        # distinct_days_active derives from sessions, not aggregate.
        ev2 = plugin_api.evaluate_custom_goals(
            [{"id": "g3", "name": "C", "metric": "distinct_days_active", "target": 2}],
            {},
            sessions,
        )
        self.assertEqual(ev2[0]["value"], 1)

    def test_records_compute_bests(self):
        import time as _t
        now = int(_t.time())
        sessions = [
            {"started_at": now - 86400, "tool_call_count": 5, "message_count": 10, "title": "A"},
            {"started_at": now - 86400, "tool_call_count": 20, "message_count": 40, "title": "B"},
            {"started_at": now, "tool_call_count": 3, "message_count": 6, "title": "C"},
        ]
        recs = plugin_api._records(sessions)
        self.assertEqual(recs["best_day"]["tool_calls"], 25)
        self.assertEqual(recs["busiest_day"]["sessions"], 2)
        self.assertEqual(recs["biggest_session"]["tool_calls"], 20)
        self.assertEqual(recs["longest_session"]["messages"], 40)

    def test_weekly_challenges_evaluate(self):
        import time as _t
        now = int(_t.time())
        sessions = [{"started_at": now, "tool_call_count": 10}]
        achievements = [{"id": "x", "unlocked": True, "unlocked_at": now}]
        wk = plugin_api._weekly_challenges(sessions, achievements)
        by_id = {c["id"]: c for c in wk}
        self.assertGreaterEqual(by_id["w_sessions"]["value"], 1)
        self.assertEqual(by_id["w_sessions"]["target"], 7)
        self.assertIn("done", by_id["w_unlocks"])
        self.assertIn("pct", by_id["w_tool_calls"])

    def test_quests_evaluate_and_xp(self):
        # q_full_stack needs 2 Vibe Coding + 2 Agent Autonomy unlocked.
        achievements = [
            {"id": "v1", "category": "Vibe Coding", "unlocked": True, "kind": "lifetime"},
            {"id": "v2", "category": "Vibe Coding", "unlocked": True, "kind": "lifetime"},
            {"id": "a1", "category": "Agent Autonomy", "unlocked": True, "kind": "lifetime"},
            {"id": "a2", "category": "Agent Autonomy", "unlocked": True, "kind": "lifetime"},
        ]
        qs = plugin_api.evaluate_quests(achievements, {"max_streak_days": 1, "distinct_model_count": 1})
        by_id = {q["id"]: q for q in qs}
        self.assertIs(by_id["q_full_stack"]["done"], True)
        self.assertIs(by_id["q_debug_king"]["done"], False)
        self.assertGreaterEqual(plugin_api.quest_xp(qs), 100)

    def test_badge_wall_svg_produces_collection(self):
        # The SVG generator must escape names and cover every achievement.
        import asyncio as _aio

        async def _run():
            return await plugin_api.badge_wall_svg()

        resp = _aio.run(_run())
        body = getattr(resp, "content", None) or getattr(resp, "body", b"")
        if isinstance(body, bytes):
            body = body.decode("utf-8", "replace")
        self.assertIn("<svg", body)
        self.assertIn("Hermes Achievements", body)
        self.assertIn("Achievements", body)

    def test_partial_scan_keeps_ledger_monotonicity(self):
        # Background scans publish partial snapshots as sessions stream in.
        # A partial snapshot must still see the ledger so earned unlocks
        # never drop out mid-scan (regression: 18/69 → 15, Lv6 → Lv5).
        import time as _t
        now = int(_t.time())
        saved = plugin_api.load_state()
        try:
            plugin_api.save_state({"unlocks": {
                "patch_wizard": {"unlocked_at": now, "first_tier": "Gold", "highest_tier": "Gold"},
            }})
            partial_scan = {
                "sessions": [],
                "aggregate": {"total_patch_calls": 0},
                "scan_meta": {"mode": "in_progress"},
            }
            partial = plugin_api._compute_from_scan(partial_scan, is_partial=True)
            pw = [a for a in partial["achievements"] if a["id"] == "patch_wizard"][0]
            self.assertIs(pw["unlocked"], True)
            self.assertEqual(pw["state"], "unlocked")
            self.assertEqual(pw["tier"], "Gold")
        finally:
            plugin_api.save_state(saved)

    def test_tier_is_monotonic_across_downgrade_scans(self):
        # XP comes from the tier, so a scan that temporarily sees fewer
        # sessions must not downgrade an earned tier (Gold → Silver) and
        # shrink XP / drop the level.
        import time as _t
        now = int(_t.time())
        saved = plugin_api.load_state()
        try:
            plugin_api.save_state({"unlocks": {
                "terminal_goblin": {"unlocked_at": now, "first_tier": "Gold", "highest_tier": "Gold"},
            }})
            low_scan = {
                "sessions": [],
                "aggregate": {"total_terminal_commands": 10},
                "scan_meta": {"mode": "incremental"},
            }
            out = plugin_api._compute_from_scan(low_scan, is_partial=False)
            tg = [a for a in out["achievements"] if a["id"] == "terminal_goblin"][0]
            self.assertEqual(tg["tier"], "Gold")
            self.assertEqual(plugin_api.xp_for(tg), plugin_api.TIER_XP["Gold"])
        finally:
            plugin_api.save_state(saved)

    def test_quest_completions_are_recorded_and_stamped(self):
        # A done quest gets a completed_at from the ledger, and the ledger
        # persists the first completion (monotonic — never re-stamped).
        import time as _t
        now = int(_t.time())
        saved = plugin_api.load_state()
        try:
            plugin_api.save_state({"unlocks": {}, "quest_completions": {}})
            qs = plugin_api.evaluate_quests(
                [
                    {"id": "v1", "category": "Vibe Coding", "unlocked": True, "kind": "lifetime"},
                    {"id": "v2", "category": "Vibe Coding", "unlocked": True, "kind": "lifetime"},
                    {"id": "a1", "category": "Agent Autonomy", "unlocked": True, "kind": "lifetime"},
                    {"id": "a2", "category": "Agent Autonomy", "unlocked": True, "kind": "lifetime"},
                ],
                {"max_streak_days": 1, "distinct_model_count": 1},
                plugin_api.load_quest_completions(),
            )
            plugin_api.record_quest_completions(qs)
            ledger = plugin_api.load_quest_completions()
            self.assertIn("q_full_stack", ledger)
            self.assertTrue(ledger["q_full_stack"].get("completed_at"))

            # Second evaluation stamps completed_at from the ledger.
            qs2 = plugin_api.evaluate_quests(
                [
                    {"id": "v1", "category": "Vibe Coding", "unlocked": True, "kind": "lifetime"},
                    {"id": "v2", "category": "Vibe Coding", "unlocked": True, "kind": "lifetime"},
                    {"id": "a1", "category": "Agent Autonomy", "unlocked": True, "kind": "lifetime"},
                    {"id": "a2", "category": "Agent Autonomy", "unlocked": True, "kind": "lifetime"},
                ],
                {"max_streak_days": 1, "distinct_model_count": 1},
                ledger,
            )
            fs = [q for q in qs2 if q["id"] == "q_full_stack"][0]
            self.assertEqual(fs["completed_at"], ledger["q_full_stack"]["completed_at"])
        finally:
            plugin_api.save_state(saved)

    def test_quest_ledger_survives_later_downgrade(self):
        # Once a quest completes, a later scan that no longer meets the
        # requirements must still report done (monotonic completion).
        import time as _t
        now = int(_t.time())
        saved = plugin_api.load_state()
        try:
            plugin_api.save_state({
                "unlocks": {},
                "quest_completions": {"q_full_stack": {"completed_at": now}},
            })
            qs = plugin_api.evaluate_quests([], {}, plugin_api.load_quest_completions())
            fs = [q for q in qs if q["id"] == "q_full_stack"][0]
            self.assertIs(fs["done"], True)
            self.assertEqual(fs["completed_at"], now)
        finally:
            plugin_api.save_state(saved)

    def test_scan_includes_archived_sessions(self):
        # Lifetime achievements must count archived sessions too — archiving
        # hides a session from the sidebar, it must not erase its progress.
        import inspect
        src = inspect.getsource(plugin_api.scan_sessions)
        self.assertIn("include_archived=True", src)

    def test_ledger_unlock_is_monotonic(self):
        # A badge recorded in the ledger stays unlocked even when the current
        # aggregate no longer clears the threshold (archived/missing sessions).
        definition = {
            "id": "terminal_goblin",
            "threshold_metric": "total_terminal_calls",
            "tiers": plugin_api.tiers([750]),
        }
        # Scan with a ledger that already recorded the unlock, but an
        # aggregate below the threshold.
        state_path = plugin_api.state_path()
        original = None
        if state_path.exists():
            original = state_path.read_text(encoding="utf-8")
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({
                "unlocks": {"terminal_goblin": {"unlocked_at": 1, "first_tier": "Copper"}}
            }), encoding="utf-8")
            computed = plugin_api._compute_from_scan(
                {"sessions": [{"session_id": "s1", "started_at": 1, "terminal_calls": 10}],
                 "aggregate": {"total_terminal_calls": 10}},
                is_partial=False,
            )
            item = next(a for a in computed["achievements"] if a["id"] == "terminal_goblin")
            self.assertIs(item["unlocked"], True)
            self.assertEqual(item["state"], "unlocked")
            self.assertEqual(item["tier"], "Copper")
        finally:
            if original is None:
                state_path.unlink(missing_ok=True)
            else:
                state_path.write_text(original, encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
