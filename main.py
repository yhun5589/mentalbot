from google import genai
import os
import asyncio
import threading
import discord
from discord.ext import commands
from aiohttp import web, ClientSession
from dotenv import load_dotenv
from pathlib import Path

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(env_path)

# ================= ENV =================
API_KEY = os.getenv("API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MODEL_NAME = os.getenv("MODEL_NAME")
PORT = int(os.getenv("PORT", 10000))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

print("API_KEY:", API_KEY)
print("BOT_TOKEN:", BOT_TOKEN)
print("MODEL_NAME:", MODEL_NAME)    
print("PORT:", PORT)
print("RENDER_EXTERNAL_URL:", RENDER_EXTERNAL_URL)


client_genai = genai.Client(api_key=API_KEY)

# ================= GEMINI CALL =================
async def generate_ai_text(prompt, retries=3):
    attempt = 0
    while attempt < retries:
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client_genai.models.generate_content(
                    model=MODEL_NAME,
                    contents=prompt
                )
            )
            return response.text

        except genai.errors.ClientError as e:
            if e.status_code == 429:
                retry_after = 5
                if e.response_json:
                    for detail in e.response_json.get("details", []):
                        if detail.get("@type", "").endswith("RetryInfo"):
                            retry_after = float(
                                detail.get("retryDelay", "5s").replace("s", "")
                            )
                await asyncio.sleep(retry_after)
                attempt += 1
            else:
                raise

    raise Exception("Gemini quota retries exhausted")

# ================= DISCORD HELPERS =================
async def send_long_message(ctx, text, limit=2000):
    for i in range(0, len(text), limit):
        await ctx.send(text[i:i + limit])

# ================= BOT CLASS =================
class MyBot(commands.Bot):
    async def setup_hook(self):
        # start self-ping task safely
        self.loop.create_task(self.self_ping())

    async def self_ping(self):
        if not RENDER_EXTERNAL_URL:
            return

        async with ClientSession() as session:
            while not self.is_closed():
                try:
                    await session.get(RENDER_EXTERNAL_URL)
                except:
                    pass
                await asyncio.sleep(600)  # 10 minutes

# ================= DISCORD BOT =================
intents = discord.Intents.default()
intents.message_content = True

bot = MyBot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Bot online as {bot.user}")

@bot.command(name="stress")
async def stress(ctx, *, user_prompt: str):
    prompt = (
        "You are a supportive mental health assistant specializing in stress and anxiety. "
        "Respond empathetically, give practical advice, and avoid giving medical prescriptions.\n\n"
        f"User: {user_prompt}"
    )
    try:
        response = await generate_ai_text(prompt)
        await send_long_message(ctx, response)
    except Exception as e:
        await ctx.send(f"Error: {e}")

# ================= RENDER WEB SERVER =================
async def handle(request):
    return web.Response(text="Bot is alive.")

def run_web():
    app = web.Application()
    app.router.add_get("/", handle)
    web.run_app(app, host="0.0.0.0", port=PORT)

# ================= START =================
threading.Thread(target=run_web, daemon=True).start()
bot.run(BOT_TOKEN)
