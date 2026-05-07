import discord
from discord.ext import commands
import asyncio
import os
import logging
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("sushimusic")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True


class SushiMusic(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        from cogs.music import MusicCog
        await self.add_cog(MusicCog(self))
        await self.tree.sync()
        log.info("Slash commands synced.")

    async def on_ready(self):
        log.info(f"Logged in as {self.user} ({self.user.id})")
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="🎶 /play to start",
            )
        )


async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN is not set in .env")
    async with SushiMusic() as bot:
        await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())