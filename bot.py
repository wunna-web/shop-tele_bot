import os
import re
import json
import sqlite3
from datetime import datetime
from typing import Optional, Tuple, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# =======================
# CONFIG
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = set()
_admin_env = os.getenv("ADMIN_IDS", "").strip()
if _admin_env:
    for x in _admin_env.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

DB_PATH = os.getenv("DB_PATH", "shop.db")

CURRENCY = "MMK"

# Conversation states (Admin)
A_CHOOSE, A_ADD_NAME, A_ADD_PRICE, A_ADD_DESC, A_ADD_PHOTO, A_EDIT_PICK, A_EDIT_FIELD, A_EDIT_VALUE, A_DEL_PICK = range(10)
P_SET_METHOD, P_SET_TEXT = range(10, 12)

# =======================
# DB
# =======================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        price INTEGER NOT NULL,
        description TEXT DEFAULT '',
        photo_file_id TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS carts (
        user_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        qty INTEGER NOT NULL,
        PRIMARY KEY (user_id, product_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        customer_name TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        address TEXT DEFAULT '',
        note TEXT DEFAULT '',
        total_amount INTEGER NOT NULL,
        status TEXT NOT NULL,               -- NEW, WAIT_PAYMENT, PAID, PACKING, SHIPPED, DONE, CANCELED
        payment_method TEXT DEFAULT '',
        payment_ref TEXT DEFAULT '',        -- transaction id or note
        payment_proof_file_id TEXT DEFAULT '',
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_items (
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        price INTEGER NOT NULL,
        qty INTEGER NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """)

    conn.commit()
    conn.close()

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def setting_get(key: str, default: str = "") -> str:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row["value"] if row else default

def setting_set(key: str, value: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
    conn.commit()
    conn.close()

# =======================
# Helpers
# =======================
def money(n: int) -> str:
    # simple formatting
    return f"{n:,} {CURRENCY}"

def now_iso() -> str:
    return datetime.utcnow().isoformat()

def get_active_products() -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE is_active = 1 ORDER BY id DESC")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_product(pid: int) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM products WHERE id = ?", (pid,))
    row = cur.fetchone()
    conn.close()
    return row

def cart_add(user_id: int, pid: int, qty: int = 1):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT qty FROM carts WHERE user_id=? AND product_id=?", (user_id, pid))
    r = cur.fetchone()
    if r:
        cur.execute("UPDATE carts SET qty=? WHERE user_id=? AND product_id=?", (r["qty"] + qty, user_id, pid))
    else:
        cur.execute("INSERT INTO carts(user_id, product_id, qty) VALUES(?,?,?)", (user_id, pid, qty))
    conn.commit()
    conn.close()

def cart_remove(user_id: int, pid: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM carts WHERE user_id=? AND product_id=?", (user_id, pid))
    conn.commit()
    conn.close()

def cart_clear(user_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def cart_list(user_id: int) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        SELECT c.product_id, c.qty, p.name, p.price
        FROM carts c JOIN products p ON p.id = c.product_id
        WHERE c.user_id=?
        ORDER BY p.id DESC
    """, (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def cart_total(user_id: int) -> int:
    items = cart_list(user_id)
    return sum(int(it["price"]) * int(it["qty"]) for it in items)

def create_order_from_cart(user_id: int, customer_name: str, phone: str, address: str, note: str) -> Tuple[int, int]:
    items = cart_list(user_id)
    if not items:
        return (0, 0)

    total = sum(int(it["price"]) * int(it["qty"]) for it in items)

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders(user_id, customer_name, phone, address, note, total_amount, status, created_at)
        VALUES(?,?,?,?,?,?,?,?)
    """, (user_id, customer_name, phone, address, note, total, "WAIT_PAYMENT", now_iso()))
    order_id = cur.lastrowid

    for it in items:
        cur.execute("""
            INSERT INTO order_items(order_id, product_id, name, price, qty)
            VALUES(?,?,?,?,?)
        """, (order_id, int(it["product_id"]), it["name"], int(it["price"]), int(it["qty"])))

    conn.commit()
    conn.close()

    cart_clear(user_id)
    return (order_id, total)

def order_get(order_id: int) -> Optional[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = ?", (order_id,))
    row = cur.fetchone()
    conn.close()
    return row

def order_items(order_id: int) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY rowid DESC", (order_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def orders_by_user(user_id: int) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 20", (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

def orders_all(limit: int = 50) -> List[sqlite3.Row]:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def order_update_payment(order_id: int, method: str, ref: str, proof_file_id: str = ""):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        UPDATE orders
        SET payment_method=?, payment_ref=?, payment_proof_file_id=?
        WHERE id=?
    """, (method, ref, proof_file_id, order_id))
    conn.commit()
    conn.close()

def order_set_status(order_id: int, status: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    conn.close()

# =======================
# UI Keyboards
# =======================
def kb_home(is_admin_user: bool) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("🛍 Product List", callback_data="C_LIST")],
        [InlineKeyboardButton("🧺 View Cart", callback_data="C_CART")],
        [InlineKeyboardButton("📦 My Orders", callback_data="C_MYORD")],
        [InlineKeyboardButton("💳 Payment Info", callback_data="C_PAYINFO")],
    ]
    if is_admin_user:
        buttons.append([InlineKeyboardButton("🛠 Admin Panel", callback_data="A_PANEL")])
    return InlineKeyboardMarkup(buttons)

def kb_product(pid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add to Cart", callback_data=f"C_ADD:{pid}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="C_LIST")],
        [InlineKeyboardButton("🏠 Home", callback_data="HOME")],
    ])

def kb_cart(items_exist: bool) -> InlineKeyboardMarkup:
    rows = []
    if items_exist:
        rows.append([InlineKeyboardButton("✅ Checkout", callback_data="C_CHECKOUT")])
    rows.append([InlineKeyboardButton("🛍 Continue Shopping", callback_data="C_LIST")])
    rows.append([InlineKeyboardButton("🏠 Home", callback_data="HOME")])
    return InlineKeyboardMarkup(rows)

def kb_admin_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add Product", callback_data="A_ADD")],
        [InlineKeyboardButton("✏️ Edit Product", callback_data="A_EDIT")],
        [InlineKeyboardButton("🗑 Delete Product", callback_data="A_DEL")],
        [InlineKeyboardButton("💳 Set Payment Info", callback_data="A_PAYSET")],
        [InlineKeyboardButton("📦 View Orders", callback_data="A_ORDERS")],
        [InlineKeyboardButton("🏠 Home", callback_data="HOME")],
    ])

def kb_admin_orders(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Mark PAID", callback_data=f"A_OS:PAID:{order_id}")],
        [InlineKeyboardButton("📦 PACKING", callback_data=f"A_OS:PACKING:{order_id}"),
         InlineKeyboardButton("🚚 SHIPPED", callback_data=f"A_OS:SHIPPED:{order_id}")],
        [InlineKeyboardButton("🏁 DONE", callback_data=f"A_OS:DONE:{order_id}"),
         InlineKeyboardButton("❌ CANCELED", callback_data=f"A_OS:CANCELED:{order_id}")],
        [InlineKeyboardButton("⬅️ Back to Orders", callback_data="A_ORDERS")],
        [InlineKeyboardButton("🏠 Home", callback_data="HOME")],
    ])

# =======================
# Customer flows
# =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    msg = (
        f"မင်္ဂလာပါ {user.first_name} 👋\n\n"
        f"ဒီ Bot က Products / Price List / Order / Payment (manual confirm) လုပ်လို့ရပါတယ်။\n\n"
        f"🆔 Your ID: `{user.id}`\n"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_home(is_admin(user.id)))

async def on_home(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    text = "🏠 Home Menu"
    await q.edit_message_text(text, reply_markup=kb_home(is_admin(user_id)))

async def customer_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    prods = get_active_products()
    if not prods:
        await q.edit_message_text("မရှိသေးပါ။ (Admin က product မထည့်သေးပါ)", reply_markup=kb_home(is_admin(q.from_user.id)))
        return

    lines = ["🛍 *Product List* (နောက်ဆုံးထည့်ထားတာအပေါ်)\n"]
    buttons = []
    for p in prods[:15]:
        lines.append(f"• `{p['id']}` — *{p['name']}* — {money(int(p['price']))}")
        buttons.append([InlineKeyboardButton(f"{p['name']} — {money(int(p['price']))}", callback_data=f"C_VIEW:{p['id']}")])

    buttons.append([InlineKeyboardButton("🧺 View Cart", callback_data="C_CART")])
    buttons.append([InlineKeyboardButton("🏠 Home", callback_data="HOME")])

    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

async def customer_view_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, pid = q.data.split(":")
    pid = int(pid)
    p = get_product(pid)
    if not p or int(p["is_active"]) != 1:
        await q.edit_message_text("ဒီ product မတွေ့ပါ (ဖျက်ထားတာဖြစ်နိုင်ပါတယ်)", reply_markup=kb_home(is_admin(q.from_user.id)))
        return

    caption = (
        f"🧾 *{p['name']}*\n"
        f"💰 Price: *{money(int(p['price']))}*\n\n"
        f"{p['description'] or ''}"
    )

    # If photo exists, send photo, else edit message
    if p["photo_file_id"]:
        await q.delete_message()
        await context.bot.send_photo(
            chat_id=q.message.chat_id,
            photo=p["photo_file_id"],
            caption=caption,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_product(pid),
        )
    else:
        await q.edit_message_text(caption, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_product(pid))

async def customer_add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, pid = q.data.split(":")
    pid = int(pid)
    p = get_product(pid)
    if not p or int(p["is_active"]) != 1:
        await q.edit_message_text("ဒီ product မတွေ့ပါ", reply_markup=kb_home(is_admin(q.from_user.id)))
        return
    cart_add(q.from_user.id, pid, 1)
    await q.edit_message_text(f"✅ Cart ထဲထည့်ပြီးပါ: *{p['name']}*\n\n🧺 Cart ကိုကြည့်မလား?", parse_mode=ParseMode.MARKDOWN,
                              reply_markup=InlineKeyboardMarkup([
                                  [InlineKeyboardButton("🧺 View Cart", callback_data="C_CART")],
                                  [InlineKeyboardButton("🛍 Continue Shopping", callback_data="C_LIST")],
                                  [InlineKeyboardButton("🏠 Home", callback_data="HOME")],
                              ]))

async def customer_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    items = cart_list(q.from_user.id)
    if not items:
        await q.edit_message_text("🧺 Cart ထဲမှာ မရှိသေးပါ", reply_markup=kb_cart(False))
        return

    lines = ["🧺 *Your Cart*\n"]
    total = 0
    buttons = []
    for it in items:
        line_total = int(it["price"]) * int(it["qty"])
        total += line_total
        lines.append(f"• `{it['product_id']}` *{it['name']}* x{it['qty']} = {money(line_total)}")
        buttons.append([InlineKeyboardButton(f"➖ Remove {it['name']}", callback_data=f"C_REM:{it['product_id']}")])

    lines.append(f"\n💵 *Total:* {money(total)}")
    buttons.append([InlineKeyboardButton("✅ Checkout", callback_data="C_CHECKOUT")])
    buttons.append([InlineKeyboardButton("🛍 Continue Shopping", callback_data="C_LIST")])
    buttons.append([InlineKeyboardButton("🏠 Home", callback_data="HOME")])

    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

async def customer_remove_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, pid = q.data.split(":")
    pid = int(pid)
    cart_remove(q.from_user.id, pid)
    await customer_cart(update, context)

async def customer_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    items = cart_list(q.from_user.id)
    if not items:
        await q.edit_message_text("Cart မရှိပါ", reply_markup=kb_home(is_admin(q.from_user.id)))
        return

    # Ask details in chat
    context.user_data["checkout_step"] = "name"
    await q.edit_message_text(
        "✅ Checkout လုပ်မယ်ဆိုရင် အောက်က အချက်အလက်တွေလိုပါတယ်။\n\n"
        "1) Customer Name ကို ထည့်ပေးပါ (ဥပမာ: Aung Aung)\n\n"
        "Cancel လုပ်ချင်ရင် /cancel လို့ရပါတယ်။"
    )

async def checkout_collect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    step = context.user_data.get("checkout_step")
    if step not in {"name", "phone", "address", "note"}:
        return

    text = (update.message.text or "").strip()

    if step == "name":
        context.user_data["co_name"] = text
        context.user_data["checkout_step"] = "phone"
        await update.message.reply_text("2) ဖုန်းနံပါတ် ထည့်ပေးပါ (ဥပမာ: 09xxxxxxxxx)")
        return

    if step == "phone":
        # very loose validation
        if not re.search(r"\d{7,12}", text):
            await update.message.reply_text("ဖုန်းနံပါတ်ပုံစံမမှန်ပါ။ ပြန်ထည့်ပေးပါ (ဥပမာ: 09xxxxxxxxx)")
            return
        context.user_data["co_phone"] = text
        context.user_data["checkout_step"] = "address"
        await update.message.reply_text("3) ပို့မယ့် Address ထည့်ပေးပါ")
        return

    if step == "address":
        context.user_data["co_address"] = text
        context.user_data["checkout_step"] = "note"
        await update.message.reply_text("4) Note (optional) — မလိုရင် `-` လို့ပဲ ထည့်ပါ")
        return

    if step == "note":
        note = "" if text == "-" else text
        name = context.user_data.get("co_name", "")
        phone = context.user_data.get("co_phone", "")
        address = context.user_data.get("co_address", "")

        order_id, total = create_order_from_cart(update.effective_user.id, name, phone, address, note)
        context.user_data["checkout_step"] = None

        if order_id == 0:
            await update.message.reply_text("Cart မရှိတော့ပါ။")
            return

        pay_methods = setting_get("payment_methods", "KBZPay,WavePay,COD")
        pay_text = setting_get("payment_text", "Payment info ကို Admin မသတ်မှတ်သေးပါ။")

        msg = (
            f"✅ *Order တင်ပြီးပါပြီ*\n"
            f"📦 Order ID: `{order_id}`\n"
            f"💵 Total: *{money(total)}*\n"
            f"📌 Status: `WAIT_PAYMENT`\n\n"
            f"💳 *Payment Methods:* {pay_methods}\n"
            f"{pay_text}\n\n"
            f"ငွေလွှဲပြီးရင် ဒီလိုအတည်ပြုပါ:\n"
            f"• `/pay {order_id} KBZPay 123456` (method + transaction id)\n"
            f"• သို့မဟုတ် Screenshot ကို အဲဒီ Order ID နဲ့အတူပို့နိုင်ပါတယ်။\n\n"
            f"ဥပမာ: `/pay {order_id} WavePay 987654`"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_home(is_admin(update.effective_user.id)))

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["checkout_step"] = None
    await update.message.reply_text("❌ Cancel လုပ်ပြီးပါပြီ", reply_markup=kb_home(is_admin(update.effective_user.id)))

async def pay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Customer: /pay <order_id> <method> <reference>
    Example: /pay 12 KBZPay 123456
    """
    parts = (update.message.text or "").strip().split(maxsplit=3)
    if len(parts) < 4:
        await update.message.reply_text("အသုံးပြုပုံ: `/pay <order_id> <method> <transaction_id>`\nဥပမာ: `/pay 12 KBZPay 123456`",
                                        parse_mode=ParseMode.MARKDOWN)
        return

    _, order_id_s, method, ref = parts
    if not order_id_s.isdigit():
        await update.message.reply_text("order_id မမှန်ပါ")
        return
    order_id = int(order_id_s)
    o = order_get(order_id)
    if not o or int(o["user_id"]) != update.effective_user.id:
        await update.message.reply_text("ဒီ Order ကိုမတွေ့ပါ (သို့) သင့် order မဟုတ်ပါ")
        return

    order_update_payment(order_id, method, ref, proof_file_id=o["payment_proof_file_id"] or "")
    # keep status WAIT_PAYMENT until admin confirms
    await update.message.reply_text(
        f"✅ Payment Info ထည့်ပြီးပါပြီ\n"
        f"Order ID: `{order_id}`\n"
        f"Method: *{method}*\n"
        f"Ref: `{ref}`\n\n"
        f"Admin ကစစ်ပြီး Confirm လုပ်ပေးမယ်။",
        parse_mode=ParseMode.MARKDOWN
    )

    # notify admins
    for aid in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=aid,
                text=(
                    f"🔔 *Payment Submit*\n"
                    f"Order ID: `{order_id}`\n"
                    f"User: `{update.effective_user.id}`\n"
                    f"Method: *{method}*\n"
                    f"Ref: `{ref}`\n"
                    f"Status: `{o['status']}`\n\n"
                    f"/order {order_id} ကိုစစ်ပါ"
                ),
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception:
            pass

async def pay_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Customer sends photo proof with caption like: "Order 12" or "12"
    """
    caption = (update.message.caption or "").strip()
    m = re.search(r"(\d+)", caption)
    if not m:
        await update.message.reply_text("Screenshot ပို့မယ်ဆို caption ထဲမှာ Order ID ထည့်ပေးပါ။ ဥပမာ: `Order 12`", parse_mode=ParseMode.MARKDOWN)
        return

    order_id = int(m.group(1))
    o = order_get(order_id)
    if not o or int(o["user_id"]) != update.effective_user.id:
        await update.message.reply_text("ဒီ Order ကိုမတွေ့ပါ (သို့) သင့် order မဟုတ်ပါ")
        return

    photo = update.message.photo[-1]
    file_id = photo.file_id
    # if method/ref missing, keep existing
    order_update_payment(order_id, o["payment_method"] or "UNKNOWN", o["payment_ref"] or "PHOTO_PROOF", proof_file_id=file_id)

    await update.message.reply_text(f"✅ Payment Screenshot လက်ခံပြီးပါပြီ (Order `{order_id}`)\nAdmin ကစစ်ပြီး Confirm လုပ်ပေးမယ်။",
                                    parse_mode=ParseMode.MARKDOWN)

    for aid in ADMIN_IDS:
        try:
            await context.bot.send_photo(
                chat_id=aid,
                photo=file_id,
                caption=f"🔔 Payment Proof for Order {order_id}\nUser: {update.effective_user.id}\nUse: /order {order_id}",
            )
        except Exception:
            pass

async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    rows = orders_by_user(q.from_user.id)
    if not rows:
        await q.edit_message_text("📦 Order မရှိသေးပါ", reply_markup=kb_home(is_admin(q.from_user.id)))
        return

    lines = ["📦 *My Orders* (နောက်ဆုံး 20 ခု)\n"]
    buttons = []
    for o in rows:
        lines.append(f"• `{o['id']}` — {money(int(o['total_amount']))} — `{o['status']}`")
        buttons.append([InlineKeyboardButton(f"Order {o['id']} — {o['status']}", callback_data=f"C_ORD:{o['id']}")])

    buttons.append([InlineKeyboardButton("🏠 Home", callback_data="HOME")])
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

async def customer_order_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, order_id_s = q.data.split(":")
    order_id = int(order_id_s)
    o = order_get(order_id)
    if not o or int(o["user_id"]) != q.from_user.id:
        await q.edit_message_text("Order မတွေ့ပါ", reply_markup=kb_home(is_admin(q.from_user.id)))
        return

    its = order_items(order_id)
    lines = [
        f"📦 *Order Detail*",
        f"Order ID: `{o['id']}`",
        f"Status: `{o['status']}`",
        f"Total: *{money(int(o['total_amount']))}*",
        "",
        "*Items:*"
    ]
    for it in its:
        lines.append(f"• {it['name']} x{it['qty']} = {money(int(it['price']) * int(it['qty']))}")

    if o["payment_method"] or o["payment_ref"]:
        lines += [
            "",
            f"Payment: *{o['payment_method']}*",
            f"Ref: `{o['payment_ref']}`",
        ]

    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN,
                              reply_markup=InlineKeyboardMarkup([
                                  [InlineKeyboardButton("💳 Payment Info", callback_data="C_PAYINFO")],
                                  [InlineKeyboardButton("🏠 Home", callback_data="HOME")],
                              ]))

async def customer_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pay_methods = setting_get("payment_methods", "KBZPay,WavePay,COD")
    pay_text = setting_get("payment_text", "Payment info ကို Admin မသတ်မှတ်သေးပါ။")

    msg = (
        f"💳 *Payment Info*\n\n"
        f"*Methods:* {pay_methods}\n\n"
        f"{pay_text}\n\n"
        f"ငွေလွှဲပြီးရင် `/pay <order_id> <method> <transaction_id>` နဲ့အတည်ပြုပါ။"
    )
    await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Home", callback_data="HOME")]
    ]))

# =======================
# Admin flows
# =======================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.edit_message_text("Admin မဟုတ်ပါ", reply_markup=kb_home(False))
        return
    await q.edit_message_text("🛠 *Admin Panel*", parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_panel())

async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.edit_message_text("Admin မဟုတ်ပါ")
        return ConversationHandler.END

    context.user_data["new_product"] = {}
    await q.edit_message_text("➕ Product Name ထည့်ပါ")
    return A_ADD_NAME

async def admin_add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = (update.message.text or "").strip()
    if not name:
        await update.message.reply_text("Name မထည့်ရသေးပါ။ ပြန်ထည့်ပါ")
        return A_ADD_NAME
    context.user_data["new_product"]["name"] = name
    await update.message.reply_text("Price (MMK) ကို နံပါတ်နဲ့ထည့်ပါ (ဥပမာ: 15000)")
    return A_ADD_PRICE

async def admin_add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip().replace(",", "")
    if not text.isdigit():
        await update.message.reply_text("Price ကို နံပါတ်နဲ့သာထည့်ပါ (ဥပမာ: 15000)")
        return A_ADD_PRICE
    context.user_data["new_product"]["price"] = int(text)
    await update.message.reply_text("Description ထည့်ပါ (မလိုရင် `-` လို့ထည့်)")
    return A_ADD_DESC

async def admin_add_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    desc = (update.message.text or "").strip()
    context.user_data["new_product"]["description"] = "" if desc == "-" else desc
    await update.message.reply_text("Product Photo ပို့ပါ (မလိုရင် `-` လို့ရိုက်)")
    return A_ADD_PHOTO

async def admin_add_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    np = context.user_data.get("new_product", {})
    photo_file_id = ""
    if update.message.photo:
        photo_file_id = update.message.photo[-1].file_id
    else:
        text = (update.message.text or "").strip()
        if text != "-":
            await update.message.reply_text("Photo ပို့ပါ (သို့) မလိုရင် `-` လို့ထည့်ပါ")
            return A_ADD_PHOTO

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO products(name, price, description, photo_file_id, is_active, created_at)
        VALUES(?,?,?,?,?,?)
    """, (np.get("name",""), int(np.get("price",0)), np.get("description",""), photo_file_id, 1, now_iso()))
    conn.commit()
    pid = cur.lastrowid
    conn.close()

    context.user_data["new_product"] = {}
    await update.message.reply_text(f"✅ Product ထည့်ပြီးပါပြီ (ID: {pid})", reply_markup=kb_home(True))
    return ConversationHandler.END

async def admin_edit_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.edit_message_text("Admin မဟုတ်ပါ")
        return ConversationHandler.END

    prods = get_active_products()
    if not prods:
        await q.edit_message_text("Product မရှိသေးပါ", reply_markup=kb_admin_panel())
        return ConversationHandler.END

    buttons = []
    for p in prods[:20]:
        buttons.append([InlineKeyboardButton(f"{p['id']} — {p['name']}", callback_data=f"A_EP:{p['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="A_PANEL")])

    await q.edit_message_text("✏️ Edit လုပ်မယ့် Product ကိုရွေးပါ", reply_markup=InlineKeyboardMarkup(buttons))
    return A_EDIT_PICK

async def admin_edit_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return ConversationHandler.END

    _, pid = q.data.split(":")
    pid = int(pid)
    p = get_product(pid)
    if not p:
        await q.edit_message_text("Product မတွေ့ပါ", reply_markup=kb_admin_panel())
        return ConversationHandler.END

    context.user_data["edit_pid"] = pid
    await q.edit_message_text(
        f"✏️ *{p['name']}* ကိုဘာပြင်မလဲ?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Name", callback_data="A_EF:name"),
             InlineKeyboardButton("Price", callback_data="A_EF:price")],
            [InlineKeyboardButton("Description", callback_data="A_EF:description"),
             InlineKeyboardButton("Photo", callback_data="A_EF:photo")],
            [InlineKeyboardButton("⬅️ Back", callback_data="A_EDIT")],
        ])
    )
    return A_EDIT_FIELD

async def admin_edit_field(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return ConversationHandler.END

    _, field = q.data.split(":")
    context.user_data["edit_field"] = field

    if field == "photo":
        await q.edit_message_text("Photo အသစ်ပို့ပါ (Cancel: /cancel)")
    else:
        await q.edit_message_text(f"{field} အသစ်ကို ထည့်ပါ (Cancel: /cancel)")
    return A_EDIT_VALUE

async def admin_edit_value(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    pid = int(context.user_data.get("edit_pid", 0))
    field = context.user_data.get("edit_field", "")
    if not pid or not field:
        return ConversationHandler.END

    value = None
    if field == "photo":
        if not update.message.photo:
            await update.message.reply_text("Photo ပို့ပါ")
            return A_EDIT_VALUE
        value = update.message.photo[-1].file_id
    else:
        text = (update.message.text or "").strip()
        if field == "price":
            t = text.replace(",", "")
            if not t.isdigit():
                await update.message.reply_text("Price ကို နံပါတ်နဲ့ထည့်ပါ")
                return A_EDIT_VALUE
            value = int(t)
        else:
            value = text

    conn = db()
    cur = conn.cursor()
    if field == "price":
        cur.execute("UPDATE products SET price=? WHERE id=?", (int(value), pid))
    elif field == "name":
        cur.execute("UPDATE products SET name=? WHERE id=?", (str(value), pid))
    elif field == "description":
        cur.execute("UPDATE products SET description=? WHERE id=?", (str(value), pid))
    elif field == "photo":
        cur.execute("UPDATE products SET photo_file_id=? WHERE id=?", (str(value), pid))
    conn.commit()
    conn.close()

    await update.message.reply_text("✅ Update ပြီးပါပြီ", reply_markup=kb_admin_panel())
    return ConversationHandler.END

async def admin_del_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.edit_message_text("Admin မဟုတ်ပါ")
        return ConversationHandler.END

    prods = get_active_products()
    if not prods:
        await q.edit_message_text("Product မရှိသေးပါ", reply_markup=kb_admin_panel())
        return ConversationHandler.END

    buttons = []
    for p in prods[:20]:
        buttons.append([InlineKeyboardButton(f"🗑 {p['id']} — {p['name']}", callback_data=f"A_DP:{p['id']}")])
    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="A_PANEL")])

    await q.edit_message_text("🗑 ဖျက်မယ့် Product ကိုရွေးပါ (soft delete)", reply_markup=InlineKeyboardMarkup(buttons))
    return A_DEL_PICK

async def admin_del_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return ConversationHandler.END
    _, pid = q.data.split(":")
    pid = int(pid)
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE products SET is_active=0 WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    await q.edit_message_text(f"✅ Product {pid} ကိုဖျက်ထားပြီးပါပြီ", reply_markup=kb_admin_panel())
    return ConversationHandler.END

async def admin_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.edit_message_text("Admin မဟုတ်ပါ")
        return

    rows = orders_all(50)
    if not rows:
        await q.edit_message_text("Orders မရှိသေးပါ", reply_markup=kb_admin_panel())
        return

    buttons = []
    lines = ["📦 *Latest Orders* (50)\n"]
    for o in rows[:20]:
        lines.append(f"• `{o['id']}` — {money(int(o['total_amount']))} — `{o['status']}` — user `{o['user_id']}`")
        buttons.append([InlineKeyboardButton(f"Order {o['id']} — {o['status']}", callback_data=f"A_OV:{o['id']}")])

    buttons.append([InlineKeyboardButton("⬅️ Back", callback_data="A_PANEL")])
    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(buttons))

async def admin_order_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    _, order_id_s = q.data.split(":")
    order_id = int(order_id_s)
    o = order_get(order_id)
    if not o:
        await q.edit_message_text("Order မတွေ့ပါ", reply_markup=kb_admin_panel())
        return

    its = order_items(order_id)
    lines = [
        f"📦 *Order #{o['id']}*",
        f"User: `{o['user_id']}`",
        f"Name: {o['customer_name']}",
        f"Phone: {o['phone']}",
        f"Address: {o['address']}",
        f"Note: {o['note']}",
        f"Total: *{money(int(o['total_amount']))}*",
        f"Status: `{o['status']}`",
        "",
        "*Items:*",
    ]
    for it in its:
        lines.append(f"• {it['name']} x{it['qty']} = {money(int(it['price']) * int(it['qty']))}")

    lines += [
        "",
        f"Payment Method: *{o['payment_method']}*",
        f"Payment Ref: `{o['payment_ref']}`",
    ]

    await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_orders(order_id))

    # If proof exists, send it separately
    if o["payment_proof_file_id"]:
        try:
            await context.bot.send_photo(chat_id=q.message.chat_id, photo=o["payment_proof_file_id"],
                                         caption=f"Payment Proof — Order {order_id}")
        except Exception:
            pass

async def admin_order_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return

    _, status, order_id_s = q.data.split(":")
    order_id = int(order_id_s)
    o = order_get(order_id)
    if not o:
        await q.edit_message_text("Order မတွေ့ပါ")
        return

    order_set_status(order_id, status)

    # notify customer
    try:
        await context.bot.send_message(
            chat_id=int(o["user_id"]),
            text=f"🔔 Order `{order_id}` status updated: `{status}`",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception:
        pass

    await q.edit_message_text(f"✅ Order {order_id} ကို `{status}` သတ်မှတ်ပြီးပါပြီ", parse_mode=ParseMode.MARKDOWN,
                              reply_markup=kb_admin_panel())

# Admin payment settings
async def admin_payset_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        await q.edit_message_text("Admin မဟုတ်ပါ")
        return ConversationHandler.END

    current_methods = setting_get("payment_methods", "KBZPay,WavePay,COD")
    current_text = setting_get("payment_text", "Payment info ကို Admin မသတ်မှတ်သေးပါ။")
    await q.edit_message_text(
        "💳 Payment Info သတ်မှတ်မယ်\n\n"
        f"Current methods: `{current_methods}`\n"
        f"Current text:\n{current_text}\n\n"
        "အသစ် methods ကို comma နဲ့ထည့်ပါ (ဥပမာ: KBZPay,WavePay,AyaPay,COD)",
        parse_mode=ParseMode.MARKDOWN
    )
    return P_SET_METHOD

async def admin_payset_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    methods = (update.message.text or "").strip()
    if not methods:
        await update.message.reply_text("methods မထည့်ရသေးပါ")
        return P_SET_METHOD

    setting_set("payment_methods", methods)
    await update.message.reply_text(
        "✅ methods သိမ်းပြီးပါပြီ။\n\n"
        "အခု payment instruction text ကိုထည့်ပါ。\n"
        "ဥပမာ:\n"
        "KBZPay - 09xxxxxxx (Name: xxx)\n"
        "WavePay - 09xxxxxxx\n"
        "COD - Yangon only\n\n"
        "(Cancel: /cancel)"
    )
    return P_SET_TEXT

async def admin_payset_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    setting_set("payment_text", text)
    await update.message.reply_text("✅ Payment Info အကုန်သတ်မှတ်ပြီးပါပြီ", reply_markup=kb_admin_panel())
    return ConversationHandler.END

# Admin command: /order <id>
async def admin_order_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    parts = (update.message.text or "").strip().split()
    if len(parts) < 2 or not parts[1].isdigit():
        await update.message.reply_text("အသုံးပြုပုံ: /order 12")
        return
    order_id = int(parts[1])
    o = order_get(order_id)
    if not o:
        await update.message.reply_text("Order မတွေ့ပါ")
        return
    its = order_items(order_id)
    lines = [
        f"📦 *Order #{o['id']}*",
        f"User: `{o['user_id']}`",
        f"Name: {o['customer_name']}",
        f"Phone: {o['phone']}",
        f"Address: {o['address']}",
        f"Total: *{money(int(o['total_amount']))}*",
        f"Status: `{o['status']}`",
        "",
        "*Items:*",
    ]
    for it in its:
        lines.append(f"• {it['name']} x{it['qty']}")

    lines += [
        "",
        f"Payment: *{o['payment_method']}*",
        f"Ref: `{o['payment_ref']}`",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=kb_admin_orders(order_id))
    if o["payment_proof_file_id"]:
        try:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=o["payment_proof_file_id"],
                                         caption=f"Payment Proof — Order {order_id}")
        except Exception:
            pass

# =======================
# Router for callback
# =======================
async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data

    # Home
    if data == "HOME":
        return await on_home(update, context)

    # Customer
    if data == "C_LIST":
        return await customer_list(update, context)
    if data.startswith("C_VIEW:"):
        return await customer_view_product(update, context)
    if data.startswith("C_ADD:"):
        return await customer_add_to_cart(update, context)
    if data == "C_CART":
        return await customer_cart(update, context)
    if data.startswith("C_REM:"):
        return await customer_remove_item(update, context)
    if data == "C_CHECKOUT":
        return await customer_checkout(update, context)
    if data == "C_MYORD":
        return await my_orders(update, context)
    if data.startswith("C_ORD:"):
        return await customer_order_detail(update, context)
    if data == "C_PAYINFO":
        return await customer_payment_info(update, context)

    # Admin shortcut
    if data == "A_PANEL":
        return await admin_panel(update, context)

    # Admin orders view (not conversation)
    if data == "A_ORDERS":
        return await admin_orders(update, context)
    if data.startswith("A_OV:"):
        return await admin_order_view(update, context)
    if data.startswith("A_OS:"):
        return await admin_order_status(update, context)

    # Unknown
    await q.answer("Unknown action", show_alert=False)

# =======================
# Main
# =======================
def main():
    if not BOT_TOKEN:
        raise SystemExit("Missing BOT_TOKEN environment variable")

    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    # Customer basic
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("cancel", cancel))
    app.add_handler(CommandHandler("pay", pay_command))
    app.add_handler(CommandHandler("order", admin_order_cmd))

    # Checkout message collector
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_collect))

    # Payment proof photo
    app.add_handler(MessageHandler(filters.PHOTO, pay_photo))

    # Admin conversations: Add / Edit / Delete / Pay settings
    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_start, pattern=r"^A_ADD$")],
        states={
            A_ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_name)],
            A_ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_price)],
            A_ADD_DESC: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_desc)],
            A_ADD_PHOTO: [
                MessageHandler(filters.PHOTO, admin_add_photo),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_photo),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="add_product",
        persistent=False,
    )
    app.add_handler(add_conv)

    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_edit_start, pattern=r"^A_EDIT$")],
        states={
            A_EDIT_PICK: [CallbackQueryHandler(admin_edit_pick, pattern=r"^A_EP:\d+$")],
            A_EDIT_FIELD: [CallbackQueryHandler(admin_edit_field, pattern=r"^A_EF:(name|price|description|photo)$")],
            A_EDIT_VALUE: [
                MessageHandler(filters.PHOTO, admin_edit_value),
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_edit_value),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(admin_edit_start, pattern=r"^A_EDIT$")],
        name="edit_product",
        persistent=False,
    )
    app.add_handler(edit_conv)

    del_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_del_start, pattern=r"^A_DEL$")],
        states={
            A_DEL_PICK: [CallbackQueryHandler(admin_del_pick, pattern=r"^A_DP:\d+$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="del_product",
        persistent=False,
    )
    app.add_handler(del_conv)

    payset_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_payset_start, pattern=r"^A_PAYSET$")],
        states={
            P_SET_METHOD: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_payset_method)],
            P_SET_TEXT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_payset_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="pay_settings",
        persistent=False,
    )
    app.add_handler(payset_conv)

    # Router for all other callback queries
    app.add_handler(CallbackQueryHandler(callback_router))

    print("Bot is running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()