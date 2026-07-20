import os
import asyncio
import datetime
import discord
from discord.ext import commands
from aiohttp import web, ClientSession
from google import genai
import yt_dlp
from dotenv import load_dotenv  # <-- ADD THIS

# ================= ENV & CONFIG =================

load_dotenv()  # <-- ADD THIS

API_KEY = os.getenv("API_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MODEL_NAME = os.getenv("MODEL_NAME", "gemini-2.5-flash")
PORT = int(os.getenv("PORT", 10000))
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")

# Initialize Gemini Client
client_genai = genai.Client(api_key=API_KEY)

# FFmpeg & YTDL Configuration
# Set noplaylist to False so YouTube playlist URLs (e.g. ?list=...) work
YTDL_OPTIONS = {
    'format': 'bestaudio/best',
    'noplaylist': False,
    'quiet': True,
    'default_search': 'auto',
    'source_address': '0.0.0.0',
    'extract_flat': 'in_playlist'
}
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'
}
ytdl = yt_dlp.YoutubeDL(YTDL_OPTIONS)

# Music Queues Storage: { guild_id: [ {'url': str, 'title': str}, ... ] }
queues = {}

# ================= AI GENERATION (WITH REAL-TIME SEARCH) =================
async def ask_gemini(prompt: str, retries: int = 3) -> str:
    attempt = 0
    while attempt < retries:
        try:
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None,
                lambda: client_genai.models.generate_content(
                    model=MODEL_NAME,
                    contents=prompt,
                    config={"tools": [{"google_search": {}}]}
                )
            )
            return response.text if response.text else "No response generated."
        except genai.errors.ClientError as e:
            if e.status_code == 429:
                await asyncio.sleep(5)
                attempt += 1
            else:
                raise e
    raise Exception("Gemini quota retries exhausted.")

# ================= BOT INITIALIZATION =================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class MultiBot(commands.Bot):
    async def setup_hook(self):
        self.loop.create_task(self.self_ping())

    async def self_ping(self):
        if not RENDER_EXTERNAL_URL:
            return
        async with ClientSession() as session:
            while not self.is_closed():
                try:
                    await session.get(RENDER_EXTERNAL_URL)
                except Exception:
                    pass
                await asyncio.sleep(600)

bot = MultiBot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Bot active as {bot.user}")

# ================= GENERAL Q&A =================
@bot.command(name="ask")
async def ask(ctx, *, query: str):
    """Ask anything. Uses Gemini with web search capabilities."""
    async with ctx.typing():
        try:
            answer = await ask_gemini(query)
            for i in range(0, len(answer), 2000):
                await ctx.send(answer[i:i + 2000])
        except Exception as e:
            await ctx.send(f"⚠️ Error: {e}")

# ================= MODERATION COMMANDS =================
@bot.command(name="kick")
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    try:
        await member.kick(reason=reason)
        await ctx.send(f"✅ Kicked **{member.display_name}** | Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("❌ I lack permissions to kick this user.")

@bot.command(name="ban")
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, reason: str = "No reason provided"):
    try:
        await member.ban(reason=reason)
        await ctx.send(f"🚨 Banned **{member.display_name}** | Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("❌ I lack permissions to ban this user.")

@bot.command(name="unban")
@commands.has_permissions(ban_members=True)
async def unban(ctx, *, user_input: str):
    banned_users = [entry async for entry in ctx.guild.bans()]
    for ban_entry in banned_users:
        user = ban_entry.user
        if user_input in (user.name, str(user.id)):
            await ctx.guild.unban(user)
            await ctx.send(f"🔓 Unbanned **{user.name}**")
            return
    await ctx.send("❌ User not found in ban list.")

@bot.command(name="mute")
@commands.has_permissions(moderate_members=True)
async def mute(ctx, member: discord.Member, minutes: int = 10, *, reason: str = "No reason provided"):
    try:
        duration = datetime.timedelta(minutes=minutes)
        await member.timeout(duration, reason=reason)
        await ctx.send(f"🔇 Muted **{member.display_name}** for {minutes}m | Reason: {reason}")
    except discord.Forbidden:
        await ctx.send("❌ I lack permissions to timeout this user.")

# ================= MUSIC & PLAYLIST ENGINE =================
async def play_next(ctx):
    """Plays the next song in the server's queue."""
    guild_id = ctx.guild.id
    if guild_id in queues and len(queues[guild_id]) > 0:
        song = queues[guild_id].pop(0)

        # Resolve full audio stream URL
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(
            None, lambda: yt_dlp.YoutubeDL({'format': 'bestaudio/best', 'quiet': True}).extract_info(song['search_url'], download=False)
        )
        stream_url = data['url']

        source = discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTIONS)
        ctx.voice_client.play(
            source,
            after=lambda e: asyncio.run_coroutine_threadsafe(play_next(ctx), bot.loop)
        )
        await ctx.send(f"🎶 Now playing: **{song['title']}**")
    else:
        await ctx.send("✅ Queue finished. Disconnecting.")
        if ctx.voice_client:
            await ctx.voice_client.disconnect()

@bot.command(name="play")
async def play(ctx, *, search: str):
    """Plays a song or adds a full playlist/search query to the queue."""
    if not ctx.author.voice:
        return await ctx.send("❌ Join a voice channel first!")

    channel = ctx.author.voice.channel
    voice_client = ctx.voice_client

    if not voice_client:
        voice_client = await channel.connect()
    elif voice_client.channel != channel:
        await voice_client.move_to(channel)

    if ctx.guild.id not in queues:
        queues[ctx.guild.id] = []

    async with ctx.typing():
        loop = asyncio.get_running_loop()
        is_url = search.startswith("http://") or search.startswith("https://")
        query = search if is_url else f"ytsearch:{search}"

        data = await loop.run_in_executor(
            None, lambda: ytdl.extract_info(query, download=False)
        )

        if 'entries' in data:
            # Handle playlist or search list
            added_count = 0
            for entry in data['entries']:
                if entry:
                    title = entry.get('title', 'Unknown Track')
                    url = entry.get('url') or entry.get('webpage_url')
                    queues[ctx.guild.id].append({'title': title, 'search_url': url})
                    added_count += 1
            await ctx.send(f"➕ Added **{added_count} tracks** to the queue!")
        else:
            # Handle single track
            title = data.get('title', 'Unknown Track')
            url = data.get('webpage_url') or search
            queues[ctx.guild.id].append({'title': title, 'search_url': url})
            await ctx.send(f"➕ Added to queue: **{title}**")

    if not voice_client.is_playing() and not voice_client.is_paused():
        await play_next(ctx)

@bot.command(name="skip")
async def skip(ctx):
    """Skips the currently playing track."""
    if ctx.voice_client and ctx.voice_client.is_playing():
        ctx.voice_client.stop()
        await ctx.send("⏭️ Skipped.")

@bot.command(name="queue")
async def show_queue(ctx):
    """Displays up to the next 10 items in the queue."""
    guild_queue = queues.get(ctx.guild.id, [])
    if not guild_queue:
        return await ctx.send("📋 Queue is empty.")

    description = ""
    for idx, song in enumerate(guild_queue[:10], start=1):
        description += f"**{idx}.** {song['title']}\n"

    if len(guild_queue) > 10:
        description += f"\n*...and {len(guild_queue) - 10} more tracks.*"

    embed = discord.Embed(title="Current Playlist Queue", description=description, color=discord.Color.blue())
    await ctx.send(embed=embed)

@bot.command(name="clear")
async def clear_queue(ctx):
    """Clears the queue."""
    queues[ctx.guild.id] = []
    await ctx.send("🗑️ Queue cleared.")

@bot.command(name="stop")
async def stop(ctx):
    """Stops playback and clears the queue."""
    queues[ctx.guild.id] = []
    if ctx.voice_client:
        ctx.voice_client.stop()
        await ctx.send("⏹️ Stopped playback and cleared queue.")

@bot.command(name="leave")
async def leave(ctx):
    """Disconnects the bot from voice."""
    queues[ctx.guild.id] = []
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("👋 Disconnected.")

# ================= WEB SERVER & MAIN =================
async def handle_ping(request):
    return web.Response(text="OK")

async def start_web():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server active on port {PORT}")

async def main():
    await start_web()
    await bot.start(BOT_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
