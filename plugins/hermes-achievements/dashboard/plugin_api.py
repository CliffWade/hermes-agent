"""Hermes Achievements dashboard plugin backend.

Mounted at /api/plugins/hermes-achievements/ by Hermes dashboard.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

try:
    from hermes_constants import get_hermes_home
except ImportError:
    import os as _os
    def get_hermes_home() -> Path:  # type: ignore[misc]
        val = (_os.environ.get("HERMES_HOME") or "").strip()
        return Path(val) if val else Path.home() / ".hermes"

try:
    from fastapi import APIRouter, Response
except Exception:  # Allows local unit tests without dashboard dependencies.
    class APIRouter:  # type: ignore
        def get(self, *_args, **_kwargs):
            return lambda fn: fn
        def post(self, *_args, **_kwargs):
            return lambda fn: fn
        def delete(self, *_args, **_kwargs):
            return lambda fn: fn
    class Response:  # type: ignore
        def __init__(self, content: str = "", media_type: str = ""):
            self.content = content
            self.media_type = media_type

router = APIRouter()

SNAPSHOT_TTL_SECONDS = 120
_SCAN_LOCK = threading.Lock()
_SNAPSHOT_CACHE: Optional[Dict[str, Any]] = None
_SNAPSHOT_CACHE_AT = 0
_SCAN_STATUS: Dict[str, Any] = {
    "state": "idle",
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "last_duration_ms": None,
    "run_count": 0,
}

ERROR_RE = re.compile(r"\b(error|failed|failure|traceback|exception|permission denied|not found|eaddrinuse|already in use|timed out|blocked)\b", re.I)
PORT_RE = re.compile(r"\b(port\s+)?(3000|5173|8000|8080|9119)\b.*\b(in use|already|taken|eaddrinuse)\b|\beaddrinuse\b", re.I)
INSTALL_RE = re.compile(r"\b(npm|pnpm|yarn|pip|uv)\b.*\b(install|add)\b", re.I)
SUCCESS_RE = re.compile(r"\b(success|passed|built|compiled|done|exit_code[\"']?\s*[:=]\s*0|verified|ok)\b", re.I)
FILE_RE = re.compile(r"(?:/home/|~/?|\./|/mnt/)[\w./-]+\.(?:py|js|ts|tsx|jsx|css|html|md|json|yaml|yml|svg|sql|sh)")

TIER_NAMES = ["Copper", "Silver", "Gold", "Diamond", "Olympian"]


def tiers(values: List[int]) -> List[Dict[str, Any]]:
    return [{"name": name, "threshold": threshold} for name, threshold in zip(TIER_NAMES, values)]


def req(metric: str, gte: int) -> Dict[str, Any]:
    return {"metric": metric, "gte": gte}


ACHIEVEMENTS: List[Dict[str, Any]] = [
    # Agent Autonomy — mostly best-session feats
    {"id": "let_him_cook", "name": "Let Him Cook", "description": "Let Hermes run a serious autonomous tool chain in one session.", "category": "Agent Autonomy", "kind": "best_session", "icon": "flame", "threshold_metric": "max_tool_calls_in_session", "tiers": tiers([200, 500, 1200, 3000, 8000])},
    {"id": "autonomous_avalanche", "name": "Autonomous Avalanche", "description": "Accumulate a lifetime avalanche of Hermes tool calls across sessions.", "category": "Agent Autonomy", "kind": "lifetime", "icon": "avalanche", "threshold_metric": "total_tool_calls", "tiers": tiers([1000, 3000, 8000, 20000, 50000])},
    {"id": "toolchain_maxxer", "name": "Toolchain Maxxer", "description": "Use a wide spread of distinct Hermes tools in one session.", "category": "Agent Autonomy", "kind": "best_session", "icon": "nodes", "threshold_metric": "max_distinct_tools_in_session", "tiers": tiers([18, 28, 45, 70, 100])},
    {"id": "full_send", "name": "Full Send", "description": "Terminal, files, and web/browser all get involved in one real run.", "category": "Agent Autonomy", "kind": "multi_condition", "icon": "rocket", "requirements": [req("max_terminal_calls_in_session", 180), req("max_file_tool_calls_in_session", 120), req("max_web_browser_calls_in_session", 60)]},
    {"id": "subagent_commander", "name": "Subagent Commander", "description": "Coordinate delegated agent work.", "category": "Agent Autonomy", "kind": "lifetime", "icon": "branch", "threshold_metric": "total_delegate_calls", "tiers": tiers([5, 40, 100, 1000, 5000])},
    {"id": "background_process_enjoyer", "name": "Background Process Enjoyer", "description": "Start or control enough long-running processes to deserve the title.", "category": "Agent Autonomy", "kind": "lifetime", "icon": "daemon", "threshold_metric": "total_process_calls", "tiers": tiers([300, 800, 2000, 6000, 15000])},
    {"id": "cron_necromancer", "name": "Cron Necromancer", "description": "Raise scheduled autonomous jobs from the dead.", "category": "Agent Autonomy", "kind": "lifetime", "icon": "clock", "threshold_metric": "total_cron_calls", "tiers": tiers([1000, 3000, 8000, 20000, 50000])},

    # Debugging Chaos — higher thresholds + multi-condition events
    {"id": "red_text_connoisseur", "name": "Red Text Connoisseur", "description": "Encounter enough errors to develop a palate for red text.", "category": "Debugging Chaos", "kind": "lifetime", "icon": "warning", "threshold_metric": "total_errors", "tiers": tiers([1500, 4000, 10000, 25000, 75000])},
    {"id": "stack_trace_sommelier", "name": "Stack Trace Sommelier", "description": "Taste tracebacks by the flight, not by the sip.", "category": "Debugging Chaos", "kind": "lifetime", "icon": "wine", "threshold_metric": "traceback_events", "tiers": tiers([300, 1000, 3000, 8000, 20000])},
    {"id": "actually_read_the_logs", "name": "Actually Read The Logs", "description": "Inspect logs repeatedly instead of guessing.", "category": "Debugging Chaos", "kind": "lifetime", "icon": "scroll", "threshold_metric": "log_read_events", "tiers": tiers([1000, 3000, 8000, 20000, 50000])},
    {"id": "port_3000_taken", "name": "Port 3000 Is Taken", "description": "Discover dev-server port conflict patterns enough times to become numb.", "category": "Debugging Chaos", "kind": "lifetime", "icon": "plug", "secret": True, "threshold_metric": "port_conflict_events", "tiers": tiers([15, 40, 100, 300, 1000])},
    {"id": "permission_denied_any_percent", "name": "Permission Denied Any%", "description": "Speedrun into permission walls.", "category": "Debugging Chaos", "kind": "lifetime", "icon": "lock", "secret": True, "threshold_metric": "permission_denied_events", "tiers": tiers([25, 75, 200, 600, 1500])},
    {"id": "dependency_hell_tourist", "name": "Dependency Hell Tourist", "description": "Package installs fail, then somehow life continues.", "category": "Debugging Chaos", "kind": "multi_condition", "icon": "package_skull", "requirements": [req("install_error_events", 25), req("install_success_events", 10)]},
    {"id": "the_fix_was_restarting", "name": "The Fix Was Restarting It", "description": "Restart after enough error clusters to call it a technique.", "category": "Debugging Chaos", "kind": "multi_condition", "icon": "restart", "requirements": [req("restart_after_error_events", 50), req("total_errors", 4000)]},
    {"id": "forgot_the_env_var", "name": "Forgot The Env Var", "description": "Auth or configuration failed because an environment variable was missing.", "category": "Debugging Chaos", "kind": "lifetime", "icon": "key", "secret": True, "threshold_metric": "env_var_error_events", "tiers": tiers([5000, 15000, 40000, 100000, 250000])},
    {"id": "yaml_colon_incident", "name": "YAML Colon Incident", "description": "Configuration syntax bites back.", "category": "Debugging Chaos", "kind": "lifetime", "icon": "colon", "secret": True, "threshold_metric": "yaml_error_events", "tiers": tiers([1000, 3000, 8000, 20000, 50000])},
    {"id": "docker_name_collision", "name": "Docker Name Collision", "description": "A container name already exists. Of course it does.", "category": "Debugging Chaos", "kind": "lifetime", "icon": "container", "secret": True, "threshold_metric": "docker_conflict_events", "tiers": tiers([75, 200, 600, 1500, 4000])},

    # Vibe Coding
    {"id": "supposed_to_be_quick", "name": "This Was Supposed To Be Quick", "description": "A tiny ask becomes an entire expedition.", "category": "Vibe Coding", "kind": "best_session", "icon": "melting_clock", "threshold_metric": "max_messages_in_session", "tiers": tiers([300, 600, 1200, 2500, 6000])},
    {"id": "one_more_small_change", "name": "One More Small Change", "description": "Make enough file edits in one session to invalidate the phrase small change.", "category": "Vibe Coding", "kind": "best_session", "icon": "pencil", "threshold_metric": "max_file_tool_calls_in_session", "tiers": tiers([150, 400, 1000, 3000, 8000])},
    {"id": "vibe_architect", "name": "Vibe Architect", "description": "Touch a broad surface area in one project session.", "category": "Vibe Coding", "kind": "best_session", "icon": "blueprint", "threshold_metric": "max_files_touched_in_session", "tiers": tiers([300, 700, 1500, 4000, 10000])},
    {"id": "pixel_goblin", "name": "Pixel Goblin", "description": "Do sustained frontend, CSS, SVG, or visual tuning.", "category": "Vibe Coding", "kind": "lifetime", "icon": "pixel", "threshold_metric": "frontend_activity_events", "tiers": tiers([20000, 50000, 120000, 300000, 800000])},
    {"id": "ship_first_ask_later", "name": "Ship First, Ask Later", "description": "Git activity after a serious tool chain.", "category": "Vibe Coding", "kind": "multi_condition", "icon": "ship", "requirements": [req("git_events", 50), req("max_tool_calls_in_session", 500)]},
    {"id": "css_exorcist", "name": "CSS Exorcist", "description": "Cast repeated styling demons out of the interface.", "category": "Vibe Coding", "kind": "lifetime", "icon": "spark_cursor", "threshold_metric": "css_activity_events", "tiers": tiers([10000, 30000, 80000, 200000, 500000])},
    {"id": "one_character_fix", "name": "One Character Fix", "description": "A tiny edit after a pile of errors. Painful. Beautiful.", "category": "Vibe Coding", "kind": "multi_condition", "icon": "needle", "secret": True, "requirements": [req("tiny_patch_after_errors_events", 5), req("total_errors", 4000)]},

    # Hermes Native
    {"id": "skillsmith", "name": "Skillsmith", "description": "Work with Hermes skills enough to leave fingerprints.", "category": "Hermes Native", "kind": "lifetime", "icon": "hammer_scroll", "threshold_metric": "skill_events", "tiers": tiers([5000, 15000, 40000, 100000, 250000])},
    {"id": "skill_issue_skill_created", "name": "Skill Issue? Skill Created.", "description": "Create or patch durable procedures instead of repeating yourself.", "category": "Hermes Native", "kind": "lifetime", "icon": "anvil", "threshold_metric": "skill_manage_events", "tiers": tiers([25, 75, 200, 600, 1500])},
    {"id": "memory_keeper", "name": "Memory Keeper", "description": "Persist durable knowledge with memory or Mnemosyne.", "category": "Hermes Native", "kind": "lifetime", "icon": "crystal", "threshold_metric": "memory_events", "tiers": tiers([100, 300, 1000, 3000, 8000])},
    {"id": "memory_palace", "name": "Memory Palace", "description": "Build a serious durable-memory trail.", "category": "Hermes Native", "kind": "lifetime", "icon": "palace", "threshold_metric": "memory_write_events", "tiers": tiers([100, 300, 1000, 3000, 8000])},
    {"id": "context_dragon", "name": "Context Dragon", "description": "Brush against compression, huge context, or token pressure repeatedly.", "category": "Hermes Native", "kind": "lifetime", "icon": "dragon", "threshold_metric": "context_events", "tiers": tiers([5000, 15000, 40000, 100000, 250000])},
    {"id": "gateway_dweller", "name": "Gateway Dweller", "description": "Live through gateway-connected Hermes workflows.", "category": "Hermes Native", "kind": "lifetime", "icon": "antenna", "threshold_metric": "gateway_events", "tiers": tiers([5000, 15000, 40000, 100000, 250000])},
    {"id": "plugin_goblin", "name": "Plugin Goblin", "description": "Use or develop plugins enough that the dashboard notices.", "category": "Hermes Native", "kind": "lifetime", "icon": "puzzle", "threshold_metric": "plugin_events", "tiers": tiers([1000, 3000, 8000, 20000, 50000])},
    {"id": "rollback_wizard", "name": "Rollback Wizard", "description": "Invoke rollback/checkpoint recovery magic.", "category": "Hermes Native", "kind": "lifetime", "icon": "rewind", "secret": True, "threshold_metric": "rollback_events", "tiers": tiers([500, 1500, 4000, 10000, 25000])},

    # Research/Web
    {"id": "rabbit_hole_certified", "name": "Rabbit Hole Certified", "description": "Search or extract enough web content to qualify as a research spiral.", "category": "Research/Web", "kind": "lifetime", "icon": "spiral", "threshold_metric": "total_web_calls", "tiers": tiers([400, 1200, 3000, 8000, 20000])},
    {"id": "citation_goblin", "name": "Citation Goblin", "description": "Extract enough web pages to become a tiny librarian.", "category": "Research/Web", "kind": "lifetime", "icon": "quote", "threshold_metric": "total_web_extract_calls", "tiers": tiers([100, 300, 1000, 3000, 8000])},
    {"id": "docs_archaeologist", "name": "Docs Archaeologist", "description": "Dig through documentation sources over and over.", "category": "Research/Web", "kind": "lifetime", "icon": "compass", "threshold_metric": "docs_activity_events", "tiers": tiers([5000, 15000, 40000, 100000, 250000])},
    {"id": "browser_possession", "name": "Browser Possession", "description": "Possess a browser through automation repeatedly.", "category": "Research/Web", "kind": "lifetime", "icon": "browser", "threshold_metric": "browser_calls", "tiers": tiers([75, 200, 600, 1500, 4000])},

    # Tool Mastery
    {"id": "terminal_goblin", "name": "Terminal Goblin", "description": "Spend serious time in shell-land.", "category": "Tool Mastery", "kind": "lifetime", "icon": "terminal", "threshold_metric": "total_terminal_calls", "tiers": tiers([750, 2000, 6000, 15000, 50000])},
    {"id": "patch_wizard", "name": "Patch Wizard", "description": "Bend files to your will with targeted patches.", "category": "Tool Mastery", "kind": "lifetime", "icon": "wand", "threshold_metric": "total_patch_calls", "tiers": tiers([250, 750, 2000, 6000, 15000])},
    {"id": "file_archaeologist", "name": "File Archaeologist", "description": "Dig through the filesystem with reads and searches.", "category": "Tool Mastery", "kind": "lifetime", "icon": "folder", "threshold_metric": "total_file_reads_searches", "tiers": tiers([750, 2000, 6000, 15000, 50000])},
    {"id": "image_whisperer", "name": "Image Whisperer", "description": "Use image generation or vision tools enough for visual work.", "category": "Tool Mastery", "kind": "lifetime", "icon": "eye", "threshold_metric": "image_vision_calls", "tiers": tiers([100, 300, 1000, 3000, 8000])},
    {"id": "voice_of_the_machine", "name": "Voice Of The Machine", "description": "Use text-to-speech or voice tooling repeatedly.", "category": "Tool Mastery", "kind": "lifetime", "icon": "wave", "threshold_metric": "tts_calls", "tiers": tiers([10, 30, 100, 300, 800])},

    # Model Lore
    {"id": "model_hopper", "name": "Model Hopper", "description": "Switch or inspect providers/models enough to count as a habit.", "category": "Model Lore", "kind": "lifetime", "icon": "swap", "threshold_metric": "model_events", "tiers": tiers([10000, 30000, 80000, 200000, 500000])},
    {"id": "openrouter_enjoyer", "name": "OpenRouter Enjoyer", "description": "Route model work through OpenRouter repeatedly.", "category": "Model Lore", "kind": "lifetime", "icon": "router", "threshold_metric": "openrouter_events", "tiers": tiers([250, 750, 2000, 6000, 15000])},
    {"id": "codex_conjurer", "name": "Codex Conjurer", "description": "Summon Codex-flavored assistance often enough for a ritual.", "category": "Model Lore", "kind": "lifetime", "icon": "codex", "threshold_metric": "codex_events", "tiers": tiers([500, 1500, 4000, 10000, 25000])},
    {"id": "multi_model_mage", "name": "Multi-Model Mage", "description": "Use a real spread of distinct model names across Hermes history.", "category": "Model Lore", "kind": "lifetime", "icon": "prism", "threshold_metric": "distinct_model_count", "tiers": tiers([10, 20, 40, 80, 160])},
    {"id": "five_model_flight", "name": "Five-Model Flight", "description": "Try at least five distinct LLMs instead of marrying the first model that answers.", "category": "Model Lore", "kind": "lifetime", "icon": "prism", "threshold_metric": "distinct_model_count", "tiers": tiers([5, 10, 20, 40, 80])},
    {"id": "provider_polyglot", "name": "Provider Polyglot", "description": "Use models from multiple providers across Hermes history.", "category": "Model Lore", "kind": "lifetime", "icon": "swap", "threshold_metric": "distinct_provider_count", "tiers": tiers([2, 3, 5, 8, 12])},
    {"id": "model_sommelier", "name": "Model Sommelier", "description": "Taste enough model/provider conversations to develop preferences.", "category": "Model Lore", "kind": "lifetime", "icon": "wine", "threshold_metric": "model_events", "tiers": tiers([250, 750, 2000, 6000, 15000])},
    {"id": "claude_confidant", "name": "Claude Confidant", "description": "Bring Claude-flavored reasoning into the workflow repeatedly.", "category": "Model Lore", "kind": "lifetime", "icon": "quote", "threshold_metric": "claude_events", "tiers": tiers([50, 150, 500, 1500, 4000])},
    {"id": "gemini_cartographer", "name": "Gemini Cartographer", "description": "Map enough Gemini-related workflows to know the terrain.", "category": "Model Lore", "kind": "lifetime", "icon": "compass", "threshold_metric": "gemini_events", "tiers": tiers([50, 150, 500, 1500, 4000])},
    {"id": "open_weights_pilgrim", "name": "Open Weights Pilgrim", "description": "Actually chat with local/open-weight models through Hermes session metadata.", "category": "Model Lore", "kind": "lifetime", "icon": "terminal", "threshold_metric": "local_model_chat_sessions", "tiers": tiers([1, 3, 10, 30, 100])},

    # Workflow Intelligence
    {"id": "toolset_cartographer", "name": "Toolset Cartographer", "description": "Navigate Hermes toolsets deliberately instead of treating tools as a blur.", "category": "Hermes Native", "kind": "lifetime", "icon": "compass", "threshold_metric": "toolset_events", "tiers": tiers([20, 60, 200, 600, 1500])},
    {"id": "config_surgeon", "name": "Config Surgeon", "description": "Operate on real config files, manifests, env files, and dashboard settings without flinching.", "category": "Hermes Native", "kind": "lifetime", "icon": "key", "threshold_metric": "config_events", "tiers": tiers([100, 300, 1000, 3000, 10000])},
    {"id": "rebase_acrobat", "name": "Rebase Acrobat", "description": "Handle real git history surgery: rebase, conflict, merge, fetch, push.", "category": "Vibe Coding", "kind": "lifetime", "icon": "branch", "threshold_metric": "git_history_events", "tiers": tiers([10, 30, 100, 300, 800])},
    {"id": "test_suite_tamer", "name": "Test Suite Tamer", "description": "Run enough verification commands that green text becomes part of the ritual.", "category": "Tool Mastery", "kind": "lifetime", "icon": "daemon", "threshold_metric": "test_events", "tiers": tiers([100, 300, 800, 2400, 6000])},
    {"id": "screenshot_hunter", "name": "Screenshot Hunter", "description": "Capture, inspect, and polish visual proof instead of just claiming it works.", "category": "Tool Mastery", "kind": "lifetime", "icon": "eye", "threshold_metric": "screenshot_events", "tiers": tiers([50, 150, 500, 1500, 5000])},

    # Lifestyle
    {"id": "marathon_operator", "name": "Marathon Operator", "description": "Accumulate a serious number of Hermes sessions.", "category": "Lifestyle", "kind": "lifetime", "icon": "marathon", "threshold_metric": "session_count", "tiers": tiers([75, 200, 500, 1500, 5000])},
    {"id": "weekend_warrior", "name": "Weekend Warrior", "description": "Run Hermes on weekends enough times to make it a lifestyle.", "category": "Lifestyle", "kind": "lifetime", "icon": "calendar", "threshold_metric": "weekend_sessions", "tiers": tiers([25, 75, 200, 600, 1500])},
    {"id": "night_shift_operator", "name": "Night Shift Operator", "description": "Run sessions during gremlin hours repeatedly.", "category": "Lifestyle", "kind": "lifetime", "icon": "moon", "threshold_metric": "night_sessions", "tiers": tiers([25, 75, 200, 600, 1500])},
    {"id": "cache_hit_appreciator", "name": "Cache Hit Appreciator", "description": "Notice or benefit from prompt/cache behavior.", "category": "Lifestyle", "kind": "lifetime", "icon": "cache", "secret": True, "threshold_metric": "cache_events", "tiers": tiers([100, 300, 1000, 3000, 8000])},

    # Streaks — consecutive-day usage (computed from session dates)
    {"id": "streak_burner", "name": "Streak Burner", "description": "Keep Hermes lit on consecutive days — a streak is a habit.", "category": "Lifestyle", "kind": "lifetime", "icon": "flame", "threshold_metric": "max_streak_days", "tiers": tiers([3, 7, 14, 30, 60])},

    # Set collections — complete every achievement in a category
    {"id": "set_autonomy", "name": "Autonomy Complete", "description": "Unlock every Agent Autonomy achievement.", "category": "Sets", "kind": "collection", "icon": "trophy", "collection": "Agent Autonomy"},
    {"id": "set_debugging", "name": "Debugging Complete", "description": "Unlock every Debugging Chaos achievement.", "category": "Sets", "kind": "collection", "icon": "trophy", "collection": "Debugging Chaos"},
    {"id": "set_vibe", "name": "Vibe Complete", "description": "Unlock every Vibe Coding achievement.", "category": "Sets", "kind": "collection", "icon": "trophy", "collection": "Vibe Coding"},
    {"id": "set_hermes_native", "name": "Native Complete", "description": "Unlock every Hermes Native achievement.", "category": "Sets", "kind": "collection", "icon": "trophy", "collection": "Hermes Native"},
    {"id": "set_research", "name": "Research Complete", "description": "Unlock every Research/Web achievement.", "category": "Sets", "kind": "collection", "icon": "trophy", "collection": "Research/Web"},
    {"id": "set_tools", "name": "Tools Complete", "description": "Unlock every Tool Mastery achievement.", "category": "Sets", "kind": "collection", "icon": "trophy", "collection": "Tool Mastery"},
    {"id": "set_models", "name": "Models Complete", "description": "Unlock every Model Lore achievement.", "category": "Sets", "kind": "collection", "icon": "trophy", "collection": "Model Lore"},
    {"id": "set_lifestyle", "name": "Lifestyle Complete", "description": "Unlock every Lifestyle achievement.", "category": "Sets", "kind": "collection", "icon": "trophy", "collection": "Lifestyle"},
]


REWARDS: List[Dict[str, Any]] = [
    {
        "id": "theme_diamond",
        "name": "Diamond Theme",
        "description": "Reach Diamond tier on any achievement to unlock the exclusive Diamond theme.",
        "kind": "tier_reached",
        "tier": "Diamond",
        "theme": "reward-diamond",
    },
    {
        "id": "theme_streak30",
        "name": "Streak Theme",
        "description": "Hold a 30-day Hermes streak to unlock the exclusive Streak theme.",
        "kind": "streak",
        "streak_days": 30,
        "theme": "reward-streak",
    },
    {
        "id": "theme_olympian",
        "name": "Olympian Theme",
        "description": "Reach Olympian tier on any achievement to unlock the exclusive Olympian theme.",
        "kind": "tier_reached",
        "tier": "Olympian",
        "theme": "reward-olympian",
    },
    {
        "id": "theme_sets",
        "name": "Completionist Theme",
        "description": "Complete every set collection to unlock the exclusive Completionist theme.",
        "kind": "all_sets",
        "theme": "reward-completionist",
    },
]


def evaluate_rewards(achievements: List[Dict[str, Any]], aggregate: Dict[str, Any], unlocked_ids: Set[str]) -> List[Dict[str, Any]]:
    """Evaluate reward status from evaluated achievements + aggregate.

    Returns reward entries with ``unlocked`` and a human ``progress`` note.
    """
    tier_rank = {t: i for i, t in enumerate(TIER_NAMES)}
    max_tier = "None"
    max_rank = -1
    for a in achievements:
        t = a.get("tier")
        if not isinstance(t, str) or t not in tier_rank:
            continue
        r = tier_rank[t]
        if r > max_rank:
            max_rank = r
            max_tier = t

    sets = [a for a in achievements if a.get("kind") == "collection"]
    sets_done = sum(1 for s in sets if s.get("unlocked"))
    sets_total = len(sets)

    out = []
    for reward in REWARDS:
        kind = reward["kind"]
        unlocked = False
        progress = ""
        if kind == "tier_reached":
            target = reward["tier"]
            target_rank = tier_rank.get(target, 0)
            unlocked = max_rank >= target_rank
            progress = f"highest tier: {max_tier}" if max_tier != "None" else "no achievements yet"
        elif kind == "streak":
            streak = int(aggregate.get("max_streak_days") or 0)
            cur = int(aggregate.get("current_streak_days") or 0)
            unlocked = streak >= reward["streak_days"]
            progress = f"current streak: {cur} days · longest streak: {streak} days"
        elif kind == "all_sets":
            unlocked = sets_total > 0 and sets_done == sets_total
            progress = f"sets complete: {sets_done}/{sets_total}"
        out.append({**reward, "unlocked": unlocked, "progress": progress})
    return out


REWARD_THEMES: Dict[str, str] = {
    "reward-diamond": """# Exclusive reward: reach Diamond tier on any achievement.
name: reward-diamond
description: Diamond achievement reward. Icy depth, brilliant accents.
colors:
  background: "#0b1020"
  ui_accent: "#8ec5ff"
  banner_accent: "#8ec5ff"
  banner_title: "#e8f1ff"
  banner_text: "#d5e4ff"
  ui_text: "#d5e4ff"
  banner_dim: "#8ea3c9"
  banner_border: "#1c2a4a"
  ui_border: "#223357"
  status_bar_bg: "#0b1020"
  status_bar_text: "#8ea3c9"
  input_bg: "#101a33"
  input_text: "#e8f1ff"
  input_rule: "#8ec5ff"
  response_border: "#3a5a9e"
  ok: "#7fd8a4"
  warn: "#f0c674"
  error: "#ff8f9f"
  diff_add: "#7fd8a4"
  diff_del: "#ff8f9f"
  diff_line: "#8ec5ff"
  syntax_keyword: "#8ec5ff"
  syntax_string: "#7fd8a4"
  syntax_number: "#f0c674"
  syntax_comment: "#8ea3c9"
  syntax_function: "#c9a0ff"
  syntax_type: "#7fd8a4"
  syntax_variable: "#e8f1ff"
  completion_bg: "#101a33"
  completion_text: "#d5e4ff"
  completion_accent: "#8ec5ff"
  chat_bubble_user: "#1c2a4a"
  chat_bubble_assistant: "#101a33"
  hover: "#14224a"
  active: "#1c2a4a"
branding:
  agent_name: "Diamond Agent"
  welcome: "Shine on."
  response_label: " 💎 Diamond "
  prompt_symbol: "◆"
tool_prefix: "◆"
""",
    "reward-streak": """# Exclusive reward: hold a 30-day Hermes streak.
name: reward-streak
description: Streak achievement reward. Ember glow, unstoppable run.
colors:
  background: "#1c0f07"
  ui_accent: "#ffb454"
  banner_accent: "#ffb454"
  banner_title: "#ffedd5"
  banner_text: "#f5dfc4"
  ui_text: "#f5dfc4"
  banner_dim: "#c99b6e"
  banner_border: "#40240f"
  ui_border: "#4c2d14"
  status_bar_bg: "#1c0f07"
  status_bar_text: "#c99b6e"
  input_bg: "#2a1709"
  input_text: "#ffedd5"
  input_rule: "#ffb454"
  response_border: "#8a4d1d"
  ok: "#a3e07a"
  warn: "#ffd479"
  error: "#ff8a7a"
  diff_add: "#a3e07a"
  diff_del: "#ff8a7a"
  diff_line: "#ffb454"
  syntax_keyword: "#ffb454"
  syntax_string: "#a3e07a"
  syntax_number: "#ffd479"
  syntax_comment: "#c99b6e"
  syntax_function: "#e0a0ff"
  syntax_type: "#a3e07a"
  syntax_variable: "#ffedd5"
  completion_bg: "#2a1709"
  completion_text: "#f5dfc4"
  completion_accent: "#ffb454"
  chat_bubble_user: "#40240f"
  chat_bubble_assistant: "#2a1709"
  hover: "#3a1e0c"
  active: "#40240f"
branding:
  agent_name: "Streak Agent"
  welcome: "Keep the fire going."
  response_label: " 🔥 Streak "
  prompt_symbol: "▶"
tool_prefix: "▶"
""",
    "reward-olympian": """# Exclusive reward: reach Olympian tier on any achievement.
name: reward-olympian
description: Olympian achievement reward. Summit gold, mythic heights.
colors:
  background: "#141020"
  ui_accent: "#ffd700"
  banner_accent: "#ffd700"
  banner_title: "#fff8e0"
  banner_text: "#f2ead0"
  ui_text: "#f2ead0"
  banner_dim: "#b3a77e"
  banner_border: "#322a4a"
  ui_border: "#3a3055"
  status_bar_bg: "#141020"
  status_bar_text: "#b3a77e"
  input_bg: "#1e1833"
  input_text: "#fff8e0"
  input_rule: "#ffd700"
  response_border: "#7a6a2e"
  ok: "#a8e080"
  warn: "#ffe07a"
  error: "#ff9a8a"
  diff_add: "#a8e080"
  diff_del: "#ff9a8a"
  diff_line: "#ffd700"
  syntax_keyword: "#ffd700"
  syntax_string: "#a8e080"
  syntax_number: "#ffe07a"
  syntax_comment: "#b3a77e"
  syntax_function: "#e0b0ff"
  syntax_type: "#a8e080"
  syntax_variable: "#fff8e0"
  completion_bg: "#1e1833"
  completion_text: "#f2ead0"
  completion_accent: "#ffd700"
  chat_bubble_user: "#322a4a"
  chat_bubble_assistant: "#1e1833"
  hover: "#282040"
  active: "#322a4a"
branding:
  agent_name: "Olympian Agent"
  welcome: "You stand at the summit."
  response_label: " 🏆 Olympian "
  prompt_symbol: "★"
tool_prefix: "★"
""",
    "reward-completionist": """# Exclusive reward: complete every set collection.
name: reward-completionist
description: Completionist reward. Every set mastered, every badge earned.
colors:
  background: "#101418"
  ui_accent: "#9fe8b0"
  banner_accent: "#9fe8b0"
  banner_title: "#eafff0"
  banner_text: "#d4eadb"
  ui_text: "#d4eadb"
  banner_dim: "#8fa89a"
  banner_border: "#22302a"
  ui_border: "#2a3a32"
  status_bar_bg: "#101418"
  status_bar_text: "#8fa89a"
  input_bg: "#182228"
  input_text: "#eafff0"
  input_rule: "#9fe8b0"
  response_border: "#3a6a4e"
  ok: "#9fe8b0"
  warn: "#f0e080"
  error: "#ff9a8a"
  diff_add: "#9fe8b0"
  diff_del: "#ff9a8a"
  diff_line: "#9fe8b0"
  syntax_keyword: "#9fe8b0"
  syntax_string: "#c8f0d0"
  syntax_number: "#f0e080"
  syntax_comment: "#8fa89a"
  syntax_function: "#e0b0ff"
  syntax_type: "#c8f0d0"
  syntax_variable: "#eafff0"
  completion_bg: "#182228"
  completion_text: "#d4eadb"
  completion_accent: "#9fe8b0"
  chat_bubble_user: "#22302a"
  chat_bubble_assistant: "#182228"
  hover: "#1e2c26"
  active: "#22302a"
branding:
  agent_name: "Completionist Agent"
  welcome: "Nothing left undone."
  response_label: " 🏅 Completionist "
  prompt_symbol: "✓"
tool_prefix: "✓"
""",
}


# ── XP + level meta-layer ───────────────────────────────────────────────────
# Every tier unlock grants XP; total XP drives a level with a name. The level
# never resets, so there is always a progression number that grows even after
# every badge is unlocked.

TIER_XP = {"Copper": 10, "Silver": 25, "Gold": 50, "Diamond": 100, "Olympian": 250}
COLLECTION_XP = 200
SECRET_BONUS_XP = 25
_TIER_RANK = {t: i for i, t in enumerate(TIER_NAMES)}


def tier_rank_of(tier) -> int:
    """Rank a tier name (Olympian highest). Non-tier values rank below Copper."""
    if isinstance(tier, str):
        return _TIER_RANK.get(tier, -1)
    return -1

# Level names 1-50 (every 5 gets a distinct title; odd levels are gradations).
LEVEL_NAMES = {
    1: "Initiate",
    2: "Scout",
    3: "Helper",
    4: "Builder",
    5: "Operator",
    6: "Fixer",
    7: "Tinkerer",
    8: "Craftsman",
    9: "Artisan",
    10: "Journeyman",
    11: "Navigator",
    12: "Strategist",
    13: "Planner",
    14: "Architect",
    15: "Vanguard",
    16: "Optimizer",
    17: "Automator",
    18: "Integrator",
    19: "Explorer",
    20: "Veteran",
    21: "Specialist",
    22: "Mentor",
    23: "Brewer",
    24: "Sculptor",
    25: "Commander",
    26: "Synthesizer",
    27: "Visionary",
    28: "Catalyst",
    29: "Engineer",
    30: "Champion",
    31: "Virtuoso",
    32: "Orchestrator",
    33: "Showrunner",
    34: "Trailblazer",
    35: "Warlord",
    36: "Cartographer",
    37: "Maestro",
    38: "Alchemist",
    39: "Titan",
    40: "Conqueror",
    41: "Oracle",
    42: "Legend",
    43: "Myth",
    44: "Deity",
    45: "Ascendant",
    46: "Transcendent",
    47: "Immortal",
    48: "Cosmic",
    49: "Singularity",
    50: "Hermes",
}


def xp_for(achievement: Dict[str, Any]) -> int:
    """XP contribution for one evaluated achievement (highest tier reached)."""
    if achievement.get("kind") == "collection":
        return COLLECTION_XP if achievement.get("unlocked") else 0
    tier = achievement.get("tier")
    xp = TIER_XP.get(tier, 0) if isinstance(tier, str) else 0
    if xp and achievement.get("state") == "secret":
        xp += SECRET_BONUS_XP
    return xp


def level_for_xp(xp: int) -> Dict[str, Any]:
    """Map total XP to {level, name, xp_in_level, xp_for_next}."""
    level = 1
    xp_prev = 0
    xp_next = 100
    while xp >= xp_next and level < 50:
        level += 1
        xp_prev = xp_next
        xp_next += 75 + (level - 1) * 25
    return {
        "level": level,
        "name": LEVEL_NAMES.get(level, "Hermes"),
        "xp": xp,
        "xp_in_level": xp - xp_prev,
        "xp_for_next": xp_next - xp_prev,
        "next_name": LEVEL_NAMES.get(level + 1, "Hermes"),
    }


def compute_xp(achievements: List[Dict[str, Any]], aggregate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    total = sum(xp_for(a) for a in achievements)
    if aggregate is not None:
        total += quest_xp(evaluate_quests(achievements, aggregate))
    info = level_for_xp(total)
    info["total_xp"] = total
    return info


# ── Custom metric achievements ──────────────────────────────────────────────
# User-defined goal badges ("500 terminal calls this week") evaluated by the
# same engine, stored separately from the permanent catalog. Each goal names
# a metric and a target; progress is computed from the aggregate.

CUSTOM_METRIC_METRICS = {
    "session_count": "Hermes sessions",
    "total_tool_calls": "Total tool calls",
    "total_terminal_calls": "Terminal calls",
    "max_streak_days": "Longest streak (days)",
    "current_streak_days": "Current streak (days)",
    "distinct_tool_count": "Distinct tools used",
    "distinct_model_count": "Distinct models used",
    "distinct_provider_count": "Distinct providers used",
    "distinct_days_active": "Distinct days active",
    "total_messages": "Total messages",
    "total_tokens": "Total tokens",
}


def custom_goals_path() -> Path:
    return get_hermes_home() / "plugins" / "hermes-achievements" / "custom_goals.json"


def load_custom_goals() -> List[Dict[str, Any]]:
    try:
        if custom_goals_path().exists():
            return json.loads(custom_goals_path().read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def save_custom_goals(goals: List[Dict[str, Any]]) -> None:
    try:
        custom_goals_path().parent.mkdir(parents=True, exist_ok=True)
        custom_goals_path().write_text(json.dumps(goals, indent=2), encoding="utf-8")
    except Exception:
        pass


def evaluate_custom_goals(goals: List[Dict[str, Any]], aggregate: Dict[str, Any], sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for g in goals:
        metric = g.get("metric", "")
        target = int(g.get("target") or 0)
        value = 0
        if metric == "distinct_days_active":
            value = len({time.strftime("%Y-%m-%d", time.localtime(float(s.get("started_at") or 0))) for s in sessions if s.get("started_at")})
        else:
            value = int(aggregate.get(metric) or 0)
        pct = min(100, round((value / target) * 100)) if target else 0
        out.append({
            **g,
            "value": value,
            "target": target,
            "done": value >= target,
            "pct": pct,
            "metric_label": CUSTOM_METRIC_METRICS.get(metric, metric),
        })
    return out


def state_path() -> Path:
    return get_hermes_home() / "plugins" / "hermes-achievements" / "state.json"


def snapshot_path() -> Path:
    return get_hermes_home() / "plugins" / "hermes-achievements" / "scan_snapshot.json"


def checkpoint_path() -> Path:
    return get_hermes_home() / "plugins" / "hermes-achievements" / "scan_checkpoint.json"


def load_state() -> Dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {"unlocks": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"unlocks": {}}


def save_state(state: Dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, set):
        return sorted(_json_safe(v) for v in value)
    return value


def load_snapshot() -> Optional[Dict[str, Any]]:
    path = snapshot_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return None
    return None


def save_snapshot(data: Dict[str, Any]) -> None:
    path = snapshot_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(data), indent=2, sort_keys=True), encoding="utf-8")


def load_checkpoint() -> Dict[str, Any]:
    path = checkpoint_path()
    if not path.exists():
        return {"schema_version": 1, "generated_at": 0, "sessions": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("schema_version", 1)
            data.setdefault("generated_at", 0)
            data.setdefault("sessions", {})
            if isinstance(data.get("sessions"), dict):
                return data
    except Exception:
        pass
    return {"schema_version": 1, "generated_at": 0, "sessions": {}}


def save_checkpoint(data: Dict[str, Any]) -> None:
    path = checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_json_safe(data), indent=2, sort_keys=True), encoding="utf-8")


def session_fingerprint(meta: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "last_active": meta.get("last_active"),
        "started_at": meta.get("started_at"),
        "model": meta.get("model"),
        "title": meta.get("title") or meta.get("preview") or "Untitled",
    }


def _cache_is_fresh(now: int) -> bool:
    return _SNAPSHOT_CACHE is not None and (now - _SNAPSHOT_CACHE_AT) <= SNAPSHOT_TTL_SECONDS


def _is_snapshot_stale(snapshot: Optional[Dict[str, Any]], now: Optional[int] = None) -> bool:
    if not isinstance(snapshot, dict):
        return True
    ts = int(snapshot.get("generated_at") or 0)
    current = int(now or time.time())
    if ts <= 0:
        return True
    return (current - ts) > SNAPSHOT_TTL_SECONDS


def _scan_status_payload(now: Optional[int] = None) -> Dict[str, Any]:
    current = int(now or time.time())
    snap = _SNAPSHOT_CACHE if isinstance(_SNAPSHOT_CACHE, dict) else None
    generated_at = int((snap or {}).get("generated_at") or 0) if snap else 0
    return {
        "state": _SCAN_STATUS.get("state", "idle"),
        "started_at": _SCAN_STATUS.get("started_at"),
        "finished_at": _SCAN_STATUS.get("finished_at"),
        "last_error": _SCAN_STATUS.get("last_error"),
        "last_duration_ms": _SCAN_STATUS.get("last_duration_ms"),
        "run_count": _SCAN_STATUS.get("run_count", 0),
        "ttl_seconds": SNAPSHOT_TTL_SECONDS,
        "snapshot_generated_at": generated_at or None,
        "snapshot_age_seconds": (current - generated_at) if generated_at else None,
        "snapshot_stale": _is_snapshot_stale(snap, current),
    }


def _tool_name_from_call(call: Any) -> Optional[str]:
    if not isinstance(call, dict):
        return None
    fn = call.get("function") or {}
    return call.get("name") or fn.get("name")


def _content(msg: Dict[str, Any]) -> str:
    content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content)
    except Exception:
        return str(content)


def _count_tool(tool_names: List[str], *needles: str) -> int:
    lowered = [name.lower() for name in tool_names]
    return sum(1 for name in lowered if any(needle in name for needle in needles))


def model_provider(model_name: str) -> Optional[str]:
    name = (model_name or "").strip().lower()
    if not name or name == "none":
        return None
    if "/" in name:
        return name.split("/", 1)[0]
    for provider in ["openai", "anthropic", "google", "gemini", "mistral", "meta", "qwen", "deepseek", "xai", "nous", "ollama", "groq", "openrouter", "codex"]:
        if provider in name:
            return "google" if provider == "gemini" else provider
    return name.split(":", 1)[0].split("-", 1)[0]


def is_local_model_name(model_name: str) -> bool:
    name = (model_name or "").strip().lower()
    if not name or name == "none":
        return False
    local_markers = ["ollama", "llama.cpp", "localhost", "127.0.0.1", "local/", "local:", "gguf", "vllm-local"]
    return any(marker in name for marker in local_markers)


def analyze_messages(session_id: str, title: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    tool_names: Set[str] = set()
    tool_sequence: List[str] = []
    files_touched: Set[str] = set()
    full_text_parts: List[str] = []
    error_count = 0
    # Calendar days (year, yday) on which this session actually had messages.
    # The desktop keeps long-lived sessions open across many days; started_at
    # alone undercounts streaks, so capture the days with real activity here.
    active_days: Set[tuple] = set()

    for msg in messages:
        text = _content(msg)
        full_text_parts.append(text)
        ts = msg.get("timestamp")
        if ts:
            try:
                # Count a day as active only for REAL activity: active rows
                # plus compacted history (pre-compaction turns Hermes kept on
                # disk, discoverable in search). Exclude rewound/undo rows
                # (active=0 AND compacted=0) — those are content the user took
                # back, not activity that should feed a streak.
                if msg.get("active", 1) or msg.get("compacted"):
                    lt = time.localtime(float(ts))
                    active_days.add((lt.tm_year, lt.tm_yday))
            except Exception:
                pass
        if msg.get("tool_name"):
            name = str(msg["tool_name"])
            tool_names.add(name)
            # Tool result rows name the tool that already appeared in the assistant tool_calls.
            # Keep it for distinct-tool detection, but do not double-count it as a new call.
            if msg.get("role") != "tool":
                tool_sequence.append(name)
        for call in msg.get("tool_calls") or []:
            name = _tool_name_from_call(call)
            if name:
                tool_names.add(name)
                tool_sequence.append(name)
        if ERROR_RE.search(text):
            error_count += 1
        blob = text
        if msg.get("tool_calls"):
            blob += " " + json.dumps(msg.get("tool_calls"), default=str)
        files_touched.update(FILE_RE.findall(blob))

    full_text = "\n".join(full_text_parts)
    lower = full_text.lower()
    terminal_calls = _count_tool(tool_sequence, "terminal")
    web_calls = _count_tool(tool_sequence, "web_search", "web_extract")
    web_extract_calls = _count_tool(tool_sequence, "web_extract")
    browser_calls = _count_tool(tool_sequence, "browser")
    web_browser_calls = web_calls + browser_calls
    patch_calls = _count_tool(tool_sequence, "patch")
    file_reads_searches = _count_tool(tool_sequence, "read_file", "search_files")
    file_tool_calls = _count_tool(tool_sequence, "read_file", "write_file", "patch", "search_files")
    delegate_calls = _count_tool(tool_sequence, "delegate_task")
    process_calls = _count_tool(tool_sequence, "process") + len(re.findall(r"background\s*=\s*true", full_text, re.I))
    cron_calls = _count_tool(tool_sequence, "cronjob")
    image_vision_calls = _count_tool(tool_sequence, "image", "vision")
    tts_calls = _count_tool(tool_sequence, "tts", "text_to_speech")
    skill_events = _count_tool(tool_sequence, "skill") + len(re.findall(r"\bskill", lower))
    skill_manage_events = _count_tool(tool_sequence, "skill_manage")
    memory_events = _count_tool(tool_sequence, "memory", "mnemosyne")
    memory_write_events = _count_tool(tool_sequence, "mnemosyne_remember", "memory")

    return {
        "session_id": session_id,
        "title": title or "Untitled session",
        "message_count": len(messages),
        "tool_call_count": len(tool_sequence),
        "tool_names": tool_names,
        "active_days": sorted(active_days),
        "distinct_tool_count": len(tool_names),
        "error_count": error_count,
        "terminal_calls": terminal_calls,
        "web_calls": web_calls,
        "web_extract_calls": web_extract_calls,
        "browser_calls": browser_calls,
        "web_browser_calls": web_browser_calls,
        "patch_calls": patch_calls,
        "file_reads_searches": file_reads_searches,
        "file_tool_calls": file_tool_calls,
        "files_touched_count": len(files_touched),
        "delegate_calls": delegate_calls,
        "process_calls": process_calls,
        "cron_calls": cron_calls,
        "image_vision_calls": image_vision_calls,
        "tts_calls": tts_calls,
        "skill_events": skill_events,
        "skill_manage_events": skill_manage_events,
        "memory_events": memory_events,
        "memory_write_events": memory_write_events,
        "port_conflict": bool(PORT_RE.search(full_text)),
        "port_conflict_events": 1 if PORT_RE.search(full_text) else 0,
        "traceback_events": len(re.findall(r"traceback|exception", full_text, re.I)),
        "log_read_events": len(re.findall(r"gateway\.log|errors\.log|agent\.log|/api/logs|\blogs\b", full_text, re.I)),
        "permission_denied_events": len(re.findall(r"permission denied|eacces|operation not permitted", full_text, re.I)),
        "install_error_events": 1 if INSTALL_RE.search(full_text) and ERROR_RE.search(full_text) else 0,
        "install_success_events": 1 if INSTALL_RE.search(full_text) and SUCCESS_RE.search(full_text) else 0,
        "restart_after_error_events": 1 if error_count and re.search(r"\brestart|reload|kill|start\b", full_text, re.I) else 0,
        "env_var_error_events": len(re.findall(r"missing .*env|api key|environment variable|not configured|unauthorized|auth", full_text, re.I)),
        "yaml_error_events": len(re.findall(r"yaml|yml|colon|parse error", full_text, re.I)) if ERROR_RE.search(full_text) else 0,
        "docker_conflict_events": len(re.findall(r"docker.*(name|container).*already|container name conflict|Conflict\. The container", full_text, re.I)),
        "frontend_activity_events": len(re.findall(r"\.(css|svg|tsx|jsx)|frontend|tailwind|react", full_text, re.I)),
        "css_activity_events": len(re.findall(r"\.css|tailwind|style|className|visual", full_text, re.I)),
        "git_events": len(re.findall(r"\bgit\s+(commit|push|merge|rebase|status|diff)", full_text, re.I)),
        "tiny_patch_after_errors_events": 1 if error_count >= 5 and re.search(r"one character|single character|typo", full_text, re.I) else 0,
        "context_events": len(re.findall(r"compress|context window|token|cache", full_text, re.I)),
        "gateway_events": len(re.findall(r"gateway|discord|telegram|slack|api_server", full_text, re.I)),
        "plugin_events": len(re.findall(r"plugin|dashboard-plugins|__HERMES_PLUGIN|manifest\.json", full_text, re.I)),
        "rollback_events": len(re.findall(r"rollback|checkpoint", full_text, re.I)),
        "docs_activity_events": len(re.findall(r"docs|documentation|docusaurus|README", full_text, re.I)),
        "model_events": len(re.findall(r"model|provider|openrouter|codex|gemini|claude|anthropic|openai|mistral|qwen|deepseek|llama|ollama|vllm|gguf", full_text, re.I)),
        "openrouter_events": len(re.findall(r"openrouter", full_text, re.I)),
        "codex_events": len(re.findall(r"codex", full_text, re.I)),
        "claude_events": len(re.findall(r"claude|anthropic", full_text, re.I)),
        "gemini_events": len(re.findall(r"gemini|google ai|google model", full_text, re.I)),
        "local_model_events": len(re.findall(r"ollama|llama\.cpp|gguf|vllm|local model|open[- ]weight|open weights", full_text, re.I)),
        "toolset_events": len(re.findall(r"toolset|enabled_toolsets|browser tool|terminal tool|file tool|web tool", full_text, re.I)),
        "config_events": len(re.findall(r"config\.ya?ml|\b[a-z0-9_-]+config\.(?:js|ts|json|ya?ml)|\.env(?:\b|\.)|manifest\.json|settings\.json|pyproject\.toml|package\.json", full_text, re.I)),
        "git_history_events": len(re.findall(r"\bgit\s+(rebase|merge|fetch|pull|push|tag|checkout)|merge conflict|conflict\s*\(|rebase --continue", full_text, re.I)),
        "test_events": len(re.findall(r"pytest|unittest|vitest|playwright|npm test|pnpm test|node --check|py_compile|tests? passed|\bOK\b", full_text, re.I)),
        "screenshot_events": len(re.findall(r"screenshot|playwright|vision_analyze|browser_vision|\.png|image data", full_text, re.I)),
        "release_events": len(re.findall(r"\bgit\s+tag|release|version bump|changelog|publish|pushed? tag", full_text, re.I)),
        "cache_events": len(re.findall(r"cache hit|prompt caching|cache_read", full_text, re.I)),
        "model_names": set(),
    }


def evaluate_tiered(definition: Dict[str, Any], aggregate: Dict[str, Any]) -> Dict[str, Any]:
    metric = definition["threshold_metric"]
    progress = int(aggregate.get(metric, 0) or 0)
    tiers_list = sorted(definition.get("tiers", []), key=lambda t: t["threshold"])
    achieved = [t for t in tiers_list if progress >= t["threshold"]]
    next_tiers = [t for t in tiers_list if progress < t["threshold"]]
    tier = achieved[-1]["name"] if achieved else None
    next_tier = next_tiers[0]["name"] if next_tiers else None
    next_threshold = next_tiers[0]["threshold"] if next_tiers else (tiers_list[-1]["threshold"] if tiers_list else 1)
    current_threshold = achieved[-1]["threshold"] if achieved else 0
    denom = max(1, next_threshold - current_threshold)
    pct = 100 if not next_tiers and achieved else max(0, min(99, math.floor(((progress - current_threshold) / denom) * 100)))
    unlocked = bool(achieved)
    discovered = bool(progress > 0)
    state = "unlocked" if unlocked else ("secret" if definition.get("secret") and not discovered else "discovered")
    return {"unlocked": unlocked, "discovered": discovered or not definition.get("secret"), "state": state, "tier": tier, "progress": progress, "next_tier": next_tier, "next_threshold": next_threshold, "progress_pct": pct}


def evaluate_requirements(definition: Dict[str, Any], aggregate: Dict[str, Any]) -> Dict[str, Any]:
    requirements = definition.get("requirements", [])
    if not requirements:
        return {"unlocked": False, "discovered": not definition.get("secret"), "state": "secret" if definition.get("secret") else "discovered", "tier": None, "progress": 0, "next_tier": None, "next_threshold": 1, "progress_pct": 0}
    parts = []
    any_progress = False
    complete = True
    for requirement in requirements:
        value = int(aggregate.get(requirement["metric"], 0) or 0)
        threshold = int(requirement.get("gte", 1))
        any_progress = any_progress or value > 0
        complete = complete and value >= threshold
        parts.append(min(1.0, value / max(1, threshold)))
    pct = math.floor((sum(parts) / len(parts)) * 100)
    state = "unlocked" if complete else ("secret" if definition.get("secret") and not any_progress else "discovered")
    return {"unlocked": complete, "discovered": any_progress or not definition.get("secret"), "state": state, "tier": None, "progress": pct, "next_tier": None, "next_threshold": 100, "progress_pct": 100 if complete else min(99, pct)}


def evaluate_boolean(definition: Dict[str, Any], aggregate: Dict[str, Any]) -> Dict[str, Any]:
    # Backward-compatible helper for old tests/definitions. New catalog avoids simple booleans.
    unlocked = bool(aggregate.get(definition["metric"]))
    return {"unlocked": unlocked, "discovered": True, "state": "unlocked" if unlocked else "discovered", "tier": None, "progress": 1 if unlocked else 0, "next_tier": None, "next_threshold": 1, "progress_pct": 100 if unlocked else 0}


METRIC_LABELS = {
    "max_tool_calls_in_session": "tool calls in one session",
    "max_distinct_tools_in_session": "distinct Hermes tools used in one session",
    "max_terminal_calls_in_session": "terminal calls in one session",
    "max_file_tool_calls_in_session": "file/search/patch calls in one session",
    "max_web_browser_calls_in_session": "web search/extract or browser calls in one session",
    "max_messages_in_session": "messages in one session",
    "max_files_touched_in_session": "files touched in one session",
    "total_delegate_calls": "lifetime delegate_task calls",
    "total_process_calls": "lifetime background process operations",
    "total_cron_calls": "lifetime scheduled-job operations",
    "total_errors": "error/failed/traceback messages observed",
    "traceback_events": "traceback or exception mentions",
    "log_read_events": "log inspections",
    "port_conflict_events": "dev-server port conflict detections",
    "permission_denied_events": "permission-denied errors",
    "install_error_events": "package-install failures",
    "install_success_events": "successful package installs after package work",
    "restart_after_error_events": "restart/reload actions after error clusters",
    "env_var_error_events": "missing auth/config/environment-variable events",
    "yaml_error_events": "YAML/config parse incidents",
    "docker_conflict_events": "Docker/container-name conflicts",
    "frontend_activity_events": "frontend/CSS/SVG/React activity mentions",
    "css_activity_events": "CSS, styling, Tailwind, or className activity",
    "git_events": "git workflow commands",
    "tiny_patch_after_errors_events": "tiny typo-style fixes after error clusters",
    "skill_events": "Hermes skill mentions or tool use",
    "skill_manage_events": "skill_manage create/patch/delete operations",
    "memory_events": "memory or Mnemosyne tool events",
    "memory_write_events": "durable memory writes",
    "context_events": "context, compression, token, or cache-pressure mentions",
    "gateway_events": "gateway/API/chat-platform activity",
    "plugin_events": "dashboard plugin development or usage signals",
    "rollback_events": "rollback/checkpoint recovery mentions",
    "docs_activity_events": "documentation/README/docs activity",
    "model_events": "model/provider-related activity",
    "openrouter_events": "OpenRouter mentions",
    "codex_events": "Codex mentions",
    "cache_events": "prompt-cache/cache-hit mentions",
    "total_web_calls": "lifetime web_search/web_extract calls",
    "total_web_extract_calls": "lifetime web_extract calls",
    "browser_calls": "lifetime browser automation calls",
    "total_tool_calls": "lifetime Hermes tool calls",
    "total_terminal_calls": "lifetime terminal calls",
    "total_patch_calls": "lifetime targeted patch edits",
    "total_file_reads_searches": "lifetime read_file/search_files calls",
    "image_vision_calls": "image generation or vision tool calls",
    "tts_calls": "text-to-speech or voice tool calls",
    "distinct_model_count": "distinct model names seen in session metadata",
    "distinct_provider_count": "distinct model providers inferred from session metadata",
    "claude_events": "Claude/Anthropic model mentions",
    "gemini_events": "Gemini/Google model mentions",
    "local_model_events": "local/open-weight model mentions",
    "local_model_chat_sessions": "Hermes sessions whose model metadata is local/open-weight",
    "toolset_events": "toolset or tool-family mentions",
    "config_events": "configuration/environment/manifest activity",
    "git_history_events": "git history operations such as rebase, merge, fetch, push, or tag",
    "test_events": "test/check/verification command mentions",
    "screenshot_events": "screenshot, Playwright, PNG, or vision-inspection activity",
    "release_events": "release, version, publish, or git tag events",
    "session_count": "Hermes sessions",
    "weekend_sessions": "sessions started on weekends",
    "night_sessions": "sessions started late night or before dawn",
    "max_streak_days": "consecutive days with Hermes sessions",
    "current_streak_days": "current consecutive-day streak",
}


def metric_label(metric: str) -> str:
    return METRIC_LABELS.get(metric, metric.replace("_", " "))


def criteria_for(definition: Dict[str, Any]) -> str:
    if definition.get("secret") and definition.get("state") == "secret":
        return "Secret: exact requirement hidden until Hermes sees the first matching signal. Keep using Hermes across debugging, tools, memory, skills, plugins, and model workflows to reveal it."
    secret_prefix = ""
    if "threshold_metric" in definition:
        tiers_list = sorted(definition.get("tiers", []), key=lambda t: t["threshold"])
        if not tiers_list:
            return secret_prefix + "Requirement: use Hermes in the matching workflow."
        metric = metric_label(definition["threshold_metric"])
        ladder = ", ".join(f"{t['name']} {t['threshold']}" for t in tiers_list)
        return secret_prefix + f"Requirement: {metric}. Tier ladder: {ladder}."
    requirements = definition.get("requirements") or []
    if requirements:
        parts = [f"{metric_label(r['metric'])} ≥ {int(r.get('gte', 1))}" for r in requirements]
        return secret_prefix + "Requirement: " + "; ".join(parts) + "."
    return secret_prefix + "Requirement: complete the matching Hermes behavior."


def display_achievement(item: Dict[str, Any]) -> Dict[str, Any]:
    clean = dict(item)
    if clean.get("state") == "secret":
        return {**clean, "name": "???", "description": "Secret achievement: hidden until Hermes detects the first relevant behavior in your session history.", "criteria": criteria_for(clean), "icon": "secret"}
    clean["criteria"] = criteria_for(clean)
    return clean


def scan_sessions(
    limit: Optional[int] = None,
    progress_callback: Optional[Any] = None,
    progress_every: int = 250,
) -> Dict[str, Any]:
    """Scan Hermes sessions and build per-session achievement stats.

    ``limit=None`` (the default) scans the ENTIRE session history. Prior
    versions capped this at 200, which silently reduced achievement totals
    to ~2% of history on long-running installs and made lifetime badges
    unreachable. SQLite's ``LIMIT -1`` means "unlimited"; we map ``None``
    and non-positive values to ``-1`` so callers get the full catalog.

    Warm scans stay cheap: the checkpoint cache stores per-session stats
    keyed by ``(started_at, last_active)`` and only re-analyzes sessions
    whose fingerprint changed. Cold scans on large histories (thousands
    of sessions) take tens of seconds to several minutes; ``evaluate_all``
    runs them on a background thread so the dashboard UI never blocks on
    the first request.

    ``progress_callback(partial_sessions, scanned_so_far, total)`` — when
    provided, fires every ``progress_every`` sessions with the sessions
    analyzed so far and progress counters. Background scans use this to
    publish intermediate snapshots so a long cold scan surfaces badges
    incrementally on each dashboard refresh instead of going all-at-once
    at the end.
    """
    try:
        from hermes_state import SessionDB
    except Exception as exc:
        return {"sessions": [], "aggregate": {}, "error": f"Could not import SessionDB: {exc}", "scan_meta": {"mode": "failed", "sessions_total": 0, "sessions_rescanned": 0, "sessions_reused": 0}}

    checkpoint = load_checkpoint()
    previous_sessions = checkpoint.get("sessions") if isinstance(checkpoint.get("sessions"), dict) else {}
    reused = 0
    rescanned = 0

    # SQLite treats LIMIT -1 as "no limit". Map None / <=0 to -1 so the
    # full session history flows through unless the caller explicitly
    # requests a small sample (e.g. a smoke test).
    db_limit = -1 if (limit is None or limit <= 0) else int(limit)

    db = SessionDB()
    try:
        try:
            sessions_meta = db.list_sessions_rich(limit=db_limit, include_children=True, project_compression_tips=False, include_archived=True)
        except TypeError:
            # Older SessionDB (or test fakes) predates the include_archived
            # parameter: fall back to the archived-excluded query rather
            # than failing the whole scan. Newer backends keep counting
            # archived sessions; older ones degrade gracefully.
            sessions_meta = db.list_sessions_rich(limit=db_limit, include_children=True, project_compression_tips=False)
        total_sessions = len(sessions_meta)
        sessions: List[Dict[str, Any]] = []
        checkpoint_sessions: Dict[str, Any] = {}
        for idx, meta in enumerate(sessions_meta, start=1):
            sid = meta.get("id")
            if not sid:
                continue
            fp = session_fingerprint(meta)
            cached = previous_sessions.get(sid) if isinstance(previous_sessions, dict) else None
            cached_stats = cached.get("stats") if isinstance(cached, dict) else None
            cached_fp = cached.get("fingerprint") if isinstance(cached, dict) else None

            if isinstance(cached_stats, dict) and cached_fp == fp:
                stats = dict(cached_stats)
                reused += 1
            else:
                # Load BOTH active and soft-deleted messages. Hermes rewinds /
                # compresses long conversations by soft-deleting (active=0)
                # old messages while keeping the row; those messages are still
                # real activity that happened, and hiding them made the streak
                # collapse (a daily user showed a 2-day streak because every
                # day before the compression window was invisible).
                try:
                    messages = db.get_messages(sid, include_inactive=True)
                except TypeError:
                    # Older SessionDB (or test fakes) predates the
                    # include_inactive parameter: fall back to the default
                    # active-only query rather than failing the whole scan.
                    # Newer backends keep counting soft-deleted messages
                    # from compressed sessions; older ones degrade
                    # gracefully.
                    messages = db.get_messages(sid)
                stats = analyze_messages(sid, meta.get("title") or meta.get("preview") or "Untitled", messages)
                rescanned += 1

            stats["session_id"] = sid
            stats["title"] = meta.get("title") or meta.get("preview") or stats.get("title") or "Untitled"
            stats["started_at"] = meta.get("started_at")
            stats["last_active"] = meta.get("last_active")
            stats["source"] = meta.get("source")
            if meta.get("model"):
                stats.setdefault("model_names", set())
                if isinstance(stats["model_names"], set):
                    stats["model_names"].add(str(meta.get("model")))
                elif isinstance(stats["model_names"], list):
                    if str(meta.get("model")) not in stats["model_names"]:
                        stats["model_names"].append(str(meta.get("model")))
                else:
                    stats["model_names"] = {str(meta.get("model"))}

            sessions.append(stats)
            checkpoint_sessions[sid] = {"fingerprint": fp, "stats": _json_safe(stats)}

            if progress_callback is not None and progress_every > 0 and (idx % progress_every == 0) and idx < total_sessions:
                try:
                    progress_callback(list(sessions), idx, total_sessions)
                except Exception:
                    # Progress callbacks are advisory — a broken publisher
                    # must never abort the scan itself.
                    pass

        save_checkpoint({
            "schema_version": 1,
            "generated_at": int(time.time()),
            "sessions": checkpoint_sessions,
        })
    finally:
        close = getattr(db, "close", None)
        if close:
            close()
    return {
        "sessions": sessions,
        "aggregate": aggregate_stats(sessions),
        "scan_meta": {
            "mode": "incremental" if reused > 0 else "full",
            "sessions_total": len(sessions),
            "sessions_rescanned": rescanned,
            "sessions_reused": reused,
            "sessions_scanned_so_far": len(sessions),
            "sessions_expected_total": total_sessions,
        },
    }


def aggregate_stats(sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
    agg: Dict[str, Any] = {
        "session_count": len(sessions),
        "max_tool_calls_in_session": 0,
        "max_distinct_tools_in_session": 0,
        "max_messages_in_session": 0,
        "max_terminal_calls_in_session": 0,
        "max_file_tool_calls_in_session": 0,
        "max_web_calls_in_session": 0,
        "max_web_browser_calls_in_session": 0,
        "max_files_touched_in_session": 0,
        "total_errors": 0,
        "total_tool_calls": 0,
        "total_terminal_calls": 0,
        "total_web_calls": 0,
        "total_web_extract_calls": 0,
        "total_patch_calls": 0,
        "total_file_reads_searches": 0,
        "total_delegate_calls": 0,
        "total_process_calls": 0,
        "total_cron_calls": 0,
        "browser_calls": 0,
        "image_vision_calls": 0,
        "tts_calls": 0,
        "distinct_model_count": 0,
        "distinct_provider_count": 0,
        "local_model_chat_sessions": 0,
        "weekend_sessions": 0,
        "night_sessions": 0,
        "max_streak_days": 0,
        "current_streak_days": 0,
    }
    sum_keys = [
        "traceback_events", "log_read_events", "port_conflict_events", "permission_denied_events", "install_error_events", "install_success_events", "restart_after_error_events", "env_var_error_events", "yaml_error_events", "docker_conflict_events", "frontend_activity_events", "css_activity_events", "git_events", "tiny_patch_after_errors_events", "skill_events", "skill_manage_events", "memory_events", "memory_write_events", "context_events", "gateway_events", "plugin_events", "rollback_events", "docs_activity_events", "model_events", "openrouter_events", "codex_events", "claude_events", "gemini_events", "local_model_events", "toolset_events", "config_events", "git_history_events", "test_events", "screenshot_events", "release_events", "cache_events",
    ]
    for key in sum_keys:
        agg[key] = 0

    model_names: Set[str] = set()
    provider_names: Set[str] = set()
    for s in sessions:
        agg["max_tool_calls_in_session"] = max(agg["max_tool_calls_in_session"], s.get("tool_call_count", 0))
        agg["max_distinct_tools_in_session"] = max(agg["max_distinct_tools_in_session"], s.get("distinct_tool_count", 0))
        agg["max_messages_in_session"] = max(agg["max_messages_in_session"], s.get("message_count", 0))
        agg["max_terminal_calls_in_session"] = max(agg["max_terminal_calls_in_session"], s.get("terminal_calls", 0))
        agg["max_file_tool_calls_in_session"] = max(agg["max_file_tool_calls_in_session"], s.get("file_tool_calls", 0))
        agg["max_web_calls_in_session"] = max(agg["max_web_calls_in_session"], s.get("web_calls", 0))
        agg["max_web_browser_calls_in_session"] = max(agg["max_web_browser_calls_in_session"], s.get("web_browser_calls", 0))
        agg["max_files_touched_in_session"] = max(agg["max_files_touched_in_session"], s.get("files_touched_count", 0))
        agg["total_errors"] += s.get("error_count", 0)
        agg["total_tool_calls"] += s.get("tool_call_count", 0)
        agg["total_terminal_calls"] += s.get("terminal_calls", 0)
        agg["total_web_calls"] += s.get("web_calls", 0)
        agg["total_web_extract_calls"] += s.get("web_extract_calls", 0)
        agg["total_patch_calls"] += s.get("patch_calls", 0)
        agg["total_file_reads_searches"] += s.get("file_reads_searches", 0)
        agg["total_delegate_calls"] += s.get("delegate_calls", 0)
        agg["total_process_calls"] += s.get("process_calls", 0)
        agg["total_cron_calls"] += s.get("cron_calls", 0)
        agg["browser_calls"] += s.get("browser_calls", 0)
        agg["image_vision_calls"] += s.get("image_vision_calls", 0)
        agg["tts_calls"] += s.get("tts_calls", 0)
        for key in sum_keys:
            agg[key] += s.get(key, 0)
        model_names.update(s.get("model_names") or set())
        session_models = s.get("model_names") or set()
        for model_name in session_models:
            provider = model_provider(str(model_name))
            if provider:
                provider_names.add(provider)
        if any(is_local_model_name(str(model_name)) for model_name in session_models):
            agg["local_model_chat_sessions"] += 1
        if s.get("started_at"):
            try:
                lt = time.localtime(float(s.get("started_at")))
                if lt.tm_wday >= 5:
                    agg["weekend_sessions"] += 1
                if lt.tm_hour < 6 or lt.tm_hour >= 23:
                    agg["night_sessions"] += 1
            except Exception:
                pass
    agg["distinct_model_count"] = len({m for m in model_names if m and m != "None"})
    agg["distinct_provider_count"] = len(provider_names)
    agg["max_streak_days"], agg["current_streak_days"] = _streak_days(sessions)
    return agg


def _streak_days(sessions: List[Dict[str, Any]]) -> tuple:
    """Return (max_streak_days, current_streak_days) from session activity.

    A "streak day" is a calendar day (local time) that had at least one
    Hermes session. The max streak is the longest run of consecutive days
    with sessions; the current streak is the run ending at the most recent
    activity day (still alive if that day is today or yesterday — a missed
    day that's still today does not break the streak until the day passes).

    Activity comes from three signals, in order of accuracy:
    1. ``active_days`` — per-session list of calendar days that actually had
       messages (captured by ``analyze_messages``). This is the ground truth:
       a desktop session can stay open for a week and be used every day, and
       every one of those days lands in this list.
    2. ``last_active`` — the freshest heartbeat / latest message timestamp.
       Re-anchors long-lived sessions on the day they were last used, which
       matters when the checkpoint cache serves older analysis that predates
       the ``active_days`` field.
    3. ``started_at`` — the day the session began.
    """
    days: Set[tuple] = set()
    for s in sessions:
        per_session_days = s.get("active_days")
        if isinstance(per_session_days, (list, set, tuple)):
            for d in per_session_days:
                if isinstance(d, (list, tuple)) and len(d) == 2:
                    days.add((int(d[0]), int(d[1])))
            # active_days is authoritative; skip the anchor fallbacks.
            continue
        for anchor in ("started_at", "last_active"):
            ts = s.get(anchor)
            if not ts:
                continue
            try:
                lt = time.localtime(float(ts))
                days.add((lt.tm_year, lt.tm_yday))
            except Exception:
                continue
    if not days:
        return (0, 0)
    ordered = sorted(days)
    # Map consecutive day tuples to day numbers since epoch for arithmetic.
    day_nums = []
    for year, yday in ordered:
        base = int(time.mktime((year, 1, 1, 0, 0, 0, 0, 0, -1)))
        day_nums.append(base // 86400 + yday - 1)
    max_streak = 1
    cur_run = 1
    for i in range(1, len(day_nums)):
        if day_nums[i] == day_nums[i - 1] + 1:
            cur_run += 1
            max_streak = max(max_streak, cur_run)
        elif day_nums[i] != day_nums[i - 1]:
            cur_run = 1
    # Current streak: the run that ends at the latest active day. If that
    # day is not today and not yesterday, the streak has lapsed.
    last_day = day_nums[-1]
    now = time.localtime()
    today = int(time.mktime((now.tm_year, 1, 1, 0, 0, 0, 0, 0, -1))) // 86400 + now.tm_yday - 1
    if last_day < today - 1:
        current = 0
    else:
        current = cur_run
    return (max_streak, current)


def evaluate_definition(definition: Dict[str, Any], aggregate: Dict[str, Any]) -> Dict[str, Any]:
    if "threshold_metric" in definition:
        return evaluate_tiered(definition, aggregate)
    if "requirements" in definition:
        return evaluate_requirements(definition, aggregate)
    return evaluate_boolean(definition, aggregate)


def evaluate_collection(definition: Dict[str, Any], unlocked_ids: Set[str]) -> Dict[str, Any]:
    """A set-collection badge unlocks when every achievement in its category
    is in the unlock ledger. Progress is the fraction of category members
    unlocked, so the card shows a real completion bar.
    """
    collection = definition.get("collection", "")
    members = [a for a in ACHIEVEMENTS if a.get("category") == collection and a.get("kind") != "collection"]
    if not members:
        return {"unlocked": False, "discovered": True, "state": "discovered", "tier": None, "progress": 0, "next_tier": None, "next_threshold": 1, "progress_pct": 0}
    unlocked_members = sum(1 for m in members if m["id"] in unlocked_ids)
    complete = unlocked_members == len(members)
    pct = math.floor((unlocked_members / len(members)) * 100)
    return {
        "unlocked": complete,
        "discovered": unlocked_members > 0,
        "state": "unlocked" if complete else "discovered",
        "tier": "Olympian" if complete else None,
        "progress": unlocked_members,
        "next_tier": None,
        "next_threshold": len(members),
        "progress_pct": 100 if complete else min(99, pct),
    }


def evidence_for(definition: Dict[str, Any], sessions: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not sessions:
        return None
    metric = definition.get("threshold_metric")
    metric_to_session_key = {
        "max_tool_calls_in_session": "tool_call_count",
        "max_distinct_tools_in_session": "distinct_tool_count",
        "max_messages_in_session": "message_count",
        "max_terminal_calls_in_session": "terminal_calls",
        "max_file_tool_calls_in_session": "file_tool_calls",
        "max_web_calls_in_session": "web_calls",
        "max_web_browser_calls_in_session": "web_browser_calls",
        "max_files_touched_in_session": "files_touched_count",
    }
    if metric in metric_to_session_key:
        key = metric_to_session_key[metric]
        s = max(sessions, key=lambda x: x.get(key, 0))
        return {"session_id": s.get("session_id"), "title": s.get("title"), "value": s.get(key, 0)}
    return None


def _eta_days_for(item: Dict[str, Any], rates: Dict[str, float]) -> Optional[int]:
    """Days until the next tier at the recent daily rate.

    Only for lifetime accumulation metrics (a best-session feat can jump
    in one run, so a rate-based estimate would mislead). Returns None when
    the metric has no observed recent rate or nothing remains to accumulate.
    """
    if item.get("unlocked") or item.get("kind") != "lifetime" or not item.get("threshold_metric"):
        return None
    metric = item["threshold_metric"]
    rate = rates.get(metric, 0.0)
    remaining = max(0, int(item.get("next_threshold") or 0) - int(item.get("progress") or 0))
    if rate <= 0 or remaining <= 0:
        return None
    return max(1, math.ceil(remaining / rate))


def _activity_calendar(sessions: List[Dict[str, Any]], days: int = 365) -> List[Dict[str, Any]]:
    """Daily session + tool-call counts for the last ``days`` calendar days.

    Returns a list aligned to the last ``days`` days (oldest first), each
    entry: {"date": "YYYY-MM-DD", "sessions": n, "tools": n}. Days with no
    activity are present with zeros so the desktop can render a contiguous
    GitHub-style heatmap without filling gaps itself.
    """
    cutoff = time.time() - days * 86400
    daily: Dict[str, List[int]] = {}
    for s in sessions:
        started = s.get("started_at")
        if not started:
            continue
        try:
            ts = float(started)
        except Exception:
            continue
        if ts < cutoff:
            continue
        lt = time.localtime(ts)
        key = f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
        entry = daily.setdefault(key, [0, 0])
        entry[0] += 1
        entry[1] += int(s.get("tool_call_count") or 0)

    out: List[Dict[str, Any]] = []
    now_lt = time.localtime()
    today = f"{now_lt.tm_year:04d}-{now_lt.tm_mon:02d}-{now_lt.tm_mday:02d}"
    # Walk back from today, at most `days` entries.
    cursor = time.mktime((now_lt.tm_year, now_lt.tm_mon, now_lt.tm_mday, 0, 0, 0, 0, 0, -1))
    for _ in range(days):
        lt = time.localtime(cursor)
        key = f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
        if key > today:
            cursor -= 86400
            continue
        counts = daily.get(key, [0, 0])
        out.append({"date": key, "sessions": counts[0], "tools": counts[1]})
        cursor -= 86400
        if key == "1970-01-01":
            break
    out.reverse()
    return out


def _category_summary(achievements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-category completion counts for the header chips.

    Each entry: {category, total, unlocked, pct}. Sorted by total desc so the
    header row reads in a stable order.
    """
    by_cat: Dict[str, Dict[str, Any]] = {}
    for a in achievements:
        cat = a.get("category") or "Other"
        if a.get("kind") == "collection":
            continue
        entry = by_cat.setdefault(cat, {"category": cat, "total": 0, "unlocked": 0})
        entry["total"] += 1
        if a.get("unlocked"):
            entry["unlocked"] += 1
    out = []
    for entry in by_cat.values():
        entry["pct"] = round((entry["unlocked"] / entry["total"]) * 100) if entry["total"] else 0
        out.append(entry)
    out.sort(key=lambda e: (-e["total"], e["category"]))
    return out


def _monthly_challenges(sessions: List[Dict[str, Any]], achievements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Evaluate the rotating monthly challenge set against current-month data.

    Challenges are time-boxed goals computed from the same scan data, so they
    stay fresh without adding permanent achievements:
      - sessions: Hermes sessions started this month
      - tool_calls: tool calls made this month
      - active_days: distinct days with sessions this month
      - unlocks: badges unlocked this month (from unlocked_at)
      - tier_ups: tier upgrades earned this month (from unlocked_at)
      - streak: current streak in days
    """
    now = time.time()
    lt = time.localtime(now)
    month_start = time.mktime((lt.tm_year, lt.tm_mon, 1, 0, 0, 0, 0, 0, -1))
    month_sessions = [s for s in sessions if float(s.get("started_at") or 0) >= month_start]
    active_days = {time.strftime("%Y-%m-%d", time.localtime(float(s["started_at"]))) for s in month_sessions if s.get("started_at")}
    month_unlocks = [a for a in achievements if a.get("unlocked") and a.get("unlocked_at") and float(a["unlocked_at"]) >= month_start]
    tier_ups = [a for a in month_unlocks if a.get("tier") and a.get("kind") != "collection"]

    def base(id_, name, desc, value, target):
        return {
            "id": id_,
            "name": name,
            "description": desc,
            "value": value,
            "target": target,
            "done": value >= target,
            "pct": min(100, round((value / target) * 100)) if target else 0,
        }

    month_name = time.strftime("%B", lt)
    return [
        base("m_sessions", f"{month_name} Sessions", "Sessions started this month", len(month_sessions), 10),
        base("m_tool_calls", f"{month_name} Tool Calls", "Tool calls made this month", sum(int(s.get("tool_call_count") or 0) for s in month_sessions), 500),
        base("m_active_days", f"{month_name} Active Days", "Distinct days with activity this month", len(active_days), 12),
        base("m_unlocks", f"{month_name} Unlocks", "Badges unlocked this month", len(month_unlocks), 3),
        base("m_tier_ups", f"{month_name} Tier Ups", "Tier upgrades this month", len(tier_ups), 2),
        base("m_streak", "Streak This Month", "Current consecutive-day streak", int(_streak_days(sessions)[1] or 0), 14),
    ]


def _records(sessions: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Personal records — bests the user can beat (replay value).

    Computed from session data: best day by tool calls, longest session by
    message count (the closest honest proxy — session ``last_active`` is the
    last-activity date, not a duration, so wall-clock math is misleading),
    most sessions in one day, biggest session by tool calls.
    """
    best_day: Dict[str, Dict[str, Any]] = {}
    sessions_per_day: Dict[str, int] = {}
    best_session: Optional[Dict[str, Any]] = None
    longest_session: Optional[Dict[str, Any]] = None
    for s in sessions:
        started = s.get("started_at")
        if not started:
            continue
        try:
            ts = float(started)
        except Exception:
            continue
        lt = time.localtime(ts)
        day = f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"
        tools = int(s.get("tool_call_count") or 0)
        msgs = int(s.get("message_count") or 0)
        sessions_per_day[day] = sessions_per_day.get(day, 0) + 1
        cur = best_day.setdefault(day, {"tools": 0})
        cur["tools"] += tools
        if best_session is None or tools > int(best_session.get("tool_call_count") or 0):
            best_session = s
        if longest_session is None or msgs > int(longest_session.get("message_count") or 0):
            longest_session = s
    top_day = max(best_day.items(), key=lambda kv: kv[1]["tools"]) if best_day else None
    top_sessions_day = max(sessions_per_day.items(), key=lambda kv: kv[1]) if sessions_per_day else None
    return {
        "best_day": {"date": top_day[0], "tool_calls": top_day[1]["tools"]} if top_day else None,
        "busiest_day": {"date": top_sessions_day[0], "sessions": top_sessions_day[1]} if top_sessions_day else None,
        "biggest_session": {
            "title": best_session.get("title") or "Untitled session",
            "tool_calls": int(best_session.get("tool_call_count") or 0),
            "date": time.strftime("%Y-%m-%d", time.localtime(float(best_session["started_at"]))) if best_session and best_session.get("started_at") else None,
        } if best_session else None,
        "longest_session": {
            "title": longest_session.get("title") or "Untitled session",
            "messages": int(longest_session.get("message_count") or 0),
            "date": time.strftime("%Y-%m-%d", time.localtime(float(longest_session["started_at"]))) if longest_session and longest_session.get("started_at") else None,
        } if longest_session else None,
    }


def _weekly_challenges(sessions: List[Dict[str, Any]], achievements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Weekly time-boxed goals (Monday-start week) for a faster win cycle."""
    now = time.time()
    lt = time.localtime(now)
    # Monday-start: tm_wday Mon=0.
    week_start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) - lt.tm_wday * 86400
    week_sessions = [s for s in sessions if float(s.get("started_at") or 0) >= week_start]
    active_days = {time.strftime("%Y-%m-%d", time.localtime(float(s["started_at"]))) for s in week_sessions if s.get("started_at")}
    week_unlocks = [a for a in achievements if a.get("unlocked") and a.get("unlocked_at") and float(a["unlocked_at"]) >= week_start]

    def base(id_, name, value, target):
        return {
            "id": id_,
            "name": name,
            "value": value,
            "target": target,
            "done": value >= target,
            "pct": min(100, round((value / target) * 100)) if target else 0,
        }

    return [
        base("w_sessions", "Sessions This Week", len(week_sessions), 7),
        base("w_active_days", "Active Days This Week", len(active_days), 4),
        base("w_unlocks", "Unlocks This Week", len(week_unlocks), 1),
        base("w_tool_calls", "Tool Calls This Week", sum(int(s.get("tool_call_count") or 0) for s in week_sessions), 250),
    ]


# ── Quests — combo requirements with bonus XP ────────────────────────────────

QUESTS: List[Dict[str, Any]] = [
    {
        "id": "q_power_user",
        "name": "Power User",
        "description": "Unlock 3 Tool Mastery badges and complete the Tools set.",
        "xp": 100,
        "requirements": {"category_counts": {"Tool Mastery": 3}, "sets": ["Tool Mastery"]},
    },
    {
        "id": "q_full_stack",
        "name": "Full Stack",
        "description": "Unlock 2 Vibe Coding and 2 Agent Autonomy badges.",
        "xp": 100,
        "requirements": {"category_counts": {"Vibe Coding": 2, "Agent Autonomy": 2}},
    },
    {
        "id": "q_debug_king",
        "name": "Debug King",
        "description": "Unlock 4 Debugging Chaos badges and hit a 7-day streak.",
        "xp": 150,
        "requirements": {"category_counts": {"Debugging Chaos": 4}, "streak_days": 7},
    },
    {
        "id": "q_model_gourmet",
        "name": "Model Gourmet",
        "description": "Unlock 3 Model Lore badges and use 3 distinct models.",
        "xp": 100,
        "requirements": {"category_counts": {"Model Lore": 3}, "distinct_models": 3},
    },
    {
        "id": "q_hermes_phd",
        "name": "Hermes PhD",
        "description": "Unlock 4 Hermes Native badges and 4 Model Lore badges.",
        "xp": 150,
        "requirements": {"category_counts": {"Hermes Native": 4, "Model Lore": 4}},
    },
]


def evaluate_quests(achievements: List[Dict[str, Any]], aggregate: Dict[str, Any], completions: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    unlocked_ids = {a["id"] for a in achievements if a.get("unlocked")}
    cat_counts: Dict[str, int] = {}
    for a in achievements:
        if a.get("unlocked") and a.get("kind") != "collection":
            cat = a.get("category") or ""
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
    sets_done = {a.get("collection") for a in achievements if a.get("kind") == "collection" and a.get("unlocked")}
    streak = int(aggregate.get("max_streak_days") or 0)
    models = int(aggregate.get("distinct_model_count") or 0)
    completions = completions or {}

    out = []
    for q in QUESTS:
        req = q.get("requirements", {})
        ok = True
        for cat, need in req.get("category_counts", {}).items():
            if cat_counts.get(cat, 0) < need:
                ok = False
        for set_name in req.get("sets", []):
            if set_name not in sets_done:
                ok = False
        if req.get("streak_days") and streak < req["streak_days"]:
            ok = False
        if req.get("distinct_models") and models < req["distinct_models"]:
            ok = False
        entry = {**q, "done": ok, "unlocked_ids": len(unlocked_ids)}
        # Monotonic completion: once a quest is recorded in the ledger it
        # stays done, even if a later scan no longer meets the requirements
        # (same contract as the unlock ledger). completed_at comes from the
        # ledger when present.
        if q["id"] in completions:
            entry["done"] = True
            entry["completed_at"] = completions[q["id"]].get("completed_at")
        out.append(entry)
    return out


def load_quest_completions() -> Dict[str, Any]:
    """Quest completion ledger: quest id → {completed_at}. Monotonic — a
    quest is completed once; later scans never un-complete it."""
    state = load_state()
    completions = state.get("quest_completions")
    if isinstance(completions, dict):
        return completions
    return {}


def record_quest_completions(quests: List[Dict[str, Any]]) -> None:
    """Persist newly-done quests to the ledger. Only called on finished
    scans (never partial snapshots) so a half-scanned session can't stamp a
    completion time that a later session would shift."""
    state = load_state()
    completions = state.setdefault("quest_completions", {})
    now = int(time.time())
    changed = False
    for q in quests:
        if q.get("done") and q.get("id") not in completions:
            completions[q["id"]] = {"completed_at": now}
            changed = True
    if changed:
        save_state(state)


def quest_xp(quests: List[Dict[str, Any]]) -> int:
    return sum(int(q.get("xp") or 0) for q in quests if q.get("done"))


def _recent_daily_rates(sessions: List[Dict[str, Any]], window_days: int = 14) -> Dict[str, float]:
    """Per-metric daily accumulation rate over the last ``window_days`` days.

    Aggregates only sessions started within the window, then divides each
    cumulative metric by the window length. Used for "days until next tier
    at your current pace" estimates on lifetime achievements.
    """
    if not sessions:
        return {}
    cutoff = time.time() - window_days * 86400
    recent = []
    for s in sessions:
        started = s.get("started_at")
        if not started:
            continue
        try:
            if float(started) >= cutoff:
                recent.append(s)
        except Exception:
            continue
    if not recent:
        return {}
    recent_agg = aggregate_stats(recent)
    rates: Dict[str, float] = {}
    for key, value in recent_agg.items():
        if isinstance(value, (int, float)) and value > 0:
            rates[key] = float(value) / window_days
    return rates


def _compute_from_scan(scan: Dict[str, Any], *, is_partial: bool = False) -> Dict[str, Any]:
    """Evaluate every achievement definition against a scan result.

    Used by ``compute_all`` for finished scans AND by the background
    progress callback for partial, in-flight snapshots. ``is_partial=True``
    skips persisting ``state.json`` unlocks — we don't want to record an
    "unlock time" based on half a scan that a later session might shift.
    """
    aggregate = scan.get("aggregate", {})
    state = load_state()
    if is_partial:
        # Partial scans MUST still see the ledger for monotonic forcing.
        # A background scan streams sessions in gradually; if a partial
        # snapshot were computed with an empty ledger, already-earned
        # unlocks would drop out of the served numbers mid-scan (e.g.
        # 18/69 → 15, Lv6 → Lv5) until the final scan landed. Read the
        # ledger for forcing, but never write new unlocks from a partial.
        state = {**state, "unlocks": dict(state.get("unlocks", {}))}
    unlocks = state.setdefault("unlocks", {})
    now = int(time.time())
    rates = _recent_daily_rates(scan.get("sessions", []))
    evaluated = []
    # First pass: regular achievements (populates the ledger for collections).
    for definition in ACHIEVEMENTS:
        if definition.get("kind") == "collection":
            continue
        result = evaluate_definition(definition, aggregate)
        unlock_id = definition["id"]
        if not is_partial and result["unlocked"] and unlock_id not in unlocks:
            unlocks[unlock_id] = {"unlocked_at": now, "first_tier": result.get("tier"), "highest_tier": result.get("tier"), "evidence": evidence_for(definition, scan.get("sessions", []))}
        item = {**definition, **result}
        # Monotonicity: an unlock recorded in the ledger is permanent. A scan
        # that temporarily misses the session (e.g. partial window, archive
        # flag flip, DB hiccup) must never re-lock a badge the user already
        # earned. The ledger is the source of truth for "has been earned".
        # The TIER is monotonic too: XP comes from the tier, so a transient
        # scan downgrade (Gold → Silver) would shrink XP and drop the level.
        # Track the highest tier ever reached and clamp display to it.
        if unlock_id in unlocks:
            item["unlocked"] = True
            item["state"] = "unlocked"
            rec = unlocks[unlock_id]
            cur_rank = tier_rank_of(item.get("tier"))
            rec_rank = tier_rank_of(rec.get("highest_tier"))
            if cur_rank > rec_rank:
                rec["highest_tier"] = item.get("tier")
            item["tier"] = rec.get("highest_tier") or rec.get("first_tier") or item.get("tier")
        if result["unlocked"]:
            item["unlocked_at"] = unlocks.get(unlock_id, {}).get("unlocked_at")
            item["evidence"] = unlocks.get(unlock_id, {}).get("evidence") or evidence_for(definition, scan.get("sessions", []))
        # ETA: days until the next tier at the recent daily rate, for
        # lifetime accumulation metrics only (a best-session feat can jump
        # in one run, so a rate-based estimate would mislead).
        eta = _eta_days_for(item, rates)
        if eta is not None:
            item["eta_days"] = eta
        evaluated.append(display_achievement(item))
    # Second pass: set collections, evaluated against the now-current ledger.
    for definition in ACHIEVEMENTS:
        if definition.get("kind") != "collection":
            continue
        result = evaluate_collection(definition, set(unlocks.keys()))
        unlock_id = definition["id"]
        if not is_partial and result["unlocked"] and unlock_id not in unlocks:
            unlocks[unlock_id] = {"unlocked_at": now, "first_tier": result.get("tier"), "evidence": None}
        item = {**definition, **result}
        if unlock_id in unlocks:
            item["unlocked"] = True
            item["state"] = "unlocked"
            item["tier"] = "Olympian"
        if result["unlocked"]:
            item["unlocked_at"] = unlocks.get(unlock_id, {}).get("unlocked_at")
        evaluated.append(display_achievement(item))
    if not is_partial:
        save_state(state)
    unlocked = [a for a in evaluated if a["unlocked"]]
    discovered = [a for a in evaluated if a.get("state") == "discovered"]
    secret = [a for a in evaluated if a.get("state") == "secret"]
    return {
        "achievements": evaluated,
        "sessions": scan.get("sessions", []),
        "aggregate": aggregate,
        "scan_meta": scan.get("scan_meta", {}),
        "error": scan.get("error"),
        "unlocked_count": len(unlocked),
        "discovered_count": len(discovered),
        "secret_count": len(secret),
        "total_count": len(evaluated),
        "generated_at": now,
    }


def compute_all(progress_callback: Optional[Any] = None, progress_every: int = 250) -> Dict[str, Any]:
    scan = scan_sessions(progress_callback=progress_callback, progress_every=progress_every)
    return _compute_from_scan(scan, is_partial=False)


_BACKGROUND_SCAN_THREAD: Optional[threading.Thread] = None
_BACKGROUND_SCAN_LOCK = threading.Lock()


def _build_pending_snapshot(now: int) -> Dict[str, Any]:
    """Placeholder payload used while the first-ever scan is still running.

    Returns a structurally-complete response so the dashboard UI can render
    an empty achievement list + spinner without special-casing "no data yet".
    """
    evaluated = [display_achievement({**d, **{"unlocked": False, "discovered": False, "state": "secret" if d.get("secret") else "discovered", "progress": 0, "progress_pct": 0, "next_tier": (d.get("tiers") or [{}])[0].get("name"), "next_threshold": (d.get("tiers") or [{}])[0].get("threshold", 1), "tier": None}}) for d in ACHIEVEMENTS]
    return {
        "achievements": evaluated,
        "sessions": [],
        "aggregate": {},
        "scan_meta": {"mode": "pending", "sessions_total": 0, "sessions_rescanned": 0, "sessions_reused": 0},
        "error": None,
        "unlocked_count": 0,
        "discovered_count": sum(1 for a in evaluated if a.get("state") == "discovered"),
        "secret_count": sum(1 for a in evaluated if a.get("state") == "secret"),
        "total_count": len(evaluated),
        "generated_at": now,
    }


def _run_scan_and_update_cache(publish_partial_snapshots: bool = True) -> None:
    """Execute a scan + snapshot update. Called synchronously or from a thread.

    When ``publish_partial_snapshots=True`` (the default for background
    scans), the scanner periodically publishes an in-progress snapshot to
    ``_SNAPSHOT_CACHE`` so each dashboard refresh during a long cold scan
    shows more progress — badges unlock incrementally as sessions stream
    in, instead of staying at zero for minutes and then jumping to the
    final state. Synchronous /rescan callers pass ``False`` because they
    block on the full result anyway.
    """
    global _SNAPSHOT_CACHE, _SNAPSHOT_CACHE_AT
    with _SCAN_LOCK:
        started = int(time.time())
        _SCAN_STATUS["state"] = "running"
        _SCAN_STATUS["started_at"] = started
        _SCAN_STATUS["last_error"] = None

        def _publish_partial(partial_sessions, scanned_so_far, total):
            global _SNAPSHOT_CACHE, _SNAPSHOT_CACHE_AT
            try:
                partial_scan = {
                    "sessions": partial_sessions,
                    "aggregate": aggregate_stats(partial_sessions),
                    "scan_meta": {
                        "mode": "in_progress",
                        "sessions_total": scanned_so_far,
                        "sessions_rescanned": 0,
                        "sessions_reused": 0,
                        "sessions_scanned_so_far": scanned_so_far,
                        "sessions_expected_total": total,
                    },
                }
                partial = _compute_from_scan(partial_scan, is_partial=True)
                # Keep the cache in the 'stale' TTL regime by NOT bumping
                # _SNAPSHOT_CACHE_AT to "now". The UI treats partial
                # results as stale so it keeps polling /scan-status and
                # sees the final snapshot when the scan finishes. In-flight
                # partials are visible but are never mistaken for finished.
                _SNAPSHOT_CACHE = _json_safe(partial)
                _SNAPSHOT_CACHE_AT = 0
            except Exception:
                # Intermediate publication is best-effort; don't kill the scan.
                pass

        callback = _publish_partial if publish_partial_snapshots else None
        try:
            computed = compute_all(progress_callback=callback)
            _SNAPSHOT_CACHE = _json_safe(computed)
            _SNAPSHOT_CACHE_AT = int(_SNAPSHOT_CACHE.get("generated_at") or int(time.time()))
            save_snapshot(_SNAPSHOT_CACHE)
            _SCAN_STATUS["state"] = "idle"
        except Exception as exc:
            _SCAN_STATUS["state"] = "failed"
            _SCAN_STATUS["last_error"] = str(exc)
        finally:
            _SCAN_STATUS["finished_at"] = int(time.time())
            _SCAN_STATUS["last_duration_ms"] = int((_SCAN_STATUS["finished_at"] - started) * 1000)
            _SCAN_STATUS["run_count"] = int(_SCAN_STATUS.get("run_count", 0)) + 1


def _start_background_scan() -> None:
    """Kick off a scan in a daemon thread if one isn't already running.

    Idempotent: concurrent callers see the in-flight thread and return
    immediately. The thread updates ``_SNAPSHOT_CACHE`` on completion so
    subsequent ``/achievements`` requests see fresh data. While running,
    it also publishes partial snapshots every ~250 sessions so the UI
    reflects incremental progress on long cold scans.
    """
    global _BACKGROUND_SCAN_THREAD
    with _BACKGROUND_SCAN_LOCK:
        existing = _BACKGROUND_SCAN_THREAD
        if existing is not None and existing.is_alive():
            return
        thread = threading.Thread(
            target=_run_scan_and_update_cache,
            kwargs={"publish_partial_snapshots": True},
            name="hermes-achievements-scan",
            daemon=True,
        )
        _BACKGROUND_SCAN_THREAD = thread
        thread.start()


def evaluate_all(force: bool = False) -> Dict[str, Any]:
    """Return the current achievements payload.

    Behavior matrix:

    * Fresh in-memory cache → return it instantly.
    * Stale on-disk snapshot → load it, kick a background rescan, return
      the stale data (UI decorates it with ``is_stale=True``).
    * No snapshot yet (first-ever run) → kick a background scan, return
      an empty-but-valid "pending" payload so the UI can render a spinner
      without blocking.
    * ``force=True`` (manual /rescan) → run synchronously, block the
      caller, replace the cache.

    Warm scans stay cheap (the checkpoint cache reuses per-session stats).
    Cold scans on 8000+ session databases take minutes; the background
    thread prevents that from ever blocking the dashboard request path.
    """
    global _SNAPSHOT_CACHE, _SNAPSHOT_CACHE_AT
    now = int(time.time())

    if not force and _cache_is_fresh(now):
        return _SNAPSHOT_CACHE or {}

    # Lazy-load persisted snapshot from disk so fresh process starts
    # don't have to wait for a scan to serve cached data.
    if _SNAPSHOT_CACHE is None:
        persisted = load_snapshot()
        if isinstance(persisted, dict):
            generated_at = int(persisted.get("generated_at") or 0)
            _SNAPSHOT_CACHE = persisted
            _SNAPSHOT_CACHE_AT = generated_at or now

    if force:
        # Manual /rescan — block the caller, synchronous scan path.
        # No partial publishing: the caller is waiting for the final result.
        _run_scan_and_update_cache(publish_partial_snapshots=False)
        if _SNAPSHOT_CACHE is not None:
            return _SNAPSHOT_CACHE
        # Scan failed with no prior cache — surface empty payload.
        return _build_pending_snapshot(now)

    # Non-force path: serve whatever we have and refresh in background.
    if _SNAPSHOT_CACHE is not None:
        if not _cache_is_fresh(now):
            _start_background_scan()
        return _SNAPSHOT_CACHE

    # First-ever run on this machine — no snapshot yet. Kick off a scan
    # and return a pending placeholder. The UI polls /scan-status and
    # re-fetches /achievements when the scan completes.
    _start_background_scan()
    return _build_pending_snapshot(now)


@router.get("/achievements")
async def achievements():
    data = evaluate_all()
    payload = {k: data[k] for k in ["achievements", "unlocked_count", "discovered_count", "secret_count", "total_count", "error", "generated_at"] if k in data}
    payload["is_stale"] = _is_snapshot_stale(data)
    payload["scan_meta"] = {
        **(data.get("scan_meta") or {}),
        "status": _scan_status_payload(),
    }
    aggregate = data.get("aggregate") or {}
    payload["streak"] = {
        "max_streak_days": int(aggregate.get("max_streak_days") or 0),
        "current_streak_days": int(aggregate.get("current_streak_days") or 0),
    }
    payload["activity"] = _activity_calendar(data.get("sessions", []))
    unlocked_ids = {a["id"] for a in data.get("achievements", []) if a.get("unlocked")}
    payload["rewards"] = evaluate_rewards(data.get("achievements", []), aggregate, unlocked_ids)
    payload["level"] = compute_xp(data.get("achievements", []), aggregate)
    payload["categories"] = _category_summary(data.get("achievements", []))
    payload["challenges"] = _monthly_challenges(data.get("sessions", []), data.get("achievements", []))
    payload["weekly"] = _weekly_challenges(data.get("sessions", []), data.get("achievements", []))
    payload["records"] = _records(data.get("sessions", []))
    payload["quests"] = evaluate_quests(data.get("achievements", []), aggregate, load_quest_completions())
    # Persist newly-completed quests (finished scans only — /achievements
    # serves the final snapshot cache, never a partial).
    record_quest_completions(payload["quests"])
    completed = [
        {**q, "completed_at": q.get("completed_at")}
        for q in payload["quests"]
        if q.get("done") and q.get("completed_at")
    ]
    completed.sort(key=lambda q: q.get("completed_at") or 0, reverse=True)
    payload["recently_completed_quests"] = completed[:5]
    payload["custom_goals"] = evaluate_custom_goals(load_custom_goals(), aggregate, data.get("sessions", []))
    payload["custom_metric_options"] = CUSTOM_METRIC_METRICS
    return payload


@router.get("/rewards")
async def rewards():
    data = evaluate_all()
    aggregate = data.get("aggregate") or {}
    unlocked_ids = {a["id"] for a in data.get("achievements", []) if a.get("unlocked")}
    return {"ok": True, "rewards": evaluate_rewards(data.get("achievements", []), aggregate, unlocked_ids)}


@router.post("/rewards/{reward_id}/install")
async def install_reward(reward_id: str):
    """Install an unlocked reward theme into the skins directory.

    The reward YAML lives in this plugin; installing copies it to
    ``<hermes_home>/skins/<theme>.yaml`` so the desktop Appearance list
    (and the Theme Switcher plugin, if installed) picks it up like any
    other skin. Refuses locked rewards.
    """
    data = evaluate_all()
    aggregate = data.get("aggregate") or {}
    unlocked_ids = {a["id"] for a in data.get("achievements", []) if a.get("unlocked")}
    evaluated = evaluate_rewards(data.get("achievements", []), aggregate, unlocked_ids)
    reward = next((r for r in evaluated if r["id"] == reward_id), None)
    if not reward:
        return {"ok": False, "error": f"unknown reward '{reward_id}'"}
    if not reward["unlocked"]:
        return {"ok": False, "error": f"reward '{reward_id}' is not unlocked yet"}
    theme = reward.get("theme", "")
    yaml_text = REWARD_THEMES.get(theme)
    if not yaml_text:
        return {"ok": False, "error": f"no theme payload for '{theme}'"}
    try:
        skins_dir = get_hermes_home() / "skins"
        skins_dir.mkdir(parents=True, exist_ok=True)
        dest = skins_dir / f"{theme}.yaml"
        dest.write_text(yaml_text, encoding="utf-8")
    except Exception as exc:
        return {"ok": False, "error": f"failed to install reward theme: {exc}"}
    return {"ok": True, "installed": theme}


@router.get("/custom-goals")
async def custom_goals():
    data = evaluate_all()
    aggregate = data.get("aggregate") or {}
    return {"ok": True, "goals": evaluate_custom_goals(load_custom_goals(), aggregate, data.get("sessions", [])), "options": CUSTOM_METRIC_METRICS}


@router.post("/custom-goals")
async def create_custom_goal(body: Dict[str, Any]):
    name = str(body.get("name") or "").strip()
    metric = str(body.get("metric") or "").strip()
    target = int(body.get("target") or 0)
    if not name or metric not in CUSTOM_METRIC_METRICS or target <= 0:
        return {"ok": False, "error": "name, a valid metric, and a positive target are required"}
    goals = load_custom_goals()
    goal = {
        "id": f"cg_{int(time.time())}_{len(goals)}",
        "name": name[:80],
        "metric": metric,
        "target": target,
        "created_at": int(time.time()),
    }
    goals.append(goal)
    save_custom_goals(goals)
    return {"ok": True, "goal": goal}


@router.delete("/custom-goals/{goal_id}")
async def delete_custom_goal(goal_id: str):
    goals = load_custom_goals()
    remaining = [g for g in goals if g.get("id") != goal_id]
    if len(remaining) == len(goals):
        return {"ok": False, "error": f"unknown goal '{goal_id}'"}
    save_custom_goals(remaining)
    return {"ok": True}


@router.get("/badge.svg")
async def badge_svg():
    """Flat README badge: level + tier + streak as an SVG shield.

    Usable in any README: ![](https://host/api/plugins/hermes-achievements/badge.svg)
    Uses only system fonts and plain shapes, no external assets.
    """
    data = evaluate_all()
    achievements = data.get("achievements", [])
    agg = data.get("aggregate") or {}
    level = compute_xp(achievements)
    unlocked = sum(1 for a in achievements if a.get("unlocked"))
    streak = int(agg.get("current_streak_days") or 0)
    total = len(achievements)

    left = "Hermes"
    right = f"Lv{level['level']} · {unlocked}/{total}"
    if streak >= 2:
        right = f"Lv{level['level']} · {unlocked}/{total} · 🔥{streak}"

    left_w = 8 + len(left) * 6.2
    right_w = 8 + len(right) * 6.2
    total_w = left_w + right_w

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{int(total_w)}" height="20" role="img" aria-label="Hermes achievements: {right}">
  <linearGradient id="s" x2="0" y2="100%">
    <stop offset="0" stop-color="#bbb" stop-opacity=".1"/>
    <stop offset="1" stop-opacity=".1"/>
  </linearGradient>
  <clipPath id="r"><rect width="{int(total_w)}" height="20" rx="3" fill="#fff"/></clipPath>
  <g clip-path="url(#r)">
    <rect width="{int(left_w)}" height="20" fill="#555"/>
    <rect x="{int(left_w)}" width="{int(right_w)}" height="20" fill="#7B2D8E"/>
    <rect width="{int(total_w)}" height="20" fill="url(#s)"/>
  </g>
  <g fill="#fff" text-anchor="middle" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">
    <text x="{int(left_w / 2)}" y="14">{left}</text>
    <text x="{int(left_w + right_w / 2)}" y="14">{right}</text>
  </g>
</svg>'''
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/badge-wall.svg")
async def badge_wall_svg():
    """Full badge collection as an SVG poster.

    Renders every achievement in a uniform grid, colored by category, with
    tier chips and progress bars. Ready to screenshot for social posts or
    embed anywhere an SVG is accepted. No external assets.
    """
    data = evaluate_all()
    achievements = data.get("achievements", [])
    level = compute_xp(achievements, data.get("aggregate") or {})
    unlocked = sum(1 for a in achievements if a.get("unlocked"))

    cols = 8
    card_w = 150
    card_h = 92
    gap = 12
    pad = 28
    rows = (len(achievements) + cols - 1) // cols
    width = pad * 2 + cols * card_w + (cols - 1) * gap
    height = pad * 2 + rows * card_h + (rows - 1) * gap

    def esc(s):
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # Category hue map (matches the desktop plugin).
    cat_hue = {
        "Agent Autonomy": 250, "Debugging Chaos": 15, "Hermes Native": 205,
        "Lifestyle": 150, "Model Lore": 330, "Research/Web": 275,
        "Sets": 45, "Tool Mastery": 190, "Vibe Coding": 0,
    }
    def cat_color(cat):
        return f"hsl({cat_hue.get(cat, 220)} 55% 45%)"

    cards = []
    for idx, a in enumerate(achievements):
        r, c = divmod(idx, cols)
        x = pad + c * (card_w + gap)
        y = pad + r * (card_h + gap)
        color = cat_color(a.get("category", ""))
        name = "???" if a.get("state") == "secret" else (a.get("name") or "")
        tier = a.get("tier") or ""
        pct = min(100, int(a.get("progress_pct") or 0)) if not a.get("unlocked") else 100
        fill = "hsl(45 90% 92%)" if a.get("kind") == "collection" else ("hsl(220 15% 96%)" if not a.get("unlocked") else "hsl(0 0% 94%)")
        cards.append(f'''
    <g transform="translate({x},{y})">
      <rect width="{card_w}" height="{card_h}" rx="8" fill="{fill}" stroke="{color}" stroke-opacity="0.5" stroke-width="1.5"/>
      <rect width="4" height="{card_h}" rx="2" fill="{color}"/>
      <text x="12" y="22" font-size="10.5" font-weight="600" fill="#333" font-family="Verdana,DejaVu Sans,sans-serif">{esc(name)}</text>
      <text x="12" y="38" font-size="7.5" fill="#888" font-family="Verdana,DejaVu Sans,sans-serif">{esc(a.get("category", ""))}</text>
      <rect x="12" y="46" width="{card_w - 24}" height="5" rx="2.5" fill="#e5e5e5"/>
      <rect x="12" y="46" width="{(card_w - 24) * pct // 100}" height="5" rx="2.5" fill="{color}"/>
      <text x="12" y="70" font-size="8" font-weight="600" fill="{color}" font-family="Verdana,DejaVu Sans,sans-serif">{esc(tier) if tier else ("EARNED" if a.get("unlocked") else "locked")}</text>
      <text x="{card_w - 12}" y="70" text-anchor="end" font-size="8" fill="#999" font-family="Verdana,DejaVu Sans,sans-serif">{pct}%</text>
    </g>''')

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="{width}" height="{height}" fill="#fafafa"/>
  <text x="{pad}" y="18" font-size="14" font-weight="700" fill="#333" font-family="Verdana,DejaVu Sans,sans-serif">Hermes Achievements — {unlocked}/{len(achievements)} unlocked · Level {level['level']} {level['name']}</text>
  {''.join(cards)}
</svg>'''
    return Response(content=svg, media_type="image/svg+xml")


@router.get("/scan-status")
async def scan_status():
    return _scan_status_payload()


@router.get("/recent-unlocks")
async def recent_unlocks():
    data = evaluate_all()
    return sorted([a for a in data["achievements"] if a["unlocked"]], key=lambda a: a.get("unlocked_at") or 0, reverse=True)[:20]


def _resolve_stored_session_id(session_id: str) -> str:
    """Resolve a runtime session id to its stored session id.

    The desktop plugin host exposes ``host.state.activeSessionId`` as the
    gateway's RUNTIME session id (the in-memory key in ``tui_gateway``'s
    ``_sessions`` map), which is different from the STORED session id
    (e.g. ``20260807_170436_b7b698``) that the scan snapshot is keyed by.
    Without translation, the per-session badges endpoint can never find the
    live session and "This session" renders empty even when badges exist.

    Resolution strategy (cheapest first):
      1. If the id already matches a stored session, return it unchanged.
      2. If the gateway is running in this process (it is: the plugin API is
         mounted inside the same web server that serves /api/ws), translate
         the runtime id via tui_gateway's live ``_sessions`` registry.
      3. Fall back to the id as-is when the gateway isn't reachable so the
         endpoint never 500s.
    """
    if not session_id:
        return session_id

    # Fast path: already a stored session id.
    try:
        data = evaluate_all()
        if any(s["session_id"] == session_id for s in data["sessions"]):
            return session_id
    except Exception:
        pass

    # Runtime -> stored translation via the live gateway session registry.
    try:
        from tui_gateway.server import _sessions, _sessions_lock
        with _sessions_lock:
            session = _sessions.get(session_id)
            if session:
                key = session.get("session_key") or ""
                if key:
                    return key
    except Exception:
        pass

    return session_id


@router.get("/sessions/{session_id}/badges")
async def session_badges(session_id: str):
    data = evaluate_all()
    resolved_id = _resolve_stored_session_id(session_id)
    session = next((s for s in data["sessions"] if s["session_id"] == resolved_id), None)
    if not session:
        return {"session_id": session_id, "badges": []}
    aggregate = aggregate_stats([session])
    badges = []
    for definition in ACHIEVEMENTS:
        try:
            result = evaluate_definition(definition, aggregate)
        except Exception:
            # Some definitions cannot be evaluated against a single session
            # (secret achievements with hidden criteria, collection badges,
            # anything without a metric/threshold key). Skipping them is
            # correct for the per-session view; crashing on one definition
            # used to 500 the whole endpoint, so "Badges this session"
            # never populated for any session.
            continue
        if result.get("unlocked"):
            badges.append(display_achievement({**definition, **result}))
    return {"session_id": session_id, "badges": badges}


@router.post("/rescan")
async def rescan():
    return {"ok": True, **evaluate_all(force=True)}


@router.post("/reset-state")
async def reset_state():
    global _SNAPSHOT_CACHE, _SNAPSHOT_CACHE_AT
    save_state({"unlocks": {}})
    _SNAPSHOT_CACHE = None
    _SNAPSHOT_CACHE_AT = 0
    _SCAN_STATUS["state"] = "idle"
    _SCAN_STATUS["started_at"] = None
    _SCAN_STATUS["finished_at"] = None
    _SCAN_STATUS["last_error"] = None
    _SCAN_STATUS["last_duration_ms"] = None
    try:
        snapshot_path().unlink(missing_ok=True)
    except Exception:
        pass
    try:
        checkpoint_path().unlink(missing_ok=True)
    except Exception:
        pass
    return {"ok": True}
