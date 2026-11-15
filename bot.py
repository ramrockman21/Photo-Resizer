import os
import sqlite3
from io import BytesIO
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont, ImageOps

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

TIMEZONE_NAME = "Asia/Kolkata"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_IMAGE_DIMENSION = 10000

# Free users can complete this many image operations per India calendar day.
FREE_DAILY_OPERATIONS = 3

# Telegram Stars subscription settings.
# This is an example price. Change it before launching.
PREMIUM_PRICE_STARS = 100
PREMIUM_PERIOD_SECONDS = 2592000  # Telegram currently requires exactly 30 days.

DB_PATH = BASE_DIR / "bot_data.sqlite3"
PAYMENT_PAYLOAD = "photo_resizer_premium_30_days"


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    return sqlite3.connect(DB_PATH)


def init_database():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                expires_at TEXT NOT NULL,
                telegram_charge_id TEXT NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_usage (
                user_id INTEGER NOT NULL,
                usage_date TEXT NOT NULL,
                operations INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, usage_date)
            )
        """)
        conn.commit()


def india_today():
    return datetime.now(ZoneInfo(TIMEZONE_NAME)).date().isoformat()


def coerce_datetime(value):
    if value is None:
        return None

    if isinstance(value, datetime):
        expiry = value
    elif isinstance(value, (int, float)):
        expiry = datetime.fromtimestamp(value, tz=timezone.utc)
    elif isinstance(value, str):
        try:
            expiry = datetime.fromisoformat(value)
        except ValueError:
            try:
                expiry = datetime.fromtimestamp(float(value), tz=timezone.utc)
            except (TypeError, ValueError):
                return None
    else:
        return None

    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry.astimezone(timezone.utc)


def get_subscription_expiry(user_id):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    if not row:
        return None
    return coerce_datetime(row[0])


def is_premium(user_id):
    expiry = get_subscription_expiry(user_id)
    return bool(expiry and expiry > datetime.now(timezone.utc))


def activate_subscription(user_id, expiry, charge_id):
    expiry = coerce_datetime(expiry)
    if expiry is None:
        raise ValueError("Subscription expiry must be a datetime or unix timestamp.")

    with db_connect() as conn:
        conn.execute("""
            INSERT INTO subscriptions (user_id, expires_at, telegram_charge_id)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                expires_at = excluded.expires_at,
                telegram_charge_id = excluded.telegram_charge_id
        """, (user_id, expiry.isoformat(), charge_id))
        conn.commit()


def get_daily_usage(user_id):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT operations FROM daily_usage WHERE user_id = ? AND usage_date = ?",
            (user_id, india_today()),
        ).fetchone()
    return row[0] if row else 0


def can_perform_operation(user_id):
    if is_premium(user_id):
        return True, None

    used = get_daily_usage(user_id)
    if used >= FREE_DAILY_OPERATIONS:
        return False, used

    return True, used


def record_operation(user_id):
    with db_connect() as conn:
        conn.execute("""
            INSERT INTO daily_usage (user_id, usage_date, operations)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, usage_date)
            DO UPDATE SET operations = operations + 1
        """, (user_id, india_today()))
        conn.commit()


# ============================================================
# IMAGE UTILITIES
# ============================================================

def open_image(image_bytes):
    image = Image.open(BytesIO(image_bytes))
    image = ImageOps.exif_transpose(image)
    image.load()

    if image.width > MAX_IMAGE_DIMENSION or image.height > MAX_IMAGE_DIMENSION:
        raise ValueError("Image dimensions are too large.")

    return image


def to_rgb(image):
    if image.mode in ("RGBA", "LA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        return background.convert("RGB")
    return image.convert("RGB")


def get_font(draw, text, max_size, max_width):
    for size in range(max_size, 8, -1):
        try:
            font = ImageFont.truetype("arialbd.ttf", size)
        except OSError:
            try:
                font = ImageFont.truetype("DejaVuSans-Bold.ttf", size)
            except OSError:
                return ImageFont.load_default()

        bbox = draw.textbbox((0, 0), text, font=font)
        if bbox[2] - bbox[0] <= max_width:
            return font

    return ImageFont.load_default()


def add_name_and_date(image_bytes, name="", date_text=""):
    image = to_rgb(open_image(image_bytes))
    name = (name or "").strip().upper()
    date_text = (date_text or "").strip()

    if not name and not date_text:
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    width, height = image.size
    border = max(2, width // 300)
    strip_height = max(70, width // 4)

    canvas = Image.new("RGB", (width, height + strip_height), "white")
    canvas.paste(image, (0, 0))
    draw = ImageDraw.Draw(canvas)

    lines = []
    if name:
        lines.append((
            name,
            get_font(draw, name, max(12, width // 15), width - 4 * border),
        ))
    if date_text:
        lines.append((
            date_text,
            get_font(draw, date_text, max(12, width // 17), width - 4 * border),
        ))

    line_heights = []
    for text, font in lines:
        bbox = draw.textbbox((0, 0), text, font=font)
        line_heights.append(bbox[3] - bbox[1])

    spacing = max(4, width // 100)
    total_height = sum(line_heights) + (spacing if len(lines) > 1 else 0)
    y = height + max(0, (strip_height - total_height) // 2)

    for text, font in lines:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (width - text_width) // 2
        draw.text((x, y - bbox[1]), text, fill="black", font=font)
        y += text_height + spacing

    draw.rectangle(
        (0, 0, width - 1, height + strip_height - 1),
        outline="black",
        width=border,
    )

    output = BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


def compress_image(image_bytes, target_kb):
    """Return a JPEG no larger than target_kb where achievable.

    If required, dimensions are reduced gradually. Exact target size cannot
    be guaranteed for every image; the caller checks the actual result.
    """
    image = to_rgb(open_image(image_bytes))
    max_bytes = target_kb * 1024

    for _ in range(24):
        low, high = 10, 95
        best = None

        while low <= high:
            quality = (low + high) // 2
            output = BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            data = output.getvalue()

            if len(data) <= max_bytes:
                best = data
                low = quality + 1
            else:
                high = quality - 1

        if best is not None:
            return best

        new_width = int(image.width * 0.85)
        new_height = int(image.height * 0.85)
        if new_width < 40 or new_height < 40:
            break

        image = image.resize(
            (new_width, new_height),
            Image.Resampling.LANCZOS,
        )

    output = BytesIO()
    image.save(output, format="JPEG", quality=10, optimize=True)
    return output.getvalue()


def convert_image(image_bytes, output_format):
    image = open_image(image_bytes)
    output = BytesIO()

    if output_format == "JPG":
        to_rgb(image).save(output, format="JPEG", quality=95, optimize=True)
        filename = "processed_photo.jpg"
    elif output_format == "PNG":
        image.save(output, format="PNG", optimize=True)
        filename = "processed_photo.png"
    elif output_format == "PDF":
        to_rgb(image).save(output, format="PDF", resolution=150)
        filename = "processed_photo.pdf"
    else:
        raise ValueError("Unsupported output format")

    return output.getvalue(), filename


# ============================================================
# TELEGRAM KEYBOARDS
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📐 Custom Dimensions", callback_data="resize")],
        [InlineKeyboardButton("🗜 Compress Image", callback_data="compress_menu")],
        [InlineKeyboardButton("✍️ Add / Edit Name & Date", callback_data="edit_both")],
        [InlineKeyboardButton("📅 Add / Edit Date Only", callback_data="edit_date")],
        [InlineKeyboardButton("⬇️ Download JPG / PNG / PDF", callback_data="download_menu")],
        [InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="premium")],
        [InlineKeyboardButton("📊 My Plan / Usage", callback_data="myplan")],
        [InlineKeyboardButton("🆕 Start with New Photo", callback_data="new_photo")],
    ])


def next_operation_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📐 Change Dimensions", callback_data="resize")],
        [InlineKeyboardButton("🗜 Change File Size", callback_data="compress_menu")],
        [InlineKeyboardButton("✍️ Add / Edit Name", callback_data="edit_name")],
        [InlineKeyboardButton("📅 Add / Edit Date", callback_data="edit_date")],
        [InlineKeyboardButton("✍️ Edit Name + Date", callback_data="edit_both")],
        [InlineKeyboardButton("⬇️ Download JPG / PNG / PDF", callback_data="download_menu")],
        [InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="premium")],
        [InlineKeyboardButton("📊 My Plan / Usage", callback_data="myplan")],
        [InlineKeyboardButton("🆕 Start with New Photo", callback_data="new_photo")],
    ])


def compression_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("10 KB", callback_data="compress_10"),
            InlineKeyboardButton("20 KB", callback_data="compress_20"),
        ],
        [
            InlineKeyboardButton("50 KB", callback_data="compress_50"),
            InlineKeyboardButton("100 KB", callback_data="compress_100"),
        ],
        [InlineKeyboardButton("↩ Other Operation", callback_data="other")],
    ])


def date_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Use Today's Date", callback_data="date_today")],
        [InlineKeyboardButton("Enter Date Manually", callback_data="date_manual")],
        [InlineKeyboardButton("Keep Existing Date", callback_data="date_keep")],
        [InlineKeyboardButton("Skip Date", callback_data="date_skip")],
    ])


def download_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Download JPG", callback_data="download_jpg")],
        [InlineKeyboardButton("Download PNG", callback_data="download_png")],
        [InlineKeyboardButton("Download PDF", callback_data="download_pdf")],
        [InlineKeyboardButton("↩ Other Operation", callback_data="other")],
    ])


# ============================================================
# COMMON HELPERS
# ============================================================

async def require_image(message, context):
    if not context.user_data.get("working_image"):
        await message.reply_text("Please upload a photo or signature first.")
        return False
    return True


async def require_free_or_premium(update, context):
    user_id = update.effective_user.id
    allowed, used = can_perform_operation(user_id)

    if allowed:
        return True

    await update.effective_message.reply_text(
        f"You have used your {FREE_DAILY_OPERATIONS} free operations today.\n\n"
        "Upgrade to Premium for a higher processing limit.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="premium")],
            [InlineKeyboardButton("📊 My Plan / Usage", callback_data="myplan")],
        ]),
    )
    return False


async def ask_next(message):
    await message.reply_text(
        "✅ Operation completed!\n\nWhat would you like to do next?",
        reply_markup=next_operation_keyboard(),
    )


async def show_plan(message, user_id):
    if is_premium(user_id):
        expiry = get_subscription_expiry(user_id)
        expiry_local = expiry.astimezone(ZoneInfo(TIMEZONE_NAME))
        await message.reply_text(
            "⭐ Your plan: PREMIUM\n"
            f"Subscription expires: {expiry_local.strftime('%d-%m-%Y %I:%M %p')}\n\n"
            "You have Premium processing access.",
            reply_markup=next_operation_keyboard(),
        )
    else:
        used = get_daily_usage(user_id)
        remaining = max(0, FREE_DAILY_OPERATIONS - used)
        await message.reply_text(
            "Your plan: FREE\n"
            f"Today's operations used: {used}/{FREE_DAILY_OPERATIONS}\n"
            f"Free operations remaining: {remaining}\n\n"
            "Upgrade to Premium for a higher processing limit.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⭐ Upgrade to Premium", callback_data="premium")],
                [InlineKeyboardButton("↩ Back to Operations", callback_data="other")],
            ]),
        )


# ============================================================
# PREMIUM PAYMENT
# ============================================================

async def send_premium_invoice(chat_id, context):
    await context.bot.send_invoice(
        chat_id=chat_id,
        title="Photo Resizer Premium",
        description="30-day Premium access for Photo & Signature Resizer.",
        payload=PAYMENT_PAYLOAD,
        provider_token="",  # Empty for Telegram Stars.
        currency="XTR",
        prices=[
            LabeledPrice(
                label="Photo Resizer Premium - 30 days",
                amount=PREMIUM_PRICE_STARS,
            )
        ],
        subscription_period=PREMIUM_PERIOD_SECONDS,
    )


async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query

    valid = (
        query.invoice_payload == PAYMENT_PAYLOAD
        and query.currency == "XTR"
        and query.total_amount == PREMIUM_PRICE_STARS
    )

    if valid:
        await query.answer(ok=True)
    else:
        await query.answer(
            ok=False,
            error_message="Payment details could not be verified. Please try again.",
        )


async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment

    if (
        payment.invoice_payload != PAYMENT_PAYLOAD
        or payment.currency != "XTR"
        or payment.total_amount != PREMIUM_PRICE_STARS
    ):
        await update.message.reply_text(
            "Payment received, but its details did not match this Premium plan. "
            "Please contact the bot administrator."
        )
        return

    # Telegram provides the expiry timestamp for recurring payments as either a
    # unix timestamp or a datetime-like object depending on the library version.
    expiry = coerce_datetime(payment.subscription_expiration_date)
    if expiry is None:
        expiry = datetime.now(timezone.utc) + timedelta(days=30)

    activate_subscription(
        user_id=update.effective_user.id,
        expiry=expiry,
        charge_id=payment.telegram_payment_charge_id,
    )

    expiry_local = expiry.astimezone(ZoneInfo(TIMEZONE_NAME))
    await update.message.reply_text(
        "🎉 Premium activated successfully!\n\n"
        f"Access expires: {expiry_local.strftime('%d-%m-%Y %I:%M %p')}\n"
        "Your Premium features are now unlocked.",
        reply_markup=next_operation_keyboard(),
    )


# ============================================================
# COMMANDS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "Welcome to Photo & Signature Resizer!\n\n"
        "Send a photo or an image file to begin.\n\n"
        "Free plan: 3 image operations per day.\n"
        "Premium: higher processing limits.",
        reply_markup=main_keyboard(),
    )


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"⭐ Photo Resizer Premium\n\n"
        f"Price: {PREMIUM_PRICE_STARS} Telegram Stars\n"
        "Duration: 30 days (recurring subscription)\n\n"
        "Premium gives you a higher image-processing limit.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⭐ Subscribe with Telegram Stars", callback_data="premium")],
            [InlineKeyboardButton("📊 My Plan", callback_data="myplan")],
        ]),
    )


async def myplan_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await show_plan(update.message, update.effective_user.id)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("input_step", None)
    context.user_data.pop("resize_width", None)
    context.user_data.pop("name_edit_mode", None)
    await update.message.reply_text(
        "Current input cancelled. Your uploaded image is still available.\n"
        "Choose another operation:",
        reply_markup=next_operation_keyboard(),
    )


# ============================================================
# RECEIVE PHOTO / IMAGE DOCUMENT
# ============================================================

async def receive_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        message = update.message

        if message.photo:
            telegram_file = await message.photo[-1].get_file()
        elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
            telegram_file = await message.document.get_file()
        else:
            await message.reply_text("Please send a JPG, PNG or other supported image file.")
            return

        if telegram_file.file_size and telegram_file.file_size > MAX_UPLOAD_BYTES:
            await message.reply_text("Maximum upload size is 15 MB.")
            return

        image_bytes = bytes(await telegram_file.download_as_bytearray())
        image = open_image(image_bytes)

        context.user_data.clear()
        context.user_data["working_image"] = image_bytes
        context.user_data["photo_name"] = ""
        context.user_data["photo_date"] = ""
        context.user_data["target_kb"] = None

        await message.reply_text(
            "✅ Image received!\n\n"
            f"Original dimensions: {image.width} × {image.height} pixels\n"
            f"File size: {len(image_bytes) / 1024:.2f} KB\n\n"
            "Choose an operation:",
            reply_markup=main_keyboard(),
        )

    except Exception as error:
        print("Image upload error:", repr(error))
        await update.message.reply_text(
            "I couldn't process this image. Please send a valid image file."
        )


# ============================================================
# BUTTON HANDLER
# ============================================================

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    user_id = update.effective_user.id
    message = query.message

    if action == "new_photo":
        context.user_data.clear()
        await message.reply_text("Send your next photo or signature.")
        return

    if action == "premium":
        try:
            await send_premium_invoice(message.chat_id, context)
        except Exception as error:
            print("Invoice error:", repr(error))
            await message.reply_text(
                "I couldn't create the Premium invoice. Please try again later."
            )
        return

    if action == "myplan":
        await show_plan(message, user_id)
        return

    if action == "other":
        if await require_image(message, context):
            await message.reply_text(
                "Choose another operation:",
                reply_markup=next_operation_keyboard(),
            )
        return

    if not await require_image(message, context):
        return

    # Downloading is free; transformations are subject to the daily limit.
    if action in ("resize", "edit_name", "edit_both", "edit_date", "compress_menu"):
        if not await require_free_or_premium(update, context):
            return

    if action == "resize":
        context.user_data["input_step"] = "width"
        await message.reply_text("Enter the required WIDTH in pixels. Example: 200")
        return

    if action == "compress_menu":
        await message.reply_text(
            "Choose the maximum target JPG size. Exact size depends on the image.",
            reply_markup=compression_keyboard(),
        )
        return

    if action.startswith("compress_"):
        if not await require_free_or_premium(update, context):
            return

        target_kb = int(action.split("_")[1])
        try:
            result = compress_image(context.user_data["working_image"], target_kb)
            context.user_data["working_image"] = result
            context.user_data["target_kb"] = target_kb
            record_operation(user_id)

            actual_kb = len(result) / 1024
            note = (
                f"✅ Compression completed.\n"
                f"Target: {target_kb} KB\n"
                f"Actual: {actual_kb:.2f} KB"
            )
            if actual_kb > target_kb:
                note += "\nThe exact target could not be achieved for this image."
            await message.reply_text(note)
            await ask_next(message)
        except Exception as error:
            print("Compression error:", repr(error))
            await message.reply_text("Compression failed. Please try another image.")
        return

    if action in ("edit_name", "edit_both"):
        if not await require_free_or_premium(update, context):
            return
        context.user_data["input_step"] = "name"
        context.user_data["name_edit_mode"] = action
        await message.reply_text(
            "Enter the name to display below the photo.\n"
            "Example: A. ANBIRKINIYAL"
        )
        return

    if action == "edit_date":
        if not await require_free_or_premium(update, context):
            return
        context.user_data["input_step"] = "date_choice"
        await message.reply_text("Choose the date option:", reply_markup=date_keyboard())
        return

    if action == "date_today":
        if not await require_free_or_premium(update, context):
            return
        context.user_data["photo_date"] = datetime.now(
            ZoneInfo(TIMEZONE_NAME)
        ).strftime("%d.%m.%Y")
        context.user_data.pop("input_step", None)
        record_operation(user_id)
        await message.reply_text(
            f"Today's date added: {context.user_data['photo_date']}"
        )
        await ask_next(message)
        return

    if action == "date_manual":
        context.user_data["input_step"] = "date"
        await message.reply_text(
            "Enter date in DD.MM.YYYY format. Example: 01.06.2026"
        )
        return

    if action == "date_keep":
        context.user_data.pop("input_step", None)
        # When a name was just entered, this completes the name + date operation.
        if context.user_data.get("name_edit_mode"):
            record_operation(user_id)
            context.user_data.pop("name_edit_mode", None)
        await message.reply_text("Existing date kept.")
        await ask_next(message)
        return

    if action == "date_skip":
        context.user_data["photo_date"] = ""
        context.user_data.pop("input_step", None)
        if context.user_data.get("name_edit_mode"):
            record_operation(user_id)
            context.user_data.pop("name_edit_mode", None)
        await message.reply_text("Date skipped.")
        await ask_next(message)
        return

    if action == "download_menu":
        await message.reply_text(
            "Choose your download format:",
            reply_markup=download_keyboard(),
        )
        return

    if action.startswith("download_"):
        output_format = action.split("_", 1)[1].upper()
        try:
            final_image = add_name_and_date(
                context.user_data["working_image"],
                context.user_data.get("photo_name", ""),
                context.user_data.get("photo_date", ""),
            )

            if output_format == "JPG" and context.user_data.get("target_kb"):
                target = context.user_data["target_kb"]
                result = compress_image(final_image, target)
                filename = "processed_photo.jpg"
            else:
                result, filename = convert_image(final_image, output_format)

            document = BytesIO(result)
            document.name = filename
            await message.reply_document(
                document=document,
                filename=filename,
                caption=f"Your {output_format} file is ready.",
            )
            await ask_next(message)
        except Exception as error:
            print("Download error:", repr(error))
            await message.reply_text("Unable to create that file. Please try again.")
        return


# ============================================================
# TEXT INPUT HANDLER
# ============================================================

async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("input_step")
    if not step:
        await update.message.reply_text(
            "Please use the operation buttons or send a new image."
        )
        return

    text = update.message.text.strip()
    user_id = update.effective_user.id

    if step == "name":
        if not text or len(text) > 60:
            await update.message.reply_text("Enter a name between 1 and 60 characters.")
            return

        context.user_data["photo_name"] = text
        context.user_data["input_step"] = "date_choice"
        await update.message.reply_text(
            f"Name saved: {text}\n\nNow choose the date option:",
            reply_markup=date_keyboard(),
        )
        return

    if step == "date":
        try:
            selected = datetime.strptime(text, "%d.%m.%Y")
            context.user_data["photo_date"] = selected.strftime("%d.%m.%Y")
            context.user_data.pop("input_step", None)

            if context.user_data.get("name_edit_mode"):
                context.user_data.pop("name_edit_mode", None)

            record_operation(user_id)
            await update.message.reply_text(
                f"Date saved: {context.user_data['photo_date']}"
            )
            await ask_next(update.message)
        except ValueError:
            await update.message.reply_text(
                "Invalid date. Use DD.MM.YYYY, for example 01.06.2026."
            )
        return

    if step == "date_choice":
        await update.message.reply_text(
            "Please choose one of the date buttons above."
        )
        return

    if step == "width":
        try:
            width = int(text)
            if not 1 <= width <= MAX_IMAGE_DIMENSION:
                raise ValueError

            context.user_data["resize_width"] = width
            context.user_data["input_step"] = "height"
            await update.message.reply_text("Now enter the required HEIGHT in pixels. Example: 230")
        except ValueError:
            await update.message.reply_text("Enter a whole-number width from 1 to 10000.")
        return

    if step == "height":
        try:
            height = int(text)
            if not 1 <= height <= MAX_IMAGE_DIMENSION:
                raise ValueError

            width = context.user_data.get("resize_width")
            image = open_image(context.user_data["working_image"])
            resized = image.resize((width, height), Image.Resampling.LANCZOS)

            output = BytesIO()
            resized.save(output, format="PNG")
            context.user_data["working_image"] = output.getvalue()

            context.user_data.pop("input_step", None)
            context.user_data.pop("resize_width", None)
            record_operation(user_id)

            await update.message.reply_text(
                f"✅ Dimensions updated!\nWidth: {width} px\nHeight: {height} px"
            )
            await ask_next(update.message)
        except ValueError:
            await update.message.reply_text("Enter a whole-number height from 1 to 10000.")
        except Exception as error:
            print("Resize error:", repr(error))
            context.user_data.pop("input_step", None)
            await update.message.reply_text("Unable to resize this image.")
        return


# ============================================================
# APPLICATION STARTUP
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Create a .env file and add BOT_TOKEN=your_token"
        )

    init_database()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("myplan", myplan_command))
    app.add_handler(CommandHandler("cancel", cancel_command))

    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(
        MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_handler)
    )

    app.add_handler(CallbackQueryHandler(button_handler))

    app.add_handler(
        MessageHandler(
            filters.PHOTO | filters.Document.IMAGE,
            receive_image,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            receive_text,
        )
    )

    print("Photo & Signature Resizer Bot is running!")
    app.run_polling()


if __name__ == "__main__":
    main()
