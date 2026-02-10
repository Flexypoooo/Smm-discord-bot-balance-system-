import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import sqlite3
import json
from datetime import datetime

BOT_TOKEN = "YOUR_BOT_TOKEN"
API_KEY = "YOUR_API_KEY"
OWNER_IDS = [YOUR_DISCORD_ID]
ADMIN_CHANNEL_ID = YOUR_CHANNEL_ID
LOG_WEBHOOK_URL = "YOUR_WEBHOOK_URL"
API_URL = "https://smmpanel.com/api/v2"
DB = "database.db"

# ========================
# SERVICES & PRICING
# ========================
SERVICES = {
    "instagram followers": 1834,
    "instagram likes": 793,
    "tiktok followers": 2004,
    "tiktok likes": 1663,
    "tiktok views": 1885
}

SERVICE_PRICES = {
    "instagram followers": PUT 2.0 FOR 2 DOLLARS.,
    "instagram likes": 4.0,
    "tiktok followers": 4.0,
    "tiktok likes": 4.0,
    "tiktok views": 4.0
}

# ========================
# DATABASE SETUP
# ========================
def db_init():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance REAL DEFAULT 0,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            api_order_id INTEGER,
            service_id INTEGER,
            service_name TEXT,
            quantity INTEGER,
            price REAL,
            status TEXT,
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS refunds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER,
            user_id INTEGER,
            amount REAL,
            reason TEXT,
            status TEXT DEFAULT 'PENDING',
            created TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()

db_init()

# ========================
# HELPERS
# ========================
def get_balance(user_id: int) -> float:
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0.0

def update_balance(user_id: int, amount: float):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, balance) VALUES (?, 0)", (user_id,))
    c.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
    conn.commit()
    conn.close()

async def api_request(payload: dict) -> dict:
    """POST request to SMMPANEL API with robust JSON decoding."""
    async with aiohttp.ClientSession() as session:
        async with session.post(API_URL, data=payload) as response:
            try:
                return await response.json()
            except json.JSONDecodeError:
                text = await response.text()
                return {"raw_response": text}

async def send_webhook(title: str, description: str, color=0x00FFFF):
    async with aiohttp.ClientSession() as session:
        await session.post(
            LOG_WEBHOOK_URL,
            json={"embeds":[{"title": title, "description": description, "color": color}]}
        )

async def log_admin(bot, embed):
    channel = bot.get_channel(ADMIN_CHANNEL_ID)
    if channel:
        await channel.send(embed=embed)

# ========================
# BACKGROUND TASKS
# ========================
@tasks.loop(minutes=3)
async def order_checker(bot):
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("SELECT id, user_id, api_order_id, price, status FROM orders WHERE status='IN_PROGRESS'")
    rows = c.fetchall()
    for internal_id, user_id, api_order_id, price, status in rows:
        payload = {"key": API_KEY, "action": "status", "order": api_order_id}
        try:
            res = await api_request(payload)
            if "status" in res and res["status"].lower() in ["completed", "partial"]:
                new_status = res["status"]
                c.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, internal_id))
                conn.commit()
                embed = discord.Embed(
                    title=f"Order Completed #{internal_id}",
                    description=(f"User <@{user_id}>'s order completed.\n"
                                 f"Status: {new_status}\n"
                                 f"Amount Spent: ${price:.2f}\n"
                                 f"New Balance: ${get_balance(user_id):.2f}"),
                    color=0x00FF99
                )
                await log_admin(bot, embed)
        except Exception as e:
            print("Order check failed:", e)
    conn.close()

# ========================
# BOT COMMANDS
# ========================
class SMM(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # -------- BALANCE --------
    @app_commands.command(name="balance", description="Check your balance")
    async def balance(self, interaction: discord.Interaction):
        bal = get_balance(interaction.user.id)
        embed = discord.Embed(title="💰 Your Balance", description=f"${bal:.2f}", color=0x00FF99)
        await interaction.response.send_message(embed=embed, ephemeral=False)

    # -------- ORDER --------
    @app_commands.command(name="order", description="Place a social media order")
    @app_commands.describe(service_name="Service Name (e.g., instagram followers)", link="Profile link", quantity="Quantity")
    async def order(self, interaction: discord.Interaction, service_name: str, link: str, quantity: int):
        user_id = interaction.user.id
        service_key = service_name.lower()
        if service_key not in SERVICES:
            return await interaction.response.send_message(f"❌ Service `{service_name}` not found.", ephemeral=False)

        service_id = SERVICES[service_key]
        cost_per_1000 = SERVICE_PRICES.get(service_key, 2.0)
        cost = (cost_per_1000 / 1000) * quantity
        balance = get_balance(user_id)
        if balance < cost:
            return await interaction.response.send_message(f"❌ Insufficient balance. Cost: ${cost:.2f} | Your Balance: ${balance:.2f}", ephemeral=False)
        update_balance(user_id, -cost)

        payload = {"key": API_KEY, "action": "add", "service": service_id, "link": link, "quantity": quantity}
        api_res = await api_request(payload)

        if "order" not in api_res:
            # Refund request
            conn = sqlite3.connect(DB)
            c = conn.cursor()
            c.execute("INSERT INTO refunds (order_id, user_id, amount, reason) VALUES (?, ?, ?, ?)",
                      (0, user_id, cost, json.dumps(api_res)))
            refund_id = c.lastrowid
            conn.commit()
            conn.close()
            await send_webhook(f"Refund Request #{refund_id}", f"User <@{user_id}> refund due to API failure.", color=0xFF0000)
            return await interaction.response.send_message("❌ API failed. Refund request sent.", ephemeral=False)

        api_order_id = api_res["order"]
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        c.execute("""
            INSERT INTO orders (user_id, api_order_id, service_id, service_name, quantity, price, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (user_id, api_order_id, service_id, service_name, quantity, cost, "IN_PROGRESS"))
        internal_order_id = c.lastrowid
        conn.commit()
        conn.close()

        embed = discord.Embed(
            title=f"Order #{internal_order_id} Placed",
            description=f"User <@{user_id}> placed an order.",
            color=0x00FF99
        )
        embed.add_field(name="Service", value=f"{service_name} (ID {service_id})", inline=False)
        embed.add_field(name="Quantity", value=str(quantity), inline=False)
        embed.add_field(name="Cost", value=f"${cost:.2f}", inline=False)
        embed.add_field(name="New Balance", value=f"${get_balance(user_id):.2f}", inline=False)
        embed.add_field(name="API Order ID", value=str(api_order_id), inline=False)
        embed.add_field(name="Price per 1000", value=f"${cost_per_1000:.2f}", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=False)
        await log_admin(self.bot, embed)
        await send_webhook(f"New Order #{internal_order_id}", f"User <@{user_id}> ordered {quantity} {service_name}.\nCost: ${cost:.2f}\nAPI Order ID: {api_order_id}", color=0x00FF99)

    # -------- LIST SERVICES (owner only) --------
    @app_commands.command(name="listservices", description="List all available services (owner only)")
    async def listservices(self, interaction: discord.Interaction):
        if interaction.user.id not in OWNER_IDS:
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=False)

        payload = {"key": API_KEY, "action": "services"}
        await api_request(payload)  # Call API but do NOT show results
        await interaction.response.send_message("✅ Services refreshed.", ephemeral=False)

    # -------- APPROVE REFUND --------
    @app_commands.command(name="approve", description="Approve a refund (admin only)")
    @app_commands.describe(refund_id="Refund ID")
    async def approve(self, interaction: discord.Interaction, refund_id: int):
        if interaction.user.id not in OWNER_IDS:
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=False)
        conn = sqlite3.connect(DB)
        c = conn.cursor()
        c.execute("SELECT user_id, amount, status FROM refunds WHERE id = ?", (refund_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return await interaction.response.send_message("❌ Refund not found.", ephemeral=False)
        user_id, amount, status = row
        if status != "PENDING":
            conn.close()
            return await interaction.response.send_message("❌ Refund already processed.", ephemeral=False)
        update_balance(user_id, amount)
        c.execute("UPDATE refunds SET status = 'APPROVED' WHERE id = ?", (refund_id,))
        conn.commit()
        conn.close()
        embed = discord.Embed(title=f"Refund Approved #{refund_id}", description=f"User <@{user_id}> refunded ${amount:.2f}", color=0x00FF99)
        await log_admin(self.bot, embed)
        await interaction.response.send_message(f"✅ Refund #{refund_id} approved.", ephemeral=False)

    # -------- ADD BALANCE --------
    @app_commands.command(name="addbalance", description="Add balance to a user (admin only)")
    @app_commands.describe(user="Target User", amount="Amount to add")
    async def addbalance(self, interaction: discord.Interaction, user: discord.Member, amount: float):
        if interaction.user.id not in OWNER_IDS:
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=False)
        update_balance(user.id, amount)
        embed = discord.Embed(title="Balance Added", description=f"Added ${amount:.2f} to <@{user.id}>", color=0x00FF99)
        await log_admin(self.bot, embed)
        await interaction.response.send_message(f"✅ Added ${amount:.2f} to {user.mention}", ephemeral=False)

    # -------- REMOVE BALANCE --------
    @app_commands.command(name="removebalance", description="Remove balance from a user (admin only)")
    @app_commands.describe(user="Target User", amount="Amount to remove")
    async def removebalance(self, interaction: discord.Interaction, user: discord.Member, amount: float):
        if interaction.user.id not in OWNER_IDS:
            return await interaction.response.send_message("❌ Not allowed.", ephemeral=False)
        update_balance(user.id, -amount)
        embed = discord.Embed(title="Balance Removed", description=f"Removed ${amount:.2f} from <@{user.id}>", color=0xFF0000)
        await log_admin(self.bot, embed)
        await interaction.response.send_message(f"✅ Removed ${amount:.2f} from {user.mention}", ephemeral=False)

# ========================
# BOT SETUP
# ========================
intents = discord.Intents.default()

class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="/", intents=intents)

    async def setup_hook(self):
        await self.add_cog(SMM(self))
        await self.tree.sync()
        order_checker.start(self)

bot = MyBot()

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")

bot.run(BOT_TOKEN)
