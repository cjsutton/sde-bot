#!/usr/bin/env python3
"""Bluesky bot v2.

Posts date-specific messages when due; otherwise posts from a shuffle bag of
regular messages. Designed for GitHub Actions with repo-backed JSON state.
"""

import argparse
import calendar
import hashlib
import json
import logging
import os
import random
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import regex as regex_module


EASTERN = ZoneInfo("America/New_York")
STATE_VERSION = 2
RECENT_FEED_CHECK = 20
MAX_GRAPHEMES = 300
MAX_BYTES = 3000

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def atomic_write_json(path: str, data) -> None:
    """Write JSON atomically so a killed process cannot leave a half-written file."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=str(destination.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, destination)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def stable_seed(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8")).hexdigest(), 16)


def pool_fingerprint(posts: list[str]) -> str:
    payload = "\n".join(sorted(posts))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def deterministic_shuffle(items: list[str], seed_text: str) -> None:
    random.Random(stable_seed(seed_text)).shuffle(items)


def grapheme_count(text: str) -> int:
    return len(regex_module.findall(r"\X", text))


def text_limit_error(text: str) -> str | None:
    graphemes = grapheme_count(text)
    byte_count = len(text.encode("utf-8"))
    if graphemes > MAX_GRAPHEMES:
        return f"{graphemes} graphemes (limit {MAX_GRAPHEMES})"
    if byte_count > MAX_BYTES:
        return f"{byte_count} UTF-8 bytes (limit {MAX_BYTES})"
    return None


def parse_month_day(value: str) -> tuple[int, int]:
    if not re.fullmatch(r"\d{2}-\d{2}", value or ""):
        raise ValueError("must use MM-DD")
    month, day = map(int, value.split("-"))
    # Leap year 2000 lets 02-29 validate.
    date(2000, month, day)
    return month, day


def scheduled_matches_today(post: dict, today: date) -> bool:
    month, day = parse_month_day(post["month_day"])
    if (today.month, today.day) == (month, day):
        return True

    fallback = post.get("non_leap_day")
    if not fallback:
        return False

    # A fallback is used only when the primary date does not exist this year.
    try:
        date(today.year, month, day)
        primary_exists = True
    except ValueError:
        primary_exists = False

    if primary_exists:
        return False

    fallback_month, fallback_day = parse_month_day(fallback)
    return (today.month, today.day) == (fallback_month, fallback_day)


class BlueskyBot:
    def __init__(
        self,
        handle: str,
        password: str,
        scheduled_posts_file: str = "scheduled_posts.json",
        regular_posts_file: str = "posts.txt",
        state_file: str = "bot_state.json",
    ):
        self.handle = handle
        self.password = password
        self.scheduled_posts_file = scheduled_posts_file
        self.regular_posts_file = regular_posts_file
        self.state_file = state_file
        self.client = None

    def authenticate(self) -> bool:
        try:
            from atproto import Client

            self.client = Client()
            self.client.login(self.handle, self.password)
            logger.info("Successfully authenticated as %s", self.handle)
            return True
        except Exception as exc:
            logger.error("Authentication failed: %s", exc)
            return False

    def load_scheduled_posts(self) -> list[dict]:
        try:
            with open(self.scheduled_posts_file, "r", encoding="utf-8") as handle:
                posts = json.load(handle)
            if not isinstance(posts, list):
                raise ValueError("scheduled_posts.json must contain a JSON array")
            return posts
        except FileNotFoundError:
            logger.info("No scheduled posts file found: %s", self.scheduled_posts_file)
            return []
        except Exception as exc:
            raise RuntimeError(f"Could not load scheduled posts: {exc}") from exc

    def save_scheduled_posts(self, posts: list[dict]) -> bool:
        try:
            atomic_write_json(self.scheduled_posts_file, posts)
            return True
        except Exception as exc:
            logger.error("Could not save scheduled posts: %s", exc)
            return False

    def load_regular_posts(self) -> list[str]:
        try:
            with open(self.regular_posts_file, "r", encoding="utf-8") as handle:
                posts = [line.strip() for line in handle if line.strip()]
            logger.info("Loaded %d regular posts", len(posts))
            return posts
        except FileNotFoundError:
            logger.error("No regular posts file found: %s", self.regular_posts_file)
            return []
        except Exception as exc:
            logger.error("Could not load regular posts: %s", exc)
            return []

    def load_state(self) -> dict:
        if not os.path.exists(self.state_file):
            return {}
        try:
            with open(self.state_file, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            return state if isinstance(state, dict) else {}
        except Exception as exc:
            logger.error("Could not load state: %s", exc)
            raise

    def save_state(self, state: dict) -> bool:
        try:
            atomic_write_json(self.state_file, state)
            logger.info(
                "Saved shuffle-bag state: cycle %s, %d posts remaining",
                state.get("cycle"),
                len(state.get("remaining_posts", [])),
            )
            return True
        except Exception as exc:
            logger.error("Could not save state: %s", exc)
            return False

    def sync_shuffle_bag(self, regular_posts: list[str]) -> dict:
        """Load/migrate state and reconcile it with the current posts.txt.

        V2 guarantees one use of every item in a cycle before the bag refills.
        New posts are inserted into the current bag automatically; deleted posts
        are removed automatically. Selection is deterministic from saved state,
        which makes retries safe after a post succeeded but a repo push failed.
        """
        state = self.load_state()
        current_set = set(regular_posts)

        if state.get("version") != STATE_VERSION:
            # Legacy migration: treat whatever the v1 history still remembers as
            # already used in the new cycle. Older v1 history cannot be recovered.
            recent_posts = state.get("recent_posts", [])
            recent_set = set(recent_posts) & current_set
            remaining = [post for post in regular_posts if post not in recent_set]
            cycle = 1
            deterministic_shuffle(
                remaining,
                f"v2-migration|{pool_fingerprint(regular_posts)}",
            )
            logger.info(
                "Migrating v1 state to v2: %d remembered posts treated as used; %d remaining",
                len(recent_set),
                len(remaining),
            )
            return {
                "version": STATE_VERSION,
                "cycle": cycle,
                "known_posts": list(regular_posts),
                "remaining_posts": remaining,
            }

        cycle = int(state.get("cycle", 1))
        known_posts = state.get("known_posts", [])
        remaining = state.get("remaining_posts", [])
        if not isinstance(known_posts, list) or not isinstance(remaining, list):
            raise ValueError("Invalid v2 bot_state.json structure")

        # Preserve saved bag order, while removing deleted entries and duplicates.
        cleaned_remaining = []
        seen = set()
        for post in remaining:
            if post in current_set and post not in seen:
                cleaned_remaining.append(post)
                seen.add(post)
        remaining = cleaned_remaining

        known_set = set(known_posts)
        new_posts = [post for post in regular_posts if post not in known_set]
        if new_posts:
            # Insert new material at deterministic pseudo-random positions so it
            # joins the current cycle immediately and retries choose the same item.
            for post in sorted(new_posts):
                index_seed = stable_seed(f"cycle:{cycle}|new:{post}")
                index = index_seed % (len(remaining) + 1)
                remaining.insert(index, post)
            logger.info("Added %d new posts to the current shuffle bag", len(new_posts))

        if not remaining:
            cycle += 1
            remaining = list(regular_posts)
            deterministic_shuffle(
                remaining,
                f"cycle:{cycle}|{pool_fingerprint(regular_posts)}",
            )
            logger.info("Started shuffle-bag cycle %d with %d posts", cycle, len(remaining))

        return {
            "version": STATE_VERSION,
            "cycle": cycle,
            "known_posts": list(regular_posts),
            "remaining_posts": remaining,
        }

    def find_scheduled_post_for_today(self) -> dict | None:
        scheduled_posts = self.load_scheduled_posts()
        today = datetime.now(EASTERN).date()

        for post in scheduled_posts:
            try:
                if not scheduled_matches_today(post, today):
                    continue
            except Exception as exc:
                logger.error("Invalid scheduled post %r: %s", post.get("id"), exc)
                continue

            if post.get("last_posted_year") == today.year:
                logger.info("Scheduled post %s was already sent this year", post.get("id"))
                continue

            logger.info("Found scheduled post for today: %s", post.get("id"))
            return post

        return None

    def mark_scheduled_post_as_sent(self, post_id: str) -> bool:
        try:
            scheduled_posts = self.load_scheduled_posts()
            now = datetime.now(EASTERN)
            found = False
            for post in scheduled_posts:
                if post.get("id") == post_id:
                    post["last_posted_year"] = now.year
                    post["posted_at"] = now.isoformat()
                    found = True
                    break

            if not found:
                logger.error("Could not find scheduled post id %s to mark as sent", post_id)
                return False

            if not self.save_scheduled_posts(scheduled_posts):
                return False
            logger.info("Marked scheduled post %s as sent for %d", post_id, now.year)
            return True
        except Exception as exc:
            logger.error("Could not mark scheduled post as sent: %s", exc)
            return False

    def was_recently_posted(self, text: str) -> bool | None:
        """Return True/False, or None if the safety check itself failed."""
        try:
            feed = self.client.get_author_feed(
                actor=self.handle,
                filter="posts_no_replies",
                limit=RECENT_FEED_CHECK,
            )
            for feed_view in feed.feed:
                if getattr(feed_view.post.record, "text", None) == text:
                    return True
            return False
        except Exception as exc:
            logger.error("Could not verify recent Bluesky posts: %s", exc)
            return None

    def send_with_duplicate_guard(self, text: str) -> str:
        """Return 'posted', 'already_posted', or 'failed'."""
        duplicate = self.was_recently_posted(text)
        if duplicate is None:
            # Fail closed. Better to miss a run and alert than risk a duplicate.
            return "failed"
        if duplicate:
            logger.warning(
                "Exact text already appears in the last %d Bluesky posts; treating this as a retry and not posting again",
                RECENT_FEED_CHECK,
            )
            return "already_posted"

        try:
            self.client.send_post(text=text)
            return "posted"
        except Exception as exc:
            logger.error("Error sending post: %s", exc)
            return "failed"

    def post_next(self) -> bool:
        scheduled_post = self.find_scheduled_post_for_today()

        if scheduled_post:
            post_text = scheduled_post.get("text", "")
            post_id = scheduled_post.get("id", "")
            if not post_text or not post_id:
                logger.error("Scheduled post is missing id or text")
                return False

            if not self.authenticate():
                return False

            result = self.send_with_duplicate_guard(post_text)
            if result == "failed":
                return False
            if result == "posted":
                logger.info("Posted SCHEDULED post %s: %s...", post_id, post_text[:100])

            # Also heal state after a prior send succeeded but the state commit/push failed.
            return self.mark_scheduled_post_as_sent(post_id)

        logger.info("No unposted scheduled post for today; using regular shuffle bag")
        regular_posts = self.load_regular_posts()
        if not regular_posts:
            return False

        try:
            state = self.sync_shuffle_bag(regular_posts)
        except Exception as exc:
            logger.error("Could not prepare shuffle bag: %s", exc)
            return False

        if not state["remaining_posts"]:
            logger.error("Shuffle bag unexpectedly empty")
            return False

        # Deterministic candidate: if state persistence/push fails after posting,
        # the next run selects this same item and the Bluesky guard heals the state.
        post_text = state["remaining_posts"][-1]

        if not self.authenticate():
            return False

        result = self.send_with_duplicate_guard(post_text)
        if result == "failed":
            return False
        if result == "posted":
            logger.info(
                "Posted RANDOM post from cycle %d (%d before removal): %s...",
                state["cycle"],
                len(state["remaining_posts"]),
                post_text[:100],
            )

        state["remaining_posts"].pop()
        return self.save_state(state)


def validate_files(
    regular_posts_file: str = "posts.txt",
    scheduled_posts_file: str = "scheduled_posts.json",
) -> bool:
    errors: list[str] = []
    warnings: list[str] = []

    try:
        with open(regular_posts_file, "r", encoding="utf-8") as handle:
            regular_posts = [line.strip() for line in handle if line.strip()]
    except Exception as exc:
        errors.append(f"Could not read {regular_posts_file}: {exc}")
        regular_posts = []

    duplicates = sorted({post for post in regular_posts if regular_posts.count(post) > 1})
    if duplicates:
        errors.append(f"{regular_posts_file} contains {len(duplicates)} exact duplicate post(s)")

    for index, text in enumerate(regular_posts, start=1):
        limit_error = text_limit_error(text)
        if limit_error:
            errors.append(f"{regular_posts_file} candidate #{index}: {limit_error}")

    try:
        with open(scheduled_posts_file, "r", encoding="utf-8") as handle:
            scheduled = json.load(handle)
        if not isinstance(scheduled, list):
            raise ValueError("top level must be a JSON array")
    except FileNotFoundError:
        scheduled = []
        warnings.append(f"{scheduled_posts_file} not found; scheduled posts are disabled")
    except Exception as exc:
        scheduled = []
        errors.append(f"Could not parse {scheduled_posts_file}: {exc}")

    ids: set[str] = set()
    due_days: dict[str, list[str]] = {}
    for index, post in enumerate(scheduled, start=1):
        label = f"{scheduled_posts_file} entry #{index}"
        if not isinstance(post, dict):
            errors.append(f"{label}: must be an object")
            continue

        post_id = post.get("id")
        if not isinstance(post_id, str) or not post_id.strip():
            errors.append(f"{label}: missing non-empty id")
        elif post_id in ids:
            errors.append(f"{label}: duplicate id {post_id!r}")
        else:
            ids.add(post_id)

        month_day = post.get("month_day")
        try:
            parse_month_day(month_day)
            due_days.setdefault(month_day, []).append(post_id or f"entry-{index}")
        except Exception as exc:
            errors.append(f"{label}: invalid month_day {month_day!r} ({exc})")

        fallback = post.get("non_leap_day")
        if fallback is not None:
            try:
                parse_month_day(fallback)
            except Exception as exc:
                errors.append(f"{label}: invalid non_leap_day {fallback!r} ({exc})")

        text = post.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{label}: missing non-empty text")
        else:
            limit_error = text_limit_error(text)
            if limit_error:
                errors.append(f"{label}: {limit_error}")

        year = post.get("last_posted_year")
        if year is not None and (not isinstance(year, int) or year < 2000 or year > 9999):
            errors.append(f"{label}: last_posted_year must be an integer year or null")

    for month_day, post_ids in sorted(due_days.items()):
        if len(post_ids) > 1:
            warnings.append(
                f"{month_day} has {len(post_ids)} scheduled posts; separate daily runs will send them one at a time"
            )

    if warnings:
        for warning in warnings:
            logger.warning("VALIDATION: %s", warning)

    if errors:
        for error in errors:
            logger.error("VALIDATION: %s", error)
        logger.error("Validation failed with %d error(s)", len(errors))
        return False

    logger.info(
        "Validation passed: %d regular posts, %d scheduled posts, no exact regular duplicates",
        len(regular_posts),
        len(scheduled),
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Bluesky scheduled/random posting bot")
    parser.add_argument(
        "--validate",
        action="store_true",
        help="Validate posts.txt and scheduled_posts.json without posting",
    )
    args = parser.parse_args()

    if args.validate:
        return 0 if validate_files() else 1

    handle = os.getenv("BLUESKY_HANDLE")
    password = os.getenv("BLUESKY_PASSWORD")
    if not handle or not password:
        logger.error("Missing BLUESKY_HANDLE or BLUESKY_PASSWORD environment variable")
        return 1

    if not validate_files():
        return 1

    bot = BlueskyBot(handle, password)
    return 0 if bot.post_next() else 1


if __name__ == "__main__":
    sys.exit(main())
