#!/usr/bin/env python3
"""Stoner Hideout bot, all in one file. Built by build_bundle.py.
Needs the STONER_TOKEN environment variable. Never put the token in this file."""
import importlib.abc
import importlib.util
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_SOURCES = {
    'config': '''"""Settings for the Stoner Hideout bot. No secrets in here — the token comes from the environment."""
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).parent
DB_PATH = BASE / "data" / "stoner.db"

# All 4:20 times use US Central time.
TZ = ZoneInfo("America/Chicago")

# Age the verify button asks members to confirm (server is 18+).
MIN_AGE = 18
# Each name can be a list. The first name that exists in the server is used.
# These match the existing Stoner Hideout server (emoji and spacing are ignored when matching).
VERIFIED_ROLE = ["✅ Verified", "✅Verified Smoker💨"]
MOD_ROLE = ["Mod Team", "👷 staff team"]
ADMIN_ROLE = ["🛡️ Admin", "❌ Server Manager🦺"]

CHANNELS = {
    "welcome": ["👋-welcome", "pull-up-a-chair"],
    "verify": ["✅-verify", "verify-here"],
    "support": ["🆘-support"],
    "roles": ["🎭-roles", "pick-your-stash"],
    "general": ["💬-general", "the-couch"],
    "mod_logs": ["📋-mod-logs", "paper-trail"],
}
CATEGORY_MAIN = "🌿 STONER HIDEOUT"
CATEGORY_TICKETS = "🎫 TICKETS"

# Self-assign roles offered in the dropdown in #roles. (name, emoji, description, colour)
GAME_ROLES = [
    ("Gamer", "🎮", "Game nights and lobbies", "#9B59B6"),
    ("Movie Night", "🎬", "Watch parties and show talk", "#E74C3C"),
    ("Munchie Monster", "🍕", "Snacks and food talk", "#F39C12"),
    ("Music & Smoke", "🎶", "Playlists and jam sessions", "#1ABC9C"),
    ("Night Owl", "🦉", "Late-night chats", "#5C6BC0"),
]

# Anti-spam and raid protection
SPAM_WINDOW_SECONDS = 5      # sliding window for message spam
SPAM_MAX_MESSAGES = 6        # more than this in the window = spam
MENTION_LIMIT = 5            # this many user/role pings in one message = mass mention
AUTO_TIMEOUT_MINUTES = 10
RAID_WINDOW_SECONDS = 10
RAID_JOIN_LIMIT = 8          # this many joins inside the window = possible raid
RAID_ALERT_COOLDOWN = 300    # seconds between raid alerts

# 420 features
CHECKIN_POINTS = 25
TRIVIA_POINTS = 10
CHECKIN_MINUTES = 10         # check-in opens this many minutes before/after 4:20
''',
    'db': '''"""Small SQLite store: server settings, player points, daily check-ins, warnings."""
import sqlite3
import threading
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS config (
                    guild_id INTEGER NOT NULL,
                    key      TEXT    NOT NULL,
                    value    TEXT,
                    PRIMARY KEY (guild_id, key)
                );
                CREATE TABLE IF NOT EXISTS players (
                    guild_id     INTEGER NOT NULL,
                    user_id      INTEGER NOT NULL,
                    points       INTEGER NOT NULL DEFAULT 0,
                    trivia_wins  INTEGER NOT NULL DEFAULT 0,
                    last_checkin TEXT,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS warnings (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id   INTEGER NOT NULL,
                    user_id    INTEGER NOT NULL,
                    mod_id     INTEGER NOT NULL,
                    reason     TEXT    NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )

        self._migrate()

    def _migrate(self) -> None:
        """Add columns that newer versions need. Safe to run on every start."""
        for ddl in ("ALTER TABLE players ADD COLUMN last_daily TEXT",
                    "ALTER TABLE players ADD COLUMN streak INTEGER NOT NULL DEFAULT 0",
                    "ALTER TABLE players ADD COLUMN xp INTEGER NOT NULL DEFAULT 0"):
            try:
                with self._connect() as db:
                    db.execute(ddl)
            except sqlite3.OperationalError:
                pass  # column already exists

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def _run(self, sql, params=(), fetch=None):
        with self._lock, self._connect() as db:
            cur = db.execute(sql, params)
            if fetch == "one":
                row = cur.fetchone()
                return dict(row) if row else None
            if fetch == "all":
                return [dict(r) for r in cur.fetchall()]
            return cur.rowcount

    # ── config ──
    def get_config(self, guild_id: int, key: str):
        row = self._run("SELECT value FROM config WHERE guild_id=? AND key=?",
                        (guild_id, key), fetch="one")
        return row["value"] if row else None

    def set_config(self, guild_id: int, key: str, value: str) -> None:
        self._run("INSERT INTO config(guild_id, key, value) VALUES(?,?,?) "
                  "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
                  (guild_id, key, value))

    # ── players and points ──
    def get_player(self, guild_id: int, user_id: int) -> dict:
        row = self._run("SELECT * FROM players WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id), fetch="one")
        return row or {"guild_id": guild_id, "user_id": user_id, "points": 0,
                       "trivia_wins": 0, "last_checkin": None}

    def add_points(self, guild_id: int, user_id: int, points: int, trivia_win: bool = False) -> None:
        self._run("INSERT INTO players(guild_id, user_id, points, trivia_wins) VALUES(?,?,?,?) "
                  "ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                  "points = points + excluded.points, "
                  "trivia_wins = trivia_wins + excluded.trivia_wins",
                  (guild_id, user_id, points, 1 if trivia_win else 0))

    def claim_checkin(self, guild_id: int, user_id: int, date_str: str) -> bool:
        """Mark today's check-in. Returns False if the user already checked in today."""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT last_checkin FROM players WHERE guild_id=? AND user_id=?",
                             (guild_id, user_id)).fetchone()
            if row and row["last_checkin"] == date_str:
                return False
            db.execute("INSERT INTO players(guild_id, user_id, points, last_checkin) VALUES(?,?,0,?) "
                       "ON CONFLICT(guild_id, user_id) DO UPDATE SET last_checkin=excluded.last_checkin",
                       (guild_id, user_id, date_str))
            return True

    def leaderboard(self, guild_id: int, limit: int = 10) -> list[dict]:
        return self._run("SELECT user_id, points, trivia_wins FROM players WHERE guild_id=? "
                         "ORDER BY points DESC, trivia_wins DESC LIMIT ?",
                         (guild_id, limit), fetch="all")

    # ── warnings ──
    def add_warning(self, guild_id: int, user_id: int, mod_id: int, reason: str, created_at: int) -> int:
        with self._lock, self._connect() as db:
            cur = db.execute("INSERT INTO warnings(guild_id, user_id, mod_id, reason, created_at) "
                             "VALUES(?,?,?,?,?)", (guild_id, user_id, mod_id, reason, created_at))
            return cur.lastrowid

    def list_warnings(self, guild_id: int, user_id: int) -> list[dict]:
        return self._run("SELECT id, mod_id, reason, created_at FROM warnings "
                         "WHERE guild_id=? AND user_id=? ORDER BY id DESC LIMIT 20",
                         (guild_id, user_id), fetch="all")

    def spend_points(self, guild_id: int, user_id: int, amount: int) -> bool:
        """Take points away, but only if the player has enough. Returns False if they don't."""
        return self._run("UPDATE players SET points = points - ? WHERE guild_id=? AND user_id=? AND points >= ?",
                         (amount, guild_id, user_id, amount)) == 1

    def claim_daily(self, guild_id: int, user_id: int, today: str, yesterday: str):
        """Returns the new streak, or None if already claimed today."""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT last_daily, streak FROM players WHERE guild_id=? AND user_id=?",
                             (guild_id, user_id)).fetchone()
            last = row["last_daily"] if row else None
            if last == today:
                return None
            streak = (row["streak"] if row else 0) + 1 if last == yesterday else 1
            db.execute("INSERT INTO players(guild_id, user_id, points, last_daily, streak) VALUES(?,?,0,?,?) "
                       "ON CONFLICT(guild_id, user_id) DO UPDATE SET last_daily=excluded.last_daily, "
                       "streak=excluded.streak", (guild_id, user_id, today, streak))
            return streak

    def rank(self, guild_id: int, user_id: int) -> int:
        row = self._run("SELECT points FROM players WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id), fetch="one")
        points = row["points"] if row else 0
        above = self._run("SELECT COUNT(*) AS n FROM players WHERE guild_id=? AND points > ?",
                          (guild_id, points), fetch="one")
        return above["n"] + 1

    def count_warnings(self, guild_id: int, user_id: int) -> int:
        row = self._run("SELECT COUNT(*) AS n FROM warnings WHERE guild_id=? AND user_id=?",
                        (guild_id, user_id), fetch="one")
        return row["n"]

    def add_xp(self, guild_id: int, user_id: int, amount: int) -> tuple[int, int]:
        """Add XP. Returns (xp before, xp after) so the caller can spot a level-up."""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT xp FROM players WHERE guild_id=? AND user_id=?",
                             (guild_id, user_id)).fetchone()
            before = row["xp"] if row else 0
            db.execute("INSERT INTO players(guild_id, user_id, points, xp) VALUES(?,?,0,?) "
                       "ON CONFLICT(guild_id, user_id) DO UPDATE SET xp = xp + excluded.xp",
                       (guild_id, user_id, amount))
            return before, before + amount
''',
    'logic': '''"""Pure game and security logic. No Discord calls here, so it can be unit-tested."""
import random
import re
from collections import defaultdict, deque
from datetime import datetime, time, timedelta

import config

# ───────────────────────── 4:20 clock ─────────────────────────

FOUR_TWENTY_MINUTES = (4 * 60 + 20, 16 * 60 + 20)  # 4:20 AM and 4:20 PM, in minutes after midnight


def next_420(now: datetime) -> datetime:
    """The next 4:20 (AM or PM) after `now`. `now` must be timezone-aware."""
    candidates = []
    for day_offset in (0, 1):
        day = now.date() + timedelta(days=day_offset)
        for hour in (4, 16):
            candidates.append(datetime.combine(day, time(hour, 20), tzinfo=now.tzinfo))
    return min(c for c in candidates if c > now)


def in_checkin_window(now: datetime, minutes: int = config.CHECKIN_MINUTES) -> bool:
    """True within `minutes` of 4:20 AM or 4:20 PM."""
    current = now.hour * 60 + now.minute
    return any(abs(current - mark) <= minutes for mark in FOUR_TWENTY_MINUTES)


def is_420_day(now: datetime) -> bool:
    return now.month == 4 and now.day == 20


def format_countdown(seconds: float) -> str:
    seconds = int(max(0, seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes:02d}m {secs:02d}s"


# ───────────────────────── trivia ─────────────────────────

TRIVIA = [
    {"q": "What plant family does cannabis belong to?",
     "choices": ["Cannabaceae", "Solanaceae", "Lamiaceae", "Poaceae"], "answer": 0,
     "fact": "Cannabis is in the Cannabaceae family, along with hops."},
    {"q": "Which compound is the main psychoactive cannabinoid in cannabis?",
     "choices": ["CBD", "THC", "CBG", "CBC"], "answer": 1,
     "fact": "THC (delta-9-tetrahydrocannabinol) is the main psychoactive cannabinoid."},
    {"q": "CBD stands for what?",
     "choices": ["Cannabidiol", "Cannabinoid Blend", "Cellular Bud Dose", "Cannabis Base Drug"], "answer": 0,
     "fact": "CBD is short for cannabidiol."},
    {"q": "Which body system do cannabinoids interact with?",
     "choices": ["Endocannabinoid system", "Lymphatic system", "Digestive system", "Respiratory system"],
     "answer": 0,
     "fact": "The endocannabinoid system is the body's own network that cannabinoids act on."},
    {"q": "What are the sticky resin glands on cannabis flowers called?",
     "choices": ["Trichomes", "Stomata", "Pistils", "Sepals"], "answer": 0,
     "fact": "Trichomes are the tiny resin-producing glands on the plant."},
    {"q": "What gives different cannabis strains their smell and flavor?",
     "choices": ["Terpenes", "Chlorophyll", "Nicotine", "Caffeine"], "answer": 0,
     "fact": "Terpenes are aromatic compounds that shape the smell and flavor."},
    {"q": "In the US, hemp is legally defined as cannabis with no more than what dry-weight THC?",
     "choices": ["0.3%", "3%", "10%", "0.03%"], "answer": 0,
     "fact": "The 2018 Farm Bill set the hemp limit at 0.3% delta-9 THC on a dry-weight basis."},
    {"q": "What federal law in 2018 made hemp farming legal in the US?",
     "choices": ["The Farm Bill", "The Clean Air Act", "The Tax Cuts Act", "The Sunshine Act"],
     "answer": 0,
     "fact": "The 2018 Farm Bill legalized hemp at the federal level."},
    {"q": "According to popular legend, the name '4/20' started with a group of students in which city?",
     "choices": ["San Rafael, California", "Amsterdam", "Denver", "Austin"], "answer": 0,
     "fact": "The San Rafael story is a popular legend, not a documented fact."},
    {"q": "What is the nickname for the increased appetite cannabis can cause?",
     "choices": ["The munchies", "The jitters", "The sweats", "The zoomies"], "answer": 0,
     "fact": "It's called 'the munchies.'"},
]


def pick_trivia(rng: random.Random):
    return rng.choice(TRIVIA)


def is_correct(question: dict, choice_index: int) -> bool:
    return choice_index == question["answer"]


# ───────────────────────── fun picks ─────────────────────────

MUNCHIES = [
    ("Cheesy puffs", "Orange fingers are a badge of honor."),
    ("Pizza bagels", "Ten minutes in the oven and you're a chef."),
    ("Cinnamon toast crunch", "Breakfast for dinner, no notes."),
    ("Gas station nachos", "Questionable, but somehow perfect."),
    ("Frozen waffles with syrup", "Warm, sticky, and no dishes."),
    ("Ramen with an egg", "Five minutes and it feels fancy."),
    ("Gummy worms", "Chewy, colorful, and very committed to the bit."),
    ("A whole sleeve of Oreos", "Twist, lick, dunk, repeat."),
    ("Cold pizza, eaten standing up", "Yes, straight from the box."),
    ("Chips and salsa", "The classic. Never disappoints."),
]

STRAIN_ADJECTIVES = ["Purple", "Sour", "Sleepy", "Giggly", "Cosmic", "Lazy", "Crispy", "Golden",
                     "Midnight", "Sunny", "Dreamy", "Turbo", "Fuzzy", "Mellow", "Electric"]
STRAIN_NOUNS = ["Haze", "Kush", "Moon", "Dream", "Pancake", "Donut", "Thunder", "Sunset",
                "Nebula", "Puddle", "Cloud", "Taco", "Comet", "Gummy", "Lightning"]
STRAIN_TAILS = ["", " #2", " Deluxe", " XL", " Express", " Reserve", " Og", " 420"]

WOULD_YOU_RATHER = [
    ("have a bottomless snack drawer", "have a bottomless playlist"),
    ("only eat breakfast food forever", "only eat pizza forever"),
    ("always have the perfect song playing", "always have the perfect snack appear when you're hungry"),
    ("be able to talk to animals", "be able to speak every language"),
    ("live in a treehouse", "live in a houseboat"),
    ("have free Wi-Fi everywhere", "have free delivery from your favorite spot"),
    ("never have to do dishes", "never have to do laundry"),
    ("win the lottery", "get a lifetime supply of your favorite snack"),
]

VIBE_LINES = [
    (0, 20, "Cloudy with a chance of couch."),
    (21, 40, "Chill, but you might need a snack."),
    (41, 60, "Solid vibes, good energy."),
    (61, 80, "Big mood. Keep it rolling."),
    (81, 100, "Off the charts. The hideout salutes you."),
]


def strain_name(rng: random.Random) -> str:
    return f"{rng.choice(STRAIN_ADJECTIVES)} {rng.choice(STRAIN_NOUNS)}{rng.choice(STRAIN_TAILS)}"


def vibe_score(seed: str) -> tuple[int, str]:
    """Same seed (e.g. user id + date) always gives the same score for the day."""
    rng = random.Random(seed)
    score = rng.randint(0, 100)
    for low, high, line in VIBE_LINES:
        if low <= score <= high:
            return score, line
    return score, VIBE_LINES[-1][2]


# ───────────────────────── anti-spam and raid tracking ─────────────────────────

class SpamTracker:
    """Flags a user who sends more than `max_messages` messages inside `window` seconds."""

    def __init__(self, window: float, max_messages: int):
        self.window = window
        self.max_messages = max_messages
        self._hits = defaultdict(deque)

    def hit(self, user_id: int, timestamp: float) -> bool:
        q = self._hits[user_id]
        q.append(timestamp)
        while q and timestamp - q[0] > self.window:
            q.popleft()
        return len(q) > self.max_messages

    def reset(self, user_id: int) -> None:
        self._hits.pop(user_id, None)


class JoinTracker:
    """Flags a possible raid: `limit` joins inside `window` seconds (per guild)."""

    def __init__(self, window: float, limit: int):
        self.window = window
        self.limit = limit
        self._joins = defaultdict(deque)

    def hit(self, guild_id: int, timestamp: float) -> bool:
        q = self._joins[guild_id]
        q.append(timestamp)
        while q and timestamp - q[0] > self.window:
            q.popleft()
        return len(q) >= self.limit


def mass_mention(user_mentions: int, role_mentions: int) -> bool:
    return user_mentions + role_mentions >= config.MENTION_LIMIT


# ── Arcade (added in v2) ──────────────────────────────────────────────

SLOT_SYMBOLS = ["🍒", "🍋", "🔔", "💎", "7️⃣", "🌿"]
# Payout multiplier for three of a kind. Two of a kind returns your bet (1x). No match loses the bet.
SLOT_TRIPLE_MULT = {"🍒": 2, "🍋": 3, "🔔": 4, "💎": 8, "7️⃣": 6, "🌿": 10}
SLOT_PAIR_MULT = 1


def spin_slots(rng: random.Random) -> tuple[list[str], int]:
    """Returns (three reels, payout multiplier). 0 means the bet is lost."""
    reels = [rng.choice(SLOT_SYMBOLS) for _ in range(3)]
    if reels[0] == reels[1] == reels[2]:
        return reels, SLOT_TRIPLE_MULT[reels[0]]
    if len(set(reels)) == 2:
        return reels, SLOT_PAIR_MULT
    return reels, 0


def rps_winner(a: str, b: str) -> int:
    """Returns 0 for a draw, 1 if a wins, 2 if b wins. Moves: rock, paper, scissors."""
    if a == b:
        return 0
    beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    return 1 if beats[a] == b else 2


TRUTHS = [
    "What's the best meal you've had this year?",
    "What song always gets you hyped?",
    "What's your most useless talent?",
    "What show are you rewatching right now?",
    "What's the best road trip snack, no debate?",
    "What's a hobby you wish you had more time for?",
    "Who in this server would you pick first for a trivia team?",
    "What's the best thing that happened to you this month?",
]
DARES = [
    "Post a photo of your pet, or a plant, or the view out your window.",
    "Send a voice note saying 'it's giggle o'clock' in the chat.",
    "Type your next message in all caps.",
    "Pick a song for the group and say why in one sentence.",
    "Post your favorite emoji and explain why it's the best.",
    "Do 10 jumping jacks, then post 🏃 to prove it.",
    "Share a playlist you're into (no explicit lyrics, please).",
]

SCRAMBLE_WORDS = [
    "mellow", "sunset", "munchies", "giggles", "chill", "pancake", "bubble", "rainbow",
    "galaxy", "jukebox", "campfire", "playlist", "sandwich", "sprinkles", "hammock", "lullaby",
    "cookie", "thunder", "compass", "monsoon", "blanket", "pillow", "kitten", "orbit",
]


def scramble(word: str, rng: random.Random) -> str:
    letters = list(word)
    for _ in range(50):
        rng.shuffle(letters)
        if "".join(letters) != word:
            return "".join(letters)
    return word[::-1]


def answer_matches(guess: str, word: str) -> bool:
    return guess.strip().lower() == word.lower()


FORTUNES = [
    "A good snack will show up right when you need it. 🍪",
    "Someone in the server is about to make you laugh out loud. 😂",
    "Your playlist is about to save the day. 🎧",
    "A quiet night with good company is in your future. 🌙",
    "You will win a trivia round soon. Study up. 🧠",
    "A new friend is closer than you think. 👋",
    "Your next idea is a good one. Write it down. ✍️",
    "The best plans are the ones with snacks. 🍕",
]


def daily_reward(streak: int) -> int:
    """10 points base, plus 2 per streak day, capped at 7 days (so 10 to 24 points)."""
    return 10 + 2 * min(max(streak, 1), 7)


def account_age_days(created_at: datetime, now: datetime) -> float:
    return (now - created_at).total_seconds() / 86400


INVITE_RE = re.compile(r"(?:discord\\.gg|discord(?:app)?\\.com/invite)/[A-Za-z0-9-]+", re.IGNORECASE)


def has_invite_link(text: str) -> bool:
    return bool(INVITE_RE.search(text or ""))


# ── XP and levels (earned by chatting, with a cooldown so spam doesn't farm it) ──
XP_PER_MESSAGE = 5
XP_COOLDOWN_SECONDS = 60
XP_PER_LEVEL = 100


def level_progress(xp: int) -> tuple[int, int, int]:
    """Returns (level, xp earned into this level, xp needed to reach the next level).
    Level 2 needs 100 total, level 3 needs 300 total, and each level needs 100 more than the last."""
    level, floor = 1, 0
    while xp >= floor + XP_PER_LEVEL * level:
        floor += XP_PER_LEVEL * level
        level += 1
    return level, xp - floor, XP_PER_LEVEL * level


EIGHT_BALL = [
    "It is certain.", "Without a doubt.", "Yes, definitely.", "Looks good to me.",
    "Ask again later.", "Better not tell you now.", "Cannot predict that.",
    "Don't count on it.", "My sources say no.", "Very doubtful.",
]
HOT_TAKES = [
    "Pineapple on pizza is elite.",
    "Cereal is a soup.",
    "Movies are better than the books, usually.",
    "Mornings are overrated, but sunrises are not.",
    "A good playlist beats a good album.",
    "Breakfast food is the best food at any hour.",
    "Board games beat video games for group nights.",
    "The best snack is the one you find in the car.",
]
''',
    'utils': '''"""Helpers shared by all cogs."""
import re
import discord

import config


def key(text: str) -> str:
    """Lowercase letters and digits only, so '✅-verify' and 'verify' compare equal."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _candidates(name) -> list[str]:
    return [name] if isinstance(name, str) else list(name)


def find_text(guild: discord.Guild, name):
    """Find a text channel by name, or the first match from a list of names."""
    for wanted in _candidates(name):
        channel = next((c for c in guild.text_channels if key(c.name) == key(wanted)), None)
        if channel is not None:
            return channel
    return None


def find_role(guild: discord.Guild, name):
    for wanted in _candidates(name):
        role = next((r for r in guild.roles if key(r.name) == key(wanted)), None)
        if role is not None:
            return role
    return None


def find_category(guild: discord.Guild, name):
    for wanted in _candidates(name):
        category = next((c for c in guild.categories if key(c.name) == key(wanted)), None)
        if category is not None:
            return category
    return None


def is_staff(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_messages or perms.moderate_members


async def log_to_mods(guild: discord.Guild, text: str, *, ping_roles: bool = False, view=None) -> None:
    """Post to #mod-logs. Only the Mod Team role can be pinged, and only when ping_roles=True.
    User mentions are shown as names and don't notify anyone."""
    channel = find_text(guild, config.CHANNELS["mod_logs"])
    if channel is None:
        return
    allowed = discord.AllowedMentions(everyone=False, roles=ping_roles, users=False, replied_user=False)
    try:
        await channel.send(text[:1900], allowed_mentions=allowed, view=view or discord.utils.MISSING)
    except discord.HTTPException:
        pass


async def reuse_or_post(bot, channel: discord.TextChannel, db_key: str, guild_id: int,
                        embed: discord.Embed, view: discord.ui.View) -> str:
    """Post a panel once. If it's already there (and ours), leave it. If it was posted by
    another bot, replace it so its buttons actually work."""
    saved = bot.db.get_config(guild_id, db_key)
    if saved:
        try:
            msg = await channel.fetch_message(int(saved))
            if msg.author.id == bot.user.id:
                return "exists"
            await msg.delete()
            status = "replaced"
        except discord.NotFound:
            status = "posted"
    else:
        status = "posted"
    msg = await channel.send(embed=embed, view=view)
    bot.db.set_config(guild_id, db_key, str(msg.id))
    return status
''',
    'cog_arcade': '''"""Arcade: slots, duels, truth or dare, word scramble, daily rewards, fortunes, profiles, help."""
import random
import time
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

import logic
from cog_games import FOOTER, now_local

SLOT_BET = 5
SLOT_COOLDOWN = 15          # seconds between spins per player
DUEL_POINTS = 10
SCRAMBLE_POINTS = 8
MOVES = {"rock": "✊ Rock", "paper": "🖐️ Paper", "scissors": "✌️ Scissors"}


class Arcade(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rng = random.Random()
        self._slot_cooldown: dict[tuple[int, int], float] = {}
        self._xp_cooldown: dict[tuple[int, int], float] = {}

    # ── XP from chatting (no message text is read) ─────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not isinstance(message.author, discord.Member):
            return
        key = (message.guild.id, message.author.id)
        now = time.time()
        if now < self._xp_cooldown.get(key, 0):
            return
        self._xp_cooldown[key] = now + logic.XP_COOLDOWN_SECONDS
        before, after = self.bot.db.add_xp(message.guild.id, message.author.id, logic.XP_PER_MESSAGE)
        old_level = logic.level_progress(before)[0]
        new_level = logic.level_progress(after)[0]
        if new_level > old_level:
            await message.channel.send(f"🎉 **{message.author.display_name}** reached **level {new_level}**!",
                                       delete_after=120, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="rank", description="Your level and XP (or someone else's).")
    @app_commands.describe(member="Whose rank (optional)")
    @app_commands.guild_only()
    async def rank(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        if target.bot:
            await interaction.response.send_message("Bots don't level up. 🤖", ephemeral=True)
            return
        player = self.bot.db.get_player(interaction.guild.id, target.id)
        level, into, need = logic.level_progress(player.get("xp", 0) or 0)
        filled = round(10 * into / need)
        bar = "🟩" * filled + "⬜" * (10 - filled)
        embed = discord.Embed(title=f"⭐ {target.display_name} · Level {level}",
                              description=f"{bar}\\n{into} / {need} XP to level {level + 1}",
                              colour=discord.Colour.blurple())
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.set_footer(text="Earn XP by chatting. One point every minute, max.")
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="coinflip", description="Heads or tails.")
    @app_commands.guild_only()
    async def coinflip(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"🪙 **{self.rng.choice(['Heads', 'Tails'])}!**")

    @app_commands.command(name="8ball", description="Ask the magic 8-ball a question.")
    @app_commands.describe(question="What do you want to know?")
    @app_commands.guild_only()
    async def eightball(self, interaction: discord.Interaction, question: app_commands.Range[str, 1, 150]):
        await interaction.response.send_message(
            f"🎱 **{question}**\\n→ {self.rng.choice(logic.EIGHT_BALL)}")

    @app_commands.command(name="hottake", description="A random hot take to argue about.")
    @app_commands.guild_only()
    async def hottake(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"🔥 **Hot take:** {self.rng.choice(logic.HOT_TAKES)}\\nAgree or disagree?")

    # ── Slots ──────────────────────────────────────────────

    @app_commands.command(name="slots", description=f"Spin the 420 slots. Costs {SLOT_BET} points.")
    @app_commands.guild_only()
    async def slots(self, interaction: discord.Interaction):
        key = (interaction.guild.id, interaction.user.id)
        wait = self._slot_cooldown.get(key, 0) - time.time()
        if wait > 0:
            await interaction.response.send_message(f"🎰 Slow down! Spin again in {int(wait) + 1}s.", ephemeral=True)
            return
        if not self.bot.db.spend_points(interaction.guild.id, interaction.user.id, SLOT_BET):
            await interaction.response.send_message(
                f"🎰 You need {SLOT_BET} points to spin. Try `/daily` or `/checkin`.", ephemeral=True)
            return
        self._slot_cooldown[key] = time.time() + SLOT_COOLDOWN
        reels, mult = logic.spin_slots(self.rng)
        face = " | ".join(reels)
        if mult == 0:
            result = f"No luck. You lost {SLOT_BET} points."
        else:
            payout = SLOT_BET * mult
            self.bot.db.add_points(interaction.guild.id, interaction.user.id, payout)
            if mult == logic.SLOT_PAIR_MULT:
                result = f"Two of a kind. Your {SLOT_BET} points came back."
            else:
                result = f"🎉 Jackpot! **+{payout - SLOT_BET}** points (x{mult})."
        embed = discord.Embed(title="🎰 420 Slots", description=f"**{face}**\\n\\n{result}",
                              colour=discord.Colour.gold())
        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(embed=embed)

    # ── Duel ───────────────────────────────────────────────

    @app_commands.command(name="duel", description="Challenge someone to rock, paper, scissors.")
    @app_commands.describe(opponent="Who you're challenging")
    @app_commands.guild_only()
    async def duel(self, interaction: discord.Interaction, opponent: discord.Member):
        if opponent.bot or opponent == interaction.user:
            await interaction.response.send_message("Pick a real opponent (not a bot, not yourself).",
                                                    ephemeral=True)
            return
        view = DuelInvite(self.bot, interaction.user, opponent)
        await interaction.response.send_message(
            f"⚔️ {interaction.user.mention} challenges {opponent.mention} to rock, paper, scissors! "
            f"Winner takes **{DUEL_POINTS}** points.",
            view=view, allowed_mentions=discord.AllowedMentions(users=True))
        view.message = await interaction.original_response()

    # ── Truth or dare ──────────────────────────────────────

    @app_commands.command(name="truthordare", description="Truth or dare, group-friendly edition.")
    @app_commands.guild_only()
    async def truthordare(self, interaction: discord.Interaction):
        view = TruthOrDareView()
        await interaction.response.send_message(embed=view.card("start"), view=view)

    # ── Word scramble ──────────────────────────────────────

    @app_commands.command(name="wordscramble", description="Unscramble the word. First correct answer wins points.")
    @app_commands.guild_only()
    async def wordscramble(self, interaction: discord.Interaction):
        word = self.rng.choice(logic.SCRAMBLE_WORDS)
        view = ScrambleView(self.bot, word, logic.scramble(word, self.rng))
        await interaction.response.send_message(embed=view.card(), view=view)
        view.message = await interaction.original_response()

    # ── Daily ──────────────────────────────────────────────

    @app_commands.command(name="daily", description="Claim your daily points. Streaks pay more.")
    @app_commands.guild_only()
    async def daily(self, interaction: discord.Interaction):
        today = now_local().date()
        streak = self.bot.db.claim_daily(interaction.guild.id, interaction.user.id,
                                         today.isoformat(), (today - timedelta(days=1)).isoformat())
        if streak is None:
            await interaction.response.send_message("You already claimed today. Come back tomorrow! 🌅",
                                                    ephemeral=True)
            return
        reward = logic.daily_reward(streak)
        self.bot.db.add_points(interaction.guild.id, interaction.user.id, reward)
        await interaction.response.send_message(
            f"🌅 {interaction.user.mention} claimed **+{reward}** points. Streak: **{streak}** day(s).")

    # ── Fortune ────────────────────────────────────────────

    @app_commands.command(name="fortune", description="Open a fortune cookie.")
    @app_commands.guild_only()
    async def fortune(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🥠 Fortune Cookie", description=self.rng.choice(logic.FORTUNES),
                              colour=discord.Colour.orange())
        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(embed=embed)

    # ── Profile ────────────────────────────────────────────

    @app_commands.command(name="profile", description="Your 420 stats (or someone else's).")
    @app_commands.describe(member="Whose profile (optional)")
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, member: discord.Member | None = None):
        target = member or interaction.user
        if target.bot:
            await interaction.response.send_message("Bots don't have profiles. 🤖", ephemeral=True)
            return
        db = self.bot.db
        player = db.get_player(interaction.guild.id, target.id)
        rank = db.rank(interaction.guild.id, target.id)
        warns = db.count_warnings(interaction.guild.id, target.id)
        embed = discord.Embed(title=f"🌿 {target.display_name}", colour=discord.Colour.green())
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Points", value=str(player["points"]))
        embed.add_field(name="Rank", value=f"#{rank}")
        embed.add_field(name="Trivia wins", value=str(player["trivia_wins"]))
        embed.add_field(name="Daily streak", value=f"{player.get('streak', 0) or 0} day(s)")
        if target == interaction.user or interaction.user.guild_permissions.moderate_members:
            embed.add_field(name="Warnings", value=str(warns))
        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    # ── Help ───────────────────────────────────────────────

    @app_commands.command(name="help", description="What this bot can do.")
    @app_commands.guild_only()
    async def help(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🌿 Stoner Hideout Commands", colour=discord.Colour.green(),
                              description=(
                                  "**🎮 Games**\\n"
                                  "`/slots` · `/duel` · `/trivia` · `/wordscramble` · `/truthordare`\\n"
                                  "`/wouldyourather` · `/munchies` · `/strain` · `/fortune`\\n"
                                  "**🕓 420 & points**\\n"
                                  "`/countdown420` · `/checkin` · `/daily` · `/leaderboard` · `/profile`\\n"
                                  "`/vibecheck` · `/rank` · `/coinflip` · `/8ball` · `/hottake`\\n\\n"
                                  "**🛡️ Staff**\\n"
                                  "`/warn` · `/warnings` · `/timeout` · `/purge` · `/lockdown` · `/slowmode`\\n"
                                  "`/setup-stoner` (admins)"))
        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(embed=embed, ephemeral=True)


class DuelInvite(discord.ui.View):
    def __init__(self, bot: commands.Bot, challenger: discord.Member, opponent: discord.Member):
        super().__init__(timeout=120)
        self.bot = bot
        self.challenger = challenger
        self.opponent = opponent
        self.message: discord.Message | None = None

    @discord.ui.button(label="Accept", emoji="⚔️", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("This challenge is for someone else.", ephemeral=True)
            return
        round_view = DuelRound(self.bot, self.challenger, self.opponent)
        await interaction.response.edit_message(
            content=f"⚔️ **Duel on!** {self.challenger.mention} vs {self.opponent.mention}. "
                    "Pick your move. Moves are locked in until both pick.",
            view=round_view)
        round_view.message = interaction.message
        self.stop()

    @discord.ui.button(label="Decline", emoji="🚫", style=discord.ButtonStyle.secondary)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opponent.id:
            await interaction.response.send_message("This challenge is for someone else.", ephemeral=True)
            return
        await interaction.response.edit_message(content=f"🚫 {self.opponent.mention} declined.", view=None)
        self.stop()

    async def on_timeout(self):
        if self.message:
            try:
                await self.message.edit(content="⌛ Challenge expired.", view=None)
            except discord.HTTPException:
                pass


class DuelRound(discord.ui.View):
    def __init__(self, bot: commands.Bot, challenger: discord.Member, opponent: discord.Member):
        super().__init__(timeout=90)
        self.bot = bot
        self.players = {challenger.id: challenger, opponent.id: opponent}
        self.picks: dict[int, str] = {}
        self.message: discord.Message | None = None

    async def _pick(self, interaction: discord.Interaction, move: str):
        if interaction.user.id not in self.players:
            await interaction.response.send_message("You're not in this duel.", ephemeral=True)
            return
        if interaction.user.id in self.picks:
            await interaction.response.send_message("You already locked in. 🔒", ephemeral=True)
            return
        self.picks[interaction.user.id] = move
        if len(self.picks) < 2:
            await interaction.response.send_message(f"🔒 Locked in: {MOVES[move]}. Waiting on the other player.",
                                                    ephemeral=True)
            return
        await self._resolve(interaction)

    async def _resolve(self, interaction: discord.Interaction):
        a_id, b_id = list(self.players)
        a_move, b_move = self.picks[a_id], self.picks[b_id]
        outcome = logic.rps_winner(a_move, b_move)
        head = (f"{self.players[a_id].mention}: {MOVES[a_move]}\\n"
                f"{self.players[b_id].mention}: {MOVES[b_move]}\\n\\n")
        if outcome == 0:
            text = head + "🤝 It's a draw. No points this time."
        else:
            winner = a_id if outcome == 1 else b_id
            self.bot.db.add_points(interaction.guild.id, winner, DUEL_POINTS)
            text = head + f"🏆 {self.players[winner].mention} wins **+{DUEL_POINTS}** points!"
        for item in self.children:
            item.disabled = True
        self.stop()
        await interaction.response.edit_message(content=text, view=self)

    @discord.ui.button(label="Rock", emoji="✊", style=discord.ButtonStyle.primary)
    async def rock(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "rock")

    @discord.ui.button(label="Paper", emoji="🖐️", style=discord.ButtonStyle.primary)
    async def paper(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "paper")

    @discord.ui.button(label="Scissors", emoji="✌️", style=discord.ButtonStyle.primary)
    async def scissors(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, "scissors")

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(content="⌛ Duel timed out. Nobody wins this one.", view=self)
            except discord.HTTPException:
                pass


class TruthOrDareView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)

    def card(self, kind: str, text: str | None = None) -> discord.Embed:
        if kind == "start":
            return discord.Embed(title="🎲 Truth or Dare", description="Pick one, or tap 🔁 for a new card.",
                                 colour=discord.Colour.purple())
        title = "🤔 Truth" if kind == "truth" else "🔥 Dare"
        return discord.Embed(title=title, description=text, colour=discord.Colour.purple())

    async def _send(self, interaction: discord.Interaction, kind: str, bank: list[str]):
        embed = self.card(kind, random.choice(bank))
        await interaction.response.send_message(embed=embed)

    @discord.ui.button(label="Truth", emoji="🤔", style=discord.ButtonStyle.primary)
    async def truth(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send(interaction, "truth", logic.TRUTHS)

    @discord.ui.button(label="Dare", emoji="🔥", style=discord.ButtonStyle.danger)
    async def dare(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._send(interaction, "dare", logic.DARES)

    @discord.ui.button(label="New card", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def new_card(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=self.card("start"), view=TruthOrDareView())


class ScrambleAnswer(discord.ui.Modal, title="Unscramble the word"):
    answer = discord.ui.TextInput(label="Your answer", max_length=30, required=True)

    def __init__(self, parent: "ScrambleView"):
        super().__init__()
        self.parent = parent

    async def on_submit(self, interaction: discord.Interaction):
        await self.parent.check(interaction, str(self.answer))


class ScrambleView(discord.ui.View):
    def __init__(self, bot: commands.Bot, word: str, scrambled: str):
        super().__init__(timeout=180)
        self.bot = bot
        self.word = word
        self.scrambled = scrambled
        self.solved = False
        self.message: discord.Message | None = None

    def card(self) -> discord.Embed:
        return discord.Embed(title="🔤 Word Scramble",
                             description=f"Unscramble: **{self.scrambled.upper()}**\\n"
                                         f"First correct answer wins **{SCRAMBLE_POINTS}** points.",
                             colour=discord.Colour.blurple())

    @discord.ui.button(label="Answer", emoji="✍️", style=discord.ButtonStyle.success)
    async def answer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.solved:
            await interaction.response.send_message("Already solved! Try `/wordscramble` again. 🔤",
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(ScrambleAnswer(self))

    async def check(self, interaction: discord.Interaction, guess: str):
        if self.solved:
            await interaction.response.send_message(f"Too slow. It was **{self.word}**.", ephemeral=True)
            return
        if not logic.answer_matches(guess, self.word):
            await interaction.response.send_message("❌ Not quite. Try again!", ephemeral=True)
            return
        self.solved = True
        self.bot.db.add_points(interaction.guild.id, interaction.user.id, SCRAMBLE_POINTS)
        for item in self.children:
            item.disabled = True
        self.stop()
        embed = discord.Embed(title="🔤 Word Scramble: solved!",
                              description=f"{interaction.user.mention} got it! The word was **{self.word}**. "
                                          f"+{SCRAMBLE_POINTS} points.",
                              colour=discord.Colour.green())
        await interaction.response.send_message(embed=embed)
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Arcade(bot))
''',
    'cog_games': '''"""420 games and fun commands. Everything here is for fun, not drug instructions."""
import random
from datetime import datetime

import discord
from discord import app_commands
from discord.ext import commands

import config
import logic

FOOTER = "🌿 Keep it legal and keep it chill."


def now_local() -> datetime:
    return datetime.now(config.TZ)


class TriviaView(discord.ui.View):
    """One-off trivia question. Each person can answer once. Correct answers earn points."""

    def __init__(self, bot: commands.Bot, question: dict):
        super().__init__(timeout=60)
        self.bot = bot
        self.question = question
        self.answered: set[int] = set()
        self.message = None
        for index, choice in enumerate(question["choices"]):
            button = discord.ui.Button(label=choice[:80], style=discord.ButtonStyle.secondary,
                                       custom_id=f"trivia:{index}")
            button.callback = self._make_callback(index)
            self.add_item(button)

    def _make_callback(self, index: int):
        async def callback(interaction: discord.Interaction):
            user = interaction.user
            if user.id in self.answered:
                await interaction.response.send_message("You already answered this one.", ephemeral=True)
                return
            self.answered.add(user.id)
            if logic.is_correct(self.question, index):
                self.bot.db.add_points(interaction.guild.id, user.id, config.TRIVIA_POINTS, trivia_win=True)
                text = f"✅ Correct! +{config.TRIVIA_POINTS} points.\\n{self.question['fact']}"
            else:
                correct = self.question["choices"][self.question["answer"]]
                text = f"❌ Not quite. The answer was **{correct}**.\\n{self.question['fact']}"
            await interaction.response.send_message(text, ephemeral=True)
        return callback

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            correct = self.question["choices"][self.question["answer"]]
            try:
                await self.message.edit(content=f"⏰ Time's up! The answer was **{correct}**.", view=self)
            except discord.HTTPException:
                pass


class Games(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.rng = random.Random()

    @app_commands.command(name="countdown420", description="How long until the next 4:20 (Central time).")
    @app_commands.guild_only()
    async def countdown(self, interaction: discord.Interaction):
        now = now_local()
        if logic.in_checkin_window(now):
            text = "🎉 **It's 4:20 right now!** Use `/checkin` for your bonus."
        else:
            target = logic.next_420(now)
            remaining = (target - now).total_seconds()
            label = "4:20 AM" if target.hour == 4 else "4:20 PM"
            text = f"⏳ **{logic.format_countdown(remaining)}** until the next **{label}** (Central)."
        if logic.is_420_day(now):
            text += "\\n🎊 **Happy 4/20!**"
        embed = discord.Embed(title="🕓 4:20 Countdown", description=text,
                              colour=discord.Colour.from_str("#2ECC71"))
        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="checkin", description="Daily 4:20 check-in for bonus points.")
    @app_commands.guild_only()
    async def checkin(self, interaction: discord.Interaction):
        now = now_local()
        if not logic.in_checkin_window(now):
            await interaction.response.send_message(
                f"🕓 Check-in opens {config.CHECKIN_MINUTES} minutes before and after 4:20 AM or PM "
                "(Central). Come back then!", ephemeral=True)
            return
        if not self.bot.db.claim_checkin(interaction.guild.id, interaction.user.id, now.date().isoformat()):
            await interaction.response.send_message("You already checked in today. 🌿", ephemeral=True)
            return
        self.bot.db.add_points(interaction.guild.id, interaction.user.id, config.CHECKIN_POINTS)
        await interaction.response.send_message(
            f"🎉 Checked in! +{config.CHECKIN_POINTS} points. Nice timing.", ephemeral=True)

    @app_commands.command(name="trivia", description="Cannabis culture and science trivia. Win points!")
    @app_commands.guild_only()
    async def trivia(self, interaction: discord.Interaction):
        question = logic.pick_trivia(self.rng)
        view = TriviaView(self.bot, question)
        await interaction.response.send_message(
            f"🧠 **Trivia:** {question['q']}\\nYou have 60 seconds. One answer each.",
            view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="leaderboard", description="Top players in this server.")
    @app_commands.guild_only()
    async def leaderboard(self, interaction: discord.Interaction):
        rows = self.bot.db.leaderboard(interaction.guild.id, 10)
        if not rows:
            await interaction.response.send_message("No points yet. Try `/trivia` or `/checkin`!")
            return
        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, row in enumerate(rows):
            badge = medals[i] if i < 3 else f"`{i + 1}.`"
            lines.append(f"{badge} <@{row['user_id']}> · **{row['points']}** pts · "
                         f"{row['trivia_wins']} trivia wins")
        embed = discord.Embed(title="🏆 Stoner Hideout Leaderboard", description="\\n".join(lines),
                              colour=discord.Colour.gold())
        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="munchies", description="Random snack pick for when the munchies hit.")
    @app_commands.guild_only()
    async def munchies(self, interaction: discord.Interaction):
        snack, quip = self.rng.choice(logic.MUNCHIES)
        embed = discord.Embed(title="🍕 Munchies Pick", description=f"**{snack}**\\n{quip}",
                              colour=discord.Colour.orange())
        embed.set_footer(text=FOOTER)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="strain", description="Generate a fictional strain name (just for fun).")
    @app_commands.guild_only()
    async def strain(self, interaction: discord.Interaction):
        name = logic.strain_name(self.rng)
        embed = discord.Embed(title="🧪 Fictional Strain Name", description=f"**{name}**",
                              colour=discord.Colour.green())
        embed.set_footer(text="Made-up name. Not a real strain or product. " + FOOTER)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="wouldyourather", description="Would you rather? (420 edition)")
    @app_commands.guild_only()
    async def wouldyourather(self, interaction: discord.Interaction):
        a, b = self.rng.choice(logic.WOULD_YOU_RATHER)
        await interaction.response.send_message(f"🤔 **Would you rather** {a}, **or** {b}?")

    @app_commands.command(name="vibecheck", description="Your vibe score for today.")
    @app_commands.guild_only()
    async def vibecheck(self, interaction: discord.Interaction):
        seed = f"{interaction.user.id}:{now_local().date().isoformat()}"
        score, line = logic.vibe_score(seed)
        await interaction.response.send_message(
            f"✨ {interaction.user.mention}, your vibe today is **{score}/100**.\\n{line}")


async def setup(bot: commands.Bot):
    await bot.add_cog(Games(bot))
''',
    'cog_moderation': '''"""Staff tools: warn, view warnings, timeout, purge. Every action is logged to #mod-logs."""
from datetime import timedelta
import time

import discord
from discord import app_commands
from discord.ext import commands

from cog_security import set_lockdown
from utils import is_staff, log_to_mods

MAX_TIMEOUT_MINUTES = 40320  # Discord's limit: 28 days


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="warn", description="Warn a member (staff).")
    @app_commands.describe(member="Who to warn", reason="Why")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if member.bot or member == interaction.user:
            await interaction.response.send_message("Pick a real member (not a bot, not yourself).", ephemeral=True)
            return
        if is_staff(member):
            await interaction.response.send_message("Staff can't be warned with this command.", ephemeral=True)
            return
        reason = reason[:400]
        warning_id = self.bot.db.add_warning(interaction.guild.id, member.id, interaction.user.id,
                                             reason, int(time.time()))
        await interaction.response.send_message(
            f"⚠️ Warned {member.mention} (warning #{warning_id}).", allowed_mentions=discord.AllowedMentions.none())
        try:
            await member.send(f"⚠️ You got a warning in **{interaction.guild.name}**: {reason}")
        except discord.HTTPException:
            pass
        await log_to_mods(interaction.guild,
                          f"⚠️ {member.mention} warned by {interaction.user.mention} (#{warning_id}): {reason}")

    @app_commands.command(name="warnings", description="See a member's recent warnings (staff).")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        rows = self.bot.db.list_warnings(interaction.guild.id, member.id)
        if not rows:
            await interaction.response.send_message(f"{member.display_name} has no warnings.", ephemeral=True)
            return
        lines = [f"`#{r['id']}` <t:{r['created_at']}:d> by <@{r['mod_id']}>: {r['reason']}" for r in rows]
        embed = discord.Embed(title=f"Warnings for {member.display_name}", description="\\n".join(lines),
                              colour=discord.Colour.orange())
        await interaction.response.send_message(embed=embed, ephemeral=True,
                                                allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="timeout", description="Timeout a member for a set number of minutes (staff).")
    @app_commands.describe(member="Who", minutes="1 to 40320", reason="Why")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def timeout(self, interaction: discord.Interaction, member: discord.Member,
                      minutes: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES], reason: str = "No reason given"):
        if member.bot or is_staff(member) or member == interaction.user:
            await interaction.response.send_message("I can't timeout that account.", ephemeral=True)
            return
        try:
            await member.timeout(timedelta(minutes=minutes), reason=f"{interaction.user}: {reason}"[:500])
        except discord.HTTPException as exc:
            await interaction.response.send_message(f"Discord said no: {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            f"⏳ {member.mention} timed out for {minutes} min.", allowed_mentions=discord.AllowedMentions.none())
        await log_to_mods(interaction.guild,
                          f"⏳ {member.mention} timed out {minutes} min by {interaction.user.mention}: {reason[:200]}")

    @app_commands.command(name="purge", description="Delete the last messages in this channel (staff).")
    @app_commands.describe(amount="How many messages (1 to 100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def purge(self, interaction: discord.Interaction, amount: app_commands.Range[int, 1, 100]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"🧹 Deleted {len(deleted)} message(s).", ephemeral=True)
        await log_to_mods(interaction.guild,
                          f"🧹 {interaction.user.mention} purged {len(deleted)} message(s) in {interaction.channel.mention}")

    @app_commands.command(name="lockdown", description="Stop everyone from chatting in public channels (staff).")
    @app_commands.describe(state="Turn the lockdown on or off")
    @app_commands.choices(state=[app_commands.Choice(name="on", value="on"),
                                 app_commands.Choice(name="off", value="off")])
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def lockdown(self, interaction: discord.Interaction, state: app_commands.Choice[str]):
        await interaction.response.defer(ephemeral=True, thinking=True)
        locking = state.value == "on"
        count = await set_lockdown(self.bot, interaction.guild, locking, f"Lockdown {state.value} by {interaction.user}")
        word = "ON 🔒" if locking else "OFF 🔓"
        await interaction.followup.send(f"Lockdown {word}. {count} channel(s) changed.", ephemeral=True)
        await log_to_mods(interaction.guild, f"Lockdown {word} by {interaction.user.mention} ({count} channels).")

    @app_commands.command(name="slowmode", description="Set slowmode in this channel (staff). 0 turns it off.")
    @app_commands.describe(seconds="0 to 21600 seconds")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def slowmode(self, interaction: discord.Interaction, seconds: app_commands.Range[int, 0, 21600]):
        await interaction.channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {interaction.user}")
        text = "Slowmode off." if seconds == 0 else f"Slowmode set to {seconds}s."
        await interaction.response.send_message(f"🐢 {text}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
''',
    'cog_roles': '''"""Self-assign roles with a dropdown menu."""
import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import find_role, reuse_or_post


class RoleSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=name, value=name, emoji=emoji, description=desc)
            for name, emoji, desc, _colour in config.GAME_ROLES
        ]
        super().__init__(placeholder="Pick your vibe roles…", min_values=0,
                         max_values=len(options), options=options,
                         custom_id="stoner:roles:select")

    async def callback(self, interaction: discord.Interaction):
        guild, member = interaction.guild, interaction.user
        chosen = set(self.values)
        added, removed = [], []
        for name, emoji, _desc, _colour in config.GAME_ROLES:
            role = find_role(guild, name)
            if role is None:
                continue
            if name in chosen and role not in member.roles:
                await member.add_roles(role, reason="Self-assigned role")
                added.append(f"{emoji} {name}")
            elif name not in chosen and role in member.roles:
                await member.remove_roles(role, reason="Self-removed role")
                removed.append(f"{emoji} {name}")
        if not added and not removed:
            text = "No changes. Your roles already match."
        else:
            parts = []
            if added:
                parts.append("✅ Added: " + ", ".join(added))
            if removed:
                parts.append("➖ Removed: " + ", ".join(removed))
            text = "\\n".join(parts)
        await interaction.response.send_message(text, ephemeral=True)


class RolesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RoleSelect())


def roles_embed() -> discord.Embed:
    lines = "\\n".join(f"{emoji} **{name}**: {desc}" for name, emoji, desc, _c in config.GAME_ROLES)
    e = discord.Embed(
        title="🎭 Pick Your Vibe",
        description=("Use the dropdown below to pick your roles. Select as many as you want. "
                     "Unselect any to remove them.\\n\\n" + lines),
        colour=discord.Colour.from_str("#9B59B6"))
    e.set_footer(text="Roles are for fun and pings. Mods can change them anytime.")
    return e


class Roles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(RolesView())

    @app_commands.command(name="roles-panel", description="Post the vibe-role dropdown here (staff).")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def roles_panel(self, interaction: discord.Interaction):
        await interaction.response.send_message("Posting the roles panel…", ephemeral=True)
        status = await reuse_or_post(self.bot, interaction.channel, "panel:roles",
                                     interaction.guild.id, roles_embed(), RolesView())
        await interaction.followup.send(f"Roles panel: {status}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Roles(bot))
''',
    'cog_security': '''"""Security: spam, mass mentions, invite links, new accounts, raid alerts, and lockdown."""
import json
import time
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

import config
import logic
from utils import find_role, is_staff, log_to_mods

NEW_ACCOUNT_DAYS = 7
LOCK_KEY = "lockdown_channels"  # config table: comma-separated channel IDs we locked


async def set_lockdown(bot: commands.Bot, guild: discord.Guild, locked: bool, reason: str) -> int:
    """Stop everyone from typing in public channels (or undo it). Returns how many channels changed.

    Only channels that @everyone can see are touched, so mod-logs and tickets are never affected.
    When unlocking, only the channels we locked get restored.
    """
    everyone = guild.default_role
    db = bot.db
    changed = 0
    if locked:
        locked_ids = []
        for channel in guild.text_channels:
            ow = channel.overwrites_for(everyone)
            if ow.view_channel is False or ow.send_messages is False:
                continue
            try:
                ow.send_messages = False
                await channel.set_permissions(everyone, overwrite=ow, reason=reason)
                locked_ids.append(channel.id)
                changed += 1
            except discord.HTTPException:
                pass
        db.set_config(guild.id, LOCK_KEY, json.dumps(locked_ids))
    else:
        raw = db.get_config(guild.id, LOCK_KEY)
        for channel_id in json.loads(raw) if raw else []:
            channel = guild.get_channel(channel_id)
            if channel is None:
                continue
            ow = channel.overwrites_for(everyone)
            ow.send_messages = None
            try:
                await channel.set_permissions(everyone, overwrite=ow, reason=reason)
                changed += 1
            except discord.HTTPException:
                pass
        db.set_config(guild.id, LOCK_KEY, "[]")
    return changed


class RaidView(discord.ui.View):
    """Attached to raid alerts. Staff can lock the server with one tap."""

    def __init__(self, bot: commands.Bot, guild: discord.Guild):
        super().__init__(timeout=3600)
        self.bot = bot
        self.guild = guild

    @discord.ui.button(label="Lock server", emoji="🔒", style=discord.ButtonStyle.danger)
    async def lock(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not is_staff(interaction.user):
            await interaction.response.send_message("Staff only. 🛡️", ephemeral=True)
            return
        count = await set_lockdown(self.bot, self.guild, True, f"Raid lockdown by {interaction.user}")
        button.disabled = True
        button.label = "Locked"
        await interaction.response.edit_message(view=self)
        await log_to_mods(self.guild, f"🔒 Lockdown ON by {interaction.user} ({count} channels).")


class Security(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.spam = logic.SpamTracker(config.SPAM_WINDOW_SECONDS, config.SPAM_MAX_MESSAGES)
        self.joins = logic.JoinTracker(config.RAID_WINDOW_SECONDS, config.RAID_JOIN_LIMIT)
        self._last_raid_alert: dict[int, float] = {}

    async def _punish(self, message: discord.Message, reason: str) -> None:
        member = message.author
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        try:
            await member.timeout(timedelta(minutes=config.AUTO_TIMEOUT_MINUTES), reason=f"Auto-mod: {reason}")
            action = f"timed out {config.AUTO_TIMEOUT_MINUTES} min"
        except discord.HTTPException:
            action = "timeout failed (check role order)"
        await log_to_mods(message.guild,
                          f"🛡️ **Auto-mod:** {member} (`{member.id}`) · {reason} · {action} · "
                          f"in #{message.channel.name}")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        member = message.author
        if not isinstance(member, discord.Member) or is_staff(member):
            return  # staff are exempt

        if logic.mass_mention(len(message.mentions), len(message.role_mentions)):
            self.spam.reset(member.id)
            await self._punish(message, "mass mention")
            return
        if logic.has_invite_link(message.content):
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            await message.channel.send(f"{member.mention} invite links aren't allowed here.", delete_after=10)
            await log_to_mods(message.guild, f"🔗 Invite link removed from {member} (`{member.id}`) "
                                             f"in #{message.channel.name}")
            return
        if self.spam.hit(member.id, time.time()):
            self.spam.reset(member.id)
            await self._punish(message, "message spam")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        guild = member.guild
        now_utc = datetime.now(timezone.utc)
        age = logic.account_age_days(member.created_at, now_utc)
        if age < NEW_ACCOUNT_DAYS:
            await log_to_mods(guild, f"🆕 New account joined: {member} (`{member.id}`), "
                                     f"{age:.1f} days old.")

        now = time.time()
        if not self.joins.hit(guild.id, now):
            return
        last = self._last_raid_alert.get(guild.id, 0)
        if now - last < config.RAID_ALERT_COOLDOWN:
            return
        self._last_raid_alert[guild.id] = now
        mod = find_role(guild, config.MOD_ROLE)
        ping = mod.mention if mod else "@Mod Team"
        await log_to_mods(
            guild,
            f"🚨 **Possible raid:** {config.RAID_JOIN_LIMIT}+ joins in {config.RAID_WINDOW_SECONDS}s. "
            f"{ping} check the member list, or tap 🔒 to lock the server.",
            ping_roles=True, view=RaidView(self.bot, guild))


async def setup(bot: commands.Bot):
    await bot.add_cog(Security(bot))
''',
    'cog_setup_server': '''"""/setup-stoner: connects the bot to the channels and roles already in the server, then posts the panels.

Default (create_missing = False): it never creates anything. It only uses what exists and reports what's missing.
create_missing = True: also creates missing roles and channels. Use that only on a brand-new server.
Safe to run again."""
import discord
from discord import app_commands
from discord.ext import commands

import config
from cog_roles import RolesView, roles_embed
from cog_tickets import TicketPanel, support_embed
from cog_verify import VerifyView, verify_embed
from utils import find_category, find_role, find_text, reuse_or_post


def _first_name(name) -> str:
    return name if isinstance(name, str) else name[0]


async def ensure_role(guild: discord.Guild, name, colour: str, create: bool, report: list) -> None:
    if find_role(guild, name):
        return
    if not create:
        report.append(f"⚠️ role missing: {_first_name(name)}")
        return
    await guild.create_role(name=_first_name(name), colour=discord.Colour.from_str(colour),
                            hoist=False, mentionable=False, reason="Stoner setup: role")
    report.append(f"➕ role {_first_name(name)}")


class SetupServer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="setup-stoner",
                          description="Connect the bot to this server's channels and post the panels (admins).")
    @app_commands.describe(create_missing="Also create missing roles/channels. Leave off for an existing server.")
    @app_commands.default_permissions(administrator=True)
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def setup_stoner(self, interaction: discord.Interaction, create_missing: bool = False):
        await interaction.response.defer(ephemeral=True, thinking=True)
        guild = interaction.guild
        report: list[str] = []
        try:
            # 1. Verified and vibe roles
            await ensure_role(guild, config.VERIFIED_ROLE, "#5DADE2", create_missing, report)
            for name, emoji, _desc, colour in config.GAME_ROLES:
                await ensure_role(guild, name, colour, create_missing, report)

            # 2. Channels (existing ones are used as-is)
            if not find_role(guild, config.MOD_ROLE):
                report.append("⚠️ no staff role found (Mod Team / staff team). Raid pings won't work.")
            channels = {}
            for slug in ("welcome", "verify", "roles", "general", "mod_logs", "support"):
                channel = find_text(guild, config.CHANNELS[slug])
                if channel is None and create_missing:
                    channel = await self._create(guild, slug)
                    report.append(f"➕ #{channel.name}")
                if channel is None:
                    report.append(f"⚠️ channel missing: {config.CHANNELS[slug][0]}")
                channels[slug] = channel

            # 3. Panels (posted once; replaced if another bot posted them)
            if channels["verify"]:
                status = await reuse_or_post(self.bot, channels["verify"], "panel:verify", guild.id,
                                             verify_embed(channels["welcome"].mention if channels["welcome"]
                                                          else "#welcome"), VerifyView())
                report.append(f"verify panel: {status}")
            if channels["roles"]:
                status = await reuse_or_post(self.bot, channels["roles"], "panel:roles", guild.id,
                                             roles_embed(), RolesView())
                report.append(f"roles panel: {status}")
            if channels["support"]:
                status = await reuse_or_post(self.bot, channels["support"], "panel:support", guild.id,
                                             support_embed(), TicketPanel())
                report.append(f"ticket panel: {status}")

            text = "✅ **Stoner Hideout is connected.**\\n" + ("\\n".join(report) if report else "All set.")
            await interaction.followup.send(text, ephemeral=True)
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"❌ Setup stopped: {exc}\\nCheck the bot's role is above the roles it manages, "
                "then run /setup-stoner again. It's safe to re-run.", ephemeral=True)

    async def _create(self, guild: discord.Guild, slug: str) -> discord.TextChannel:
        category = find_category(guild, config.CATEGORY_MAIN)
        if category is None:
            category = await guild.create_category(config.CATEGORY_MAIN, reason="Stoner setup")
        everyone = guild.default_role
        overwrites = {}
        if slug == "mod_logs":
            overwrites[everyone] = discord.PermissionOverwrite(view_channel=False)
            mod = find_role(guild, config.MOD_ROLE)
            if mod:
                overwrites[mod] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        return await guild.create_text_channel(_first_name(config.CHANNELS[slug]), category=category,
                                               overwrites=overwrites, reason="Stoner setup: channel")


async def setup(bot: commands.Bot):
    await bot.add_cog(SetupServer(bot))
''',
    'cog_tickets': '''"""Support tickets: a button opens a private channel with the Mod Team. One open ticket per person."""
import re

import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import find_category, find_role, log_to_mods, reuse_or_post


async def ensure_ticket_category(guild: discord.Guild) -> discord.CategoryChannel:
    cat = find_category(guild, config.CATEGORY_TICKETS)
    if cat is None:
        ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False)}
        mod = find_role(guild, config.MOD_ROLE)
        if mod:
            ow[mod] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
        cat = await guild.create_category(config.CATEGORY_TICKETS, overwrites=ow,
                                          reason="Stoner bot: ticket category")
    return cat


class TicketControls(discord.ui.View):
    """Sits inside each ticket channel."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Close ticket", emoji="🔒", style=discord.ButtonStyle.danger,
                       custom_id="stoner:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        await interaction.response.send_message("🔒 Closing in 5 seconds…")
        await log_to_mods(interaction.guild, f"🎫 **{channel.name}** closed by {interaction.user.mention}")
        import asyncio
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException:
            pass


class TicketPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Open a ticket", emoji="🎫", style=discord.ButtonStyle.primary,
                       custom_id="stoner:ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild, user = interaction.guild, interaction.user
        cat = await ensure_ticket_category(guild)
        for ch in cat.text_channels:
            if ch.topic and f"owner:{user.id}" in ch.topic:
                await interaction.response.send_message(
                    f"You already have a ticket open: {ch.mention}", ephemeral=True)
                return

        await interaction.response.defer(ephemeral=True, thinking=True)
        P = discord.PermissionOverwrite
        ow = {
            guild.default_role: P(view_channel=False),
            user: P(view_channel=True, send_messages=True, read_message_history=True,
                    attach_files=True, embed_links=True),
        }
        mod = find_role(guild, config.MOD_ROLE)
        if mod:
            ow[mod] = P(view_channel=True, send_messages=True, read_message_history=True,
                        manage_messages=True)
        slug = re.sub(r"[^a-z0-9]", "", user.name.lower())[:20] or str(user.id)
        channel = await guild.create_text_channel(
            f"ticket-{slug}", category=cat, overwrites=ow,
            topic=f"owner:{user.id} · support ticket", reason=f"Ticket opened by {user}")

        embed = discord.Embed(
            title="🎫 Ticket opened",
            description=(f"Hey {user.mention}! Tell us what you need and a mod will be with you soon.\\n\\n"
                         "Tap **Close ticket** when you're done."),
            colour=discord.Colour.from_str("#3498DB"))
        embed.set_footer(text="Only you and the Mod Team can see this channel.")
        await channel.send(content=user.mention, embed=embed, view=TicketControls())
        await interaction.followup.send(f"🎫 Your ticket is ready: {channel.mention}", ephemeral=True)
        await log_to_mods(guild, f"🎫 **{channel.name}** opened by {user.mention}")


def support_embed() -> discord.Embed:
    return discord.Embed(
        title="🎫 Need Help?",
        description=("Tap **Open a ticket** for a private channel with the Mod Team.\\n\\n"
                     "• One open ticket per person\\n"
                     "• Only you and the Mod Team can see it\\n"
                     "• Close it when you're done"),
        colour=discord.Colour.from_str("#3498DB"))


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(TicketPanel())
        self.bot.add_view(TicketControls())

    @app_commands.command(name="ticket-panel", description="Post the ticket button here (staff).")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def ticket_panel(self, interaction: discord.Interaction):
        await interaction.response.send_message("Posting the ticket panel…", ephemeral=True)
        status = await reuse_or_post(self.bot, interaction.channel, "panel:support",
                                     interaction.guild.id, support_embed(), TicketPanel())
        await interaction.followup.send(f"Ticket panel: {status}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Tickets(bot))
''',
    'cog_verify': '''"""Verification: a button that gives the Verified role after the member confirms their age."""
import discord
from discord import app_commands
from discord.ext import commands

import config
from utils import find_role, find_text, reuse_or_post


class VerifyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # persistent: keeps working after restarts

    @discord.ui.button(label="I'm 18+ and I agree", emoji="✅",
                       style=discord.ButtonStyle.success, custom_id="stoner:verify")
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = find_role(interaction.guild, config.VERIFIED_ROLE)
        if role is None:
            await interaction.response.send_message(
                "⚠️ The Verified role is missing. Ask a mod to run /setup-stoner.", ephemeral=True)
            return
        member = interaction.user
        if role in member.roles:
            await interaction.response.send_message("You're already verified. 🌿", ephemeral=True)
            return
        await member.add_roles(role, reason="Verified: member confirmed age and rules")
        await interaction.response.send_message(
            "✅ You're in. Welcome to **Stoner Hideout**! Grab your roles in the roles channel.",
            ephemeral=True)


def verify_embed(rules_hint: str) -> discord.Embed:
    e = discord.Embed(
        title="✅ Verify to Get In",
        description=(
            f"Tap **I'm {config.MIN_AGE}+ and I agree** below to unlock the server.\\n\\n"
            "By verifying you confirm that:\\n"
            f"• You are **{config.MIN_AGE} or older**\\n"
            f"• You'll follow the rules in {rules_hint}\\n"
            "• You won't buy, sell, or trade anything in the server\\n\\n"
            "This is a self-confirmation. Discord's own age settings still apply."),
        colour=discord.Colour.from_str("#2ECC71"),
    )
    e.set_footer(text="Stuck? Open a ticket in the support channel.")
    return e


class Verify(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        self.bot.add_view(VerifyView())

    @app_commands.command(name="verify-panel",
                          description="Post the verification button in this channel (staff).")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def verify_panel(self, interaction: discord.Interaction):
        rules = find_text(interaction.guild, "rules")
        embed = verify_embed(rules.mention if rules else "#rules")
        await interaction.response.send_message("Posting the verify panel…", ephemeral=True)
        status = await reuse_or_post(self.bot, interaction.channel, "panel:verify",
                                     interaction.guild.id, embed, VerifyView())
        await interaction.followup.send(f"Verify panel: {status}.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Verify(bot))
''',
    'cog_welcome': '''"""Welcome new members in #welcome (falls back to #general) with quick-jump buttons."""
import random

import discord
from discord.ext import commands

import config
from utils import find_text

GREETINGS = [
    "Glad you're here",
    "Welcome to the hideout",
    "Pull up a seat",
    "The snacks are on the table",
    "Look who just walked in",
]


class Welcome(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot:
            return
        guild = member.guild
        channel = find_text(guild, config.CHANNELS["welcome"]) or find_text(guild, config.CHANNELS["general"])
        if channel is None:
            return

        verify = find_text(guild, config.CHANNELS["verify"])
        roles = find_text(guild, config.CHANNELS["roles"])
        general = find_text(guild, config.CHANNELS["general"])
        greeting = random.choice(GREETINGS)
        embed = discord.Embed(
            title=f"🌿 {greeting}, {member.display_name}!",
            description=(
                f"{member.mention} just joined. You're member **#{guild.member_count}**.\\n\\n"
                "**Start here**\\n"
                f"1️⃣ Tap **Verify** to unlock the server\\n"
                f"2️⃣ Pick your vibe roles in {roles.mention if roles else '#roles'}\\n"
                f"3️⃣ Say hi in {general.mention if general else '#general'}\\n"
                "4️⃣ Try `/help` to see the games and tools\\n\\n"
                "Keep it chill, keep it legal, and respect the hideout. 💨"),
            colour=discord.Colour.from_str("#2ECC71"),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        view = discord.ui.View(timeout=None)
        if verify:
            view.add_item(discord.ui.Button(label="Verify", emoji="✅", style=discord.ButtonStyle.link,
                                            url=verify.jump_url))
        if roles:
            view.add_item(discord.ui.Button(label="Pick roles", emoji="🎭", style=discord.ButtonStyle.link,
                                            url=roles.jump_url))
        if general:
            view.add_item(discord.ui.Button(label="Say hi", emoji="👋", style=discord.ButtonStyle.link,
                                            url=general.jump_url))
        try:
            await channel.send(content=member.mention, embed=embed, view=view)
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcome(bot))
''',
    'main': '''#!/usr/bin/env python3
"""Stoner Hideout bot — entry point.

Run:  STONER_TOKEN=... python3 main.py
The token is read from the environment only. Never write it into a file.
"""
import asyncio
import os
import sys
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config
from db import Database

EXTENSIONS = [
    "cog_setup_server",
    "cog_welcome",
    "cog_verify",
    "cog_tickets",
    "cog_roles",
    "cog_games",
    "cog_arcade",
    "cog_security",
    "cog_moderation",
]


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}Z] {msg}", flush=True)


async def start_health_server() -> None:
    """Tiny web page at / so free hosts (Koyeb, Render) see a healthy web service. Returns 'ok'."""
    from aiohttp import web

    async def ok(_request):
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/health", ok)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", "8000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log(f"Health page listening on port {port}")


class StonerBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        # Needed for welcome messages and raid detection. Turn on "Server Members Intent"
        # in the Developer Portal (Bot tab → Privileged Gateway Intents).
        intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned,  # slash commands only; this never fires
            intents=intents,
            # The bot can never mass-ping. Users are allowed, everyone/roles/replies are not.
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                                                     users=True, replied_user=False),
            help_command=None,
        )
        self.db = Database(config.DB_PATH)
        self.tree.on_error = self.on_app_command_error

    async def setup_hook(self):
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        log(f"Loaded {len(EXTENSIONS)} modules")
        await start_health_server()

    async def sync_guild(self, guild: discord.Guild) -> None:
        """Per-server command sync: commands appear right away (global sync can take an hour)."""
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        log(f"Synced {len(synced)} commands to {guild.name}")

    async def on_ready(self):
        log(f"Online as {self.user} in {len(self.guilds)} server(s)")
        for guild in self.guilds:
            try:
                await self.sync_guild(guild)
            except discord.HTTPException as exc:
                log(f"Command sync failed in {guild.name}: {exc}")

    async def on_interaction(self, interaction: discord.Interaction):
        """Log every slash command and button press, so we can see what reaches the bot."""
        if interaction.type == discord.InteractionType.application_command:
            log(f"slash: /{interaction.command.qualified_name if interaction.command else '?'} "
                f"by {interaction.user} in {interaction.guild.name if interaction.guild else 'DM'}")
        elif interaction.type == discord.InteractionType.component:
            log(f"button: {interaction.data.get('custom_id', '?')} by {interaction.user}")

    async def on_guild_join(self, guild: discord.Guild):
        log(f"Joined {guild.name}")
        await self.sync_guild(guild)

    async def on_app_command_error(self, interaction: discord.Interaction, error: Exception):
        log(f"command error: {type(error).__name__}: {error}")
        if isinstance(error, app_commands.CheckFailure):
            text = "🚫 You need staff permissions for that."
        else:
            text = "⚠️ Something went wrong. Try again in a moment."
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


async def main():
    token = os.environ.get("STONER_TOKEN", "").strip()
    if not token:
        sys.exit("❌ Set STONER_TOKEN in the environment first.")
    bot = StonerBot()
    async with bot:
        try:
            await bot.start(token)
        except discord.LoginFailure:
            sys.exit("❌ Discord rejected STONER_TOKEN. Reset it in the Developer Portal and try again.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
''',
}


class _BundleFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, fullname, path, target=None):
        if fullname in _SOURCES:
            return importlib.util.spec_from_loader(fullname, self)
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        name = module.__spec__.name
        module.__file__ = os.path.join(BASE_DIR, name + '.py')
        exec(compile(_SOURCES[name], module.__file__, 'exec'), module.__dict__)


sys.meta_path.insert(0, _BundleFinder())


if __name__ == "__main__":
    import asyncio
    import main as _main
    try:
        asyncio.run(_main.main())
    except KeyboardInterrupt:
        pass
