import asyncio
import logging
import discord
from discord.ext import commands
from discord import app_commands
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone
import ytdl as ytdl_helper
import analytics as analytics_db

log = logging.getLogger("sushimusic.music")

EMBED_COLOR_YT    = 0xFF0000   # red   — YouTube
EMBED_COLOR_QUEUE = 0x2B2D31  # dark  — queue list


# ─── Track dataclass ──────────────────────────────────────────────────────────
@dataclass
class Track:
    title: str
    artist: str
    album: str
    duration: int          # seconds
    thumbnail: Optional[str] = None
    webpage_url: Optional[str] = None

    @property
    def duration_fmt(self) -> str:
        m, s = divmod(self.duration, 60)
        return f"{m}:{s:02d}"


# ─── Per-guild state ──────────────────────────────────────────────────────────
class GuildPlayer:
    def __init__(self):
        self.queue: list[tuple[Track, str, int]] = []  # (Track, stream_url, user_id)
        self.current: tuple[Track, str] | None = None
        self.current_user_id: int = 0                  # who queued the current track
        self.vc: discord.VoiceClient | None = None
        self.now_playing_msg: discord.Message | None = None
        self.loop: bool = False


# ─── NowPlaying embed controls ────────────────────────────────────────────────
class NowPlayingView(discord.ui.View):
    def __init__(self, cog: "MusicCog", guild_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id

    async def _safe_respond(self, interaction: discord.Interaction, msg: str):
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            log.warning(f"Button response failed: {e}")

    @discord.ui.button(emoji="⏸", style=discord.ButtonStyle.secondary)
    async def pause_btn(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        state = self.cog.get_state(self.guild_id)
        if state.vc and state.vc.is_playing():
            state.vc.pause()
            await self._safe_respond(interaction, "⏸ Paused.")
        elif state.vc and state.vc.is_paused():
            state.vc.resume()
            await self._safe_respond(interaction, "▶️ Resumed.")
        else:
            await self._safe_respond(interaction, "Nothing is playing.")

    @discord.ui.button(emoji="⏭", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        state = self.cog.get_state(self.guild_id)
        if state.vc and (state.vc.is_playing() or state.vc.is_paused()):
            state.vc.stop()
            await self._safe_respond(interaction, "⏭ Skipped.")
        else:
            await self._safe_respond(interaction, "Nothing to skip.")

    @discord.ui.button(emoji="⏹", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, _):
        await interaction.response.defer()
        state = self.cog.get_state(self.guild_id)
        state.queue.clear()
        if state.vc:
            state.vc.stop()
            await state.vc.disconnect()
            state.vc = None
        await self._safe_respond(interaction, "⏹ Stopped and cleared queue.")


# ─── Search result picker ─────────────────────────────────────────────────────
class SearchSelect(discord.ui.Select):
    def __init__(self, results: list[dict], cog: "MusicCog", channel: discord.TextChannel):
        self.results = results
        self.cog = cog
        self.channel = channel
        options = [
            discord.SelectOption(
                label=f"{r['title'][:80]}",
                description=f"{r['artist']} [{_fmt_dur(r['duration'])}]",
                value=str(i),
            )
            for i, r in enumerate(results[:8])
        ]
        super().__init__(placeholder="Choose a track…", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        r = self.results[int(self.values[0])]

        stream_url, _ = await ytdl_helper.stream_url_for_query(r["webpage_url"])
        if not stream_url:
            await interaction.followup.send("❌ Could not get stream URL.", ephemeral=True)
            return

        track = Track(
            title=r["title"],
            artist=r["artist"],
            album=r["album"],
            duration=r["duration"],
            thumbnail=r.get("thumbnail"),
            webpage_url=r.get("webpage_url"),
        )

        state = self.cog.get_state(interaction.guild_id)
        state.queue.append((track, stream_url, interaction.user.id))
        await interaction.followup.send(embed=self.cog._added_embed(track), ephemeral=True)

        if not (state.vc and (state.vc.is_playing() or state.vc.is_paused())):
            if interaction.user.voice:
                await self.cog.start_playback(
                    interaction.guild_id,
                    interaction.user.voice.channel,
                    self.channel,
                )


class SearchView(discord.ui.View):
    def __init__(self, results: list[dict], cog: "MusicCog", channel: discord.TextChannel):
        super().__init__(timeout=60)
        self.add_item(SearchSelect(results, cog, channel))


# ─── Music Cog ────────────────────────────────────────────────────────────────
class MusicCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._states: dict[int, GuildPlayer] = {}

    async def cog_load(self):
        await analytics_db.db.init()

    async def cog_unload(self):
        await analytics_db.db.close()

    def get_state(self, guild_id: int) -> GuildPlayer:
        if guild_id not in self._states:
            self._states[guild_id] = GuildPlayer()
        return self._states[guild_id]

    # ── /play ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="play", description="Play a song from YouTube Music")
    @app_commands.describe(query="Song name, artist, URL, or YouTube playlist")
    async def play(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice:
            await interaction.response.send_message(
                "❌ Join a voice channel first.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        is_url = query.startswith("http://") or query.startswith("https://")

        # ── Playlist / Radio ───────────────────────────────────────────────────
        if is_url and ytdl_helper._is_playlist_url(query):
            is_radio = ytdl_helper._is_radio_url(query)
            label = "Radio mix" if is_radio else "Playlist"
            await interaction.followup.send(f"📻 Loading {label}…")

            pl_tracks = await ytdl_helper.get_playlist_tracks(query, limit=25)
            if not pl_tracks:
                await interaction.channel.send("❌ Could not load playlist/radio.")
                return

            state = self.get_state(interaction.guild_id)
            for pt in pl_tracks:
                track = Track(
                    title=pt["title"],
                    artist=pt["artist"],
                    album=pt["album"],
                    duration=pt["duration"],
                    webpage_url=pt["webpage_url"],
                )
                # Queue with the page URL; stream URL is resolved at play time
                state.queue.append((track, pt["webpage_url"], interaction.user.id))

            await interaction.channel.send(
                f"➕ Added **{len(pl_tracks)}** tracks from {label} to the queue."
            )

            if not (state.vc and (state.vc.is_playing() or state.vc.is_paused())):
                await self.start_playback(
                    interaction.guild_id,
                    interaction.user.voice.channel,
                    interaction.channel,
                )
            return

        # ── Direct URL (single video) ──────────────────────────────────────────
        if is_url:
            stream_url, info = await ytdl_helper.stream_url_for_query(query)
            if not stream_url or not info:
                await interaction.followup.send("❌ Could not get a stream URL from YouTube.")
                return

            track = Track(
                title=ytdl_helper._clean_title(info.get("track") or info.get("title", "Unknown")),
                artist=info.get("artist") or info.get("uploader") or "YouTube",
                album=info.get("album") or "YouTube",
                duration=int(info.get("duration") or 0),
                thumbnail=info.get("thumbnail"),
                webpage_url=info.get("webpage_url") or query,
            )

            state = self.get_state(interaction.guild_id)
            state.queue.append((track, stream_url, interaction.user.id))
            await interaction.followup.send(embed=self._added_embed(track))

            if not (state.vc and (state.vc.is_playing() or state.vc.is_paused())):
                await self.start_playback(
                    interaction.guild_id,
                    interaction.user.voice.channel,
                    interaction.channel,
                )
            return

        # ── Text search ────────────────────────────────────────────────────────
        results = await ytdl_helper.search_tracks(query, limit=5)

        if not results:
            await interaction.followup.send("❌ No results found on YouTube.")
            return

        # Multiple results → show picker
        if len(results) > 1:
            embed = discord.Embed(
                title="🎵 Search Results",
                description=f"Found **{len(results)}** results for `{query}` — pick one:",
                color=EMBED_COLOR_YT,
            )
            view = SearchView(results, self, interaction.channel)
            await interaction.followup.send(embed=embed, view=view)
            return

        # Single result → stream immediately
        r = results[0]
        stream_url, _ = await ytdl_helper.stream_url_for_query(r["webpage_url"])
        if not stream_url:
            await interaction.followup.send("❌ Could not get a stream URL from YouTube.")
            return

        track = Track(
            title=r["title"],
            artist=r["artist"],
            album=r["album"],
            duration=r["duration"],
            thumbnail=r.get("thumbnail"),
            webpage_url=r.get("webpage_url"),
        )

        state = self.get_state(interaction.guild_id)
        state.queue.append((track, stream_url, interaction.user.id))
        await interaction.followup.send(embed=self._added_embed(track))

        if not (state.vc and (state.vc.is_playing() or state.vc.is_paused())):
            await self.start_playback(
                interaction.guild_id,
                interaction.user.voice.channel,
                interaction.channel,
            )

    # ── /queue ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="queue", description="Show the current queue")
    async def queue_cmd(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        embed = discord.Embed(title="📋 Queue", color=EMBED_COLOR_QUEUE)

        if state.current:
            t, _ = state.current
            embed.add_field(
                name="▶️ Now Playing",
                value=f"**{t.title}** — {t.artist} `[{t.duration_fmt}]`",
                inline=False,
            )

        if state.queue:
            lines = []
            for i, (t, _, _uid) in enumerate(state.queue[:10], 1):
                lines.append(f"`{i}.` **{t.title}** — {t.artist} `[{t.duration_fmt}]`")
            embed.add_field(name="Up Next", value="\n".join(lines), inline=False)
            if len(state.queue) > 10:
                embed.set_footer(text=f"+ {len(state.queue) - 10} more tracks")
        else:
            embed.add_field(name="Up Next", value="Queue is empty.", inline=False)

        await interaction.response.send_message(embed=embed)

    # ── /skip ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="skip", description="Skip the current track")
    async def skip(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.vc and (state.vc.is_playing() or state.vc.is_paused()):
            state.vc.stop()
            await interaction.response.send_message("⏭ Skipped.")
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    # ── /pause ─────────────────────────────────────────────────────────────────
    @app_commands.command(name="pause", description="Pause or resume playback")
    async def pause(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if state.vc and state.vc.is_playing():
            state.vc.pause()
            await interaction.response.send_message("⏸ Paused.")
        elif state.vc and state.vc.is_paused():
            state.vc.resume()
            await interaction.response.send_message("▶️ Resumed.")
        else:
            await interaction.response.send_message("Nothing is playing.", ephemeral=True)

    # ── /stop ──────────────────────────────────────────────────────────────────
    @app_commands.command(name="stop", description="Stop and clear the queue")
    async def stop(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        state.queue.clear()
        if state.vc:
            state.vc.stop()
            await state.vc.disconnect()
            state.vc = None
        await interaction.response.send_message("⏹ Stopped.")

    # ── /nowplaying ────────────────────────────────────────────────────────────
    @app_commands.command(name="nowplaying", description="Show what's currently playing")
    async def nowplaying(self, interaction: discord.Interaction):
        state = self.get_state(interaction.guild_id)
        if not state.current:
            await interaction.response.send_message(
                "Nothing is playing right now.", ephemeral=True
            )
            return
        track, _ = state.current
        embed = self._now_playing_embed(track, interaction.guild_id)
        view = NowPlayingView(self, interaction.guild_id)
        await interaction.response.send_message(embed=embed, view=view)

    # ── /wrapped ───────────────────────────────────────────────────────────────
    @app_commands.command(name="wrapped", description="Your Sushi Music Wrapped 🎶")
    @app_commands.describe(year="Year to show stats for (default: current year)")
    async def wrapped(self, interaction: discord.Interaction, year: Optional[int] = None):
        await interaction.response.defer(thinking=True)
        year = year or datetime.now(timezone.utc).year
        stats = await analytics_db.db.get_wrapped(
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            year=year,
        )

        if stats.total_plays == 0:
            await interaction.followup.send(
                f"📭 No listening data found for **{year}**. "
                "Start playing some songs and come back!",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🍣 {interaction.user.display_name}'s Wrapped {year}",
            color=0xFF6B6B,
        )
        embed.set_thumbnail(url=interaction.user.display_avatar.url)

        embed.add_field(
            name="🎵 Total Plays",
            value=f"**{stats.total_plays:,}** songs",
            inline=True,
        )
        embed.add_field(
            name="⏱️ Time Listened",
            value=f"**{stats.total_minutes:,}** minutes\n_{stats.total_minutes // 60}h {stats.total_minutes % 60}m_",
            inline=True,
        )

        if stats.peak_hour is not None:
            ampm = f"{stats.peak_hour % 12 or 12}{'am' if stats.peak_hour < 12 else 'pm'}"
            embed.add_field(
                name="🕐 Peak Hour",
                value=f"**{ampm}**",
                inline=True,
            )

        if stats.peak_day:
            embed.add_field(name="📅 Most Active Day", value=f"**{stats.peak_day}**", inline=True)

        if stats.top_tracks:
            lines = [
                f"`{i}.` **{t}** — {a} ×{c}"
                for i, (t, a, c) in enumerate(stats.top_tracks, 1)
            ]
            embed.add_field(name="🔥 Top Tracks", value="\n".join(lines), inline=False)

        if stats.top_artists:
            lines = [
                f"`{i}.` **{a}** — {c} plays · {m}m"
                for i, (a, c, m) in enumerate(stats.top_artists, 1)
            ]
            embed.add_field(name="🎤 Top Artists", value="\n".join(lines), inline=False)

        if stats.first_song:
            embed.add_field(
                name="🌅 First Song of the Year",
                value=f"**{stats.first_song['title']}** — {stats.first_song['artist']}",
                inline=False,
            )

        embed.set_footer(text="🍣 Sushi Music Wrapped — your year in music")
        await interaction.followup.send(embed=embed)

    # ── /serverwrapped ─────────────────────────────────────────────────────────
    @app_commands.command(name="serverwrapped", description="This server's Sushi Music Wrapped 🍣")
    @app_commands.describe(year="Year to show stats for (default: current year)")
    async def server_wrapped(self, interaction: discord.Interaction, year: Optional[int] = None):
        await interaction.response.defer(thinking=True)
        year = year or datetime.now(timezone.utc).year
        data = await analytics_db.db.get_guild_wrapped(
            guild_id=interaction.guild_id,
            year=year,
        )

        if data["total_plays"] == 0:
            await interaction.followup.send(
                f"📭 No server listening data for **{year}** yet.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title=f"🍣 {interaction.guild.name}'s Server Wrapped {year}",
            color=0xFFB347,
        )
        if interaction.guild.icon:
            embed.set_thumbnail(url=interaction.guild.icon.url)

        mins = data["total_minutes"]
        embed.add_field(name="🎵 Total Plays", value=f"**{data['total_plays']:,}**", inline=True)
        embed.add_field(
            name="⏱️ Time Listened",
            value=f"**{mins:,}** min _{mins // 60}h {mins % 60}m_",
            inline=True,
        )

        if data.get("top_tracks"):
            lines = [
                f"`{i}.` **{t}** — {a} ×{c}"
                for i, (t, a, c) in enumerate(data["top_tracks"], 1)
            ]
            embed.add_field(name="🔥 Server Top Tracks", value="\n".join(lines), inline=False)

        if data.get("top_artists"):
            lines = [
                f"`{i}.` **{a}** — {c} plays"
                for i, (a, c) in enumerate(data["top_artists"], 1)
            ]
            embed.add_field(name="🎤 Server Top Artists", value="\n".join(lines), inline=False)

        if data.get("top_listeners"):
            lines = []
            for i, (uid, cnt) in enumerate(data["top_listeners"], 1):
                member = interaction.guild.get_member(int(uid))
                name = member.display_name if member else f"<@{uid}>"
                lines.append(f"`{i}.` {name} — {cnt} plays")
            embed.add_field(name="👑 Top Listeners", value="\n".join(lines), inline=False)

        embed.set_footer(text="🍣 Sushi Music Wrapped — the server's year in music")
        await interaction.followup.send(embed=embed)

    # ─── Internal playback ─────────────────────────────────────────────────────
    async def start_playback(
        self, guild_id: int, voice_channel, text_channel
    ) -> bool:
        state = self.get_state(guild_id)

        if not state.queue:
            return False

        if not state.vc or not state.vc.is_connected():
            try:
                state.vc = await asyncio.wait_for(
                    voice_channel.connect(self_deaf=True), timeout=15.0
                )
            except asyncio.TimeoutError:
                log.error(f"Voice connect timed out for guild {guild_id}")
                await text_channel.send(
                    "❌ Timed out connecting to voice. "
                    "Make sure the bot has UDP outbound access (ports 50000–65535)."
                )
                return False
            except discord.ClientException as e:
                log.error(f"Voice connect ClientException: {e}")
                await text_channel.send(f"❌ Could not connect to voice: {e}")
                return False

        await self._play_next(guild_id, text_channel)
        return True

    async def _play_next(self, guild_id: int, text_channel):
        state = self.get_state(guild_id)

        if not state.queue:
            if state.vc:
                await state.vc.disconnect()
                state.vc = None
            return

        track, url, user_id = state.queue.pop(0)
        state.current_user_id = user_id

        # If URL is a YouTube page URL (from playlist), resolve stream URL now
        # Check for watch/short page URLs only — not already-resolved stream URLs
        if "youtube.com/watch" in url or "youtu.be/" in url or "youtube.com/shorts" in url:
            resolved, _ = await ytdl_helper.stream_url_for_query(url)
            if not resolved:
                log.warning(f"Could not resolve stream for: {track.title} — skipping")
                await text_channel.send(f"⚠️ Skipped **{track.title}** (could not get stream).")
                await self._play_next(guild_id, text_channel)
                return
            url = resolved

        state.current = (track, url)

        # Record play for Wrapped analytics
        try:
            await analytics_db.db.record_play(
                guild_id=guild_id,
                user_id=user_id,
                title=track.title,
                artist=track.artist,
                album=track.album,
                duration=track.duration,
                source_url=track.webpage_url or "",
            )
        except Exception as e:
            log.warning(f"Analytics record failed (non-fatal): {e}")

        # ── Hi-Res Audio Enhancement Chain ────────────────────────────────────
        # Stage 1 — Upsample to 96 kHz with high-quality Soxr resampler
        #            Gives FFmpeg more headroom to process filters accurately
        # Stage 2 — Full-range EQ (sub-bass, bass, low-mid, presence, air)
        # Stage 3 — Harmonic exciter: blend a tiny bit of 2nd-order harmonic
        #            distortion to restore "warmth" lost in lossy compression
        # Stage 4 — Stereo widening for immersive headphone listening
        # Stage 5 — Soft-knee dynamic compression — controls peaks, lifts quiets
        # Stage 6 — EBU R128 loudness normalisation — consistent track volume
        # Stage 7 — Downsample back to 48 kHz (Discord's native rate)
        _AF = (
            # — Upsample —
            "aresample=96000:resampler=soxr:precision=33:dither_method=triangular,"
            # — Full-range EQ —
            "equalizer=f=40:t=q:w=0.6:g=3,"      # sub-bass warmth
            "equalizer=f=100:t=q:w=1.0:g=4,"     # bass punch
            "equalizer=f=250:t=q:w=1.2:g=-1,"    # reduce muddiness
            "equalizer=f=3000:t=q:w=1.5:g=1.5,"  # vocal clarity
            "equalizer=f=8000:t=q:w=1.2:g=2.5,"  # presence / hi-hats
            "equalizer=f=16000:t=q:w=0.8:g=2,"   # air / brilliance
            # — Harmonic exciter (subtle warmth) —
            "aexciter=level_in=1:level_out=1:amount=0.4:drive=6.0:blend=4:freq=4000,"
            # — Stereo widening —
            "stereowiden=delay=20:feedback=0.15:crossfeed=0.25:drymix=1.0,"
            # — Soft dynamic compression —
            "acompressor=threshold=0.1:ratio=3.5:knee=6:attack=8:release=80:makeup=1.5,"
            # — Loudness normalisation —
            "loudnorm=I=-14:TP=-1:LRA=9,"
            # — Downsample to Discord native rate —
            "aresample=48000:resampler=soxr:precision=28"
        )
        ffmpeg_opts = {
            "before_options": (
                "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
            ),
            "options": f"-vn -af {_AF}",
        }
        source = discord.FFmpegPCMAudio(url, **ffmpeg_opts)
        source = discord.PCMVolumeTransformer(source, volume=0.85)

        def after(err):
            if err:
                log.error(f"Playback error: {err}")
            asyncio.run_coroutine_threadsafe(
                self._play_next(guild_id, text_channel), self.bot.loop
            )

        state.vc.play(source, after=after)

        # Now playing embed
        embed = self._now_playing_embed(track, guild_id)
        view = NowPlayingView(self, guild_id)
        msg = await text_channel.send(embed=embed, view=view)

        if state.now_playing_msg:
            try:
                await state.now_playing_msg.delete()
            except Exception:
                pass
        state.now_playing_msg = msg

    # ─── Embeds ────────────────────────────────────────────────────────────────
    def _now_playing_embed(self, track: Track, guild_id: int) -> discord.Embed:
        state = self.get_state(guild_id)
        embed = discord.Embed(
            title="🎵 Now Playing",
            description=f"## {track.title}",
            color=EMBED_COLOR_YT,
        )
        embed.add_field(name="Artist", value=track.artist, inline=True)
        embed.add_field(name="Album", value=track.album, inline=True)
        embed.add_field(
            name="Duration",
            value=track.duration_fmt if track.duration else "—",
            inline=True,
        )
        embed.add_field(
            name="Up Next",
            value=f"{len(state.queue)} track(s) in queue",
            inline=True,
        )
        if track.webpage_url:
            embed.add_field(name="Link", value=f"[YouTube]({track.webpage_url})", inline=True)
        embed.set_footer(text="⏸ Pause/Resume  ⏭ Skip  ⏹ Stop")
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        return embed

    def _added_embed(self, track: Track) -> discord.Embed:
        embed = discord.Embed(
            title="➕ Added to Queue",
            description=f"**{track.title}**",
            color=EMBED_COLOR_YT,
        )
        embed.add_field(name="Artist", value=track.artist, inline=True)
        embed.add_field(
            name="Duration",
            value=track.duration_fmt if track.duration else "—",
            inline=True,
        )
        if track.thumbnail:
            embed.set_thumbnail(url=track.thumbnail)
        return embed


# ─── Helpers ──────────────────────────────────────────────────────────────────
def _fmt_dur(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    return f"{m}:{s:02d}"