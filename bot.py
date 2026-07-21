"""SREL Discord onboarding bot.

Flow:
    1. Member joins → auto @unverified.
    2. Persistent buttons in #welcome:
       - "I'm a club member" → callsign+email modal → email code → grant @member.
       - "I'm just visiting"  → immediate @guest.
    3. Verification checks: shape → dedup (callsign/email) → roster match →
       email code (6 digits, 15 min TTL, single use).

Configuration is entirely via environment variables. See .env.example.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
from dataclasses import dataclass

import discord
from discord import app_commands
from dotenv import load_dotenv

from mailer import send_code
from roster import fetch_roster
from store import Store

log = logging.getLogger("srel")

# Worldwide amateur callsign shape check. Accepts KD8AAA, KD8BBB, W8AB, K8E, etc.
CALLSIGN_RE = re.compile(r"^[A-Z0-9]{1,3}\d[A-Z]{1,4}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# --------------------------------------------------------------------- config

@dataclass
class Config:
    token: str
    guild_id: int
    welcome_channel_id: int
    review_channel_id: int
    unverified_role_id: int
    member_role_id: int
    guest_role_id: int
    officer_role_id: int
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    from_email: str
    from_name: str
    sheet_id: str
    sa_json: str
    roster_sync_min: int
    db_path: str
    code_ttl_min: int
    code_rate_limit: int

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        g = os.environ.get
        required = [
            "DISCORD_TOKEN", "GUILD_ID", "WELCOME_CHANNEL_ID", "REVIEW_CHANNEL_ID",
            "UNVERIFIED_ROLE_ID", "MEMBER_ROLE_ID", "GUEST_ROLE_ID", "OFFICER_ROLE_ID",
            "SMTP_USER", "SMTP_PASSWORD", "FROM_EMAIL", "GOOGLE_SHEET_ID",
        ]
        missing = [k for k in required if not g(k)]
        if missing:
            raise SystemExit(f"missing required env vars: {', '.join(missing)}")
        try:
            return cls._build(g)
        except ValueError as e:
            raise SystemExit(f"env var must be a number: {e}")

    @classmethod
    def _build(cls, g) -> "Config":
        return cls(
            token=g("DISCORD_TOKEN"),
            guild_id=int(g("GUILD_ID")),
            welcome_channel_id=int(g("WELCOME_CHANNEL_ID")),
            review_channel_id=int(g("REVIEW_CHANNEL_ID")),
            unverified_role_id=int(g("UNVERIFIED_ROLE_ID")),
            member_role_id=int(g("MEMBER_ROLE_ID")),
            guest_role_id=int(g("GUEST_ROLE_ID")),
            officer_role_id=int(g("OFFICER_ROLE_ID")),
            smtp_host=g("SMTP_HOST", "smtp.fastmail.com"),
            smtp_port=int(g("SMTP_PORT", "465")),
            smtp_user=g("SMTP_USER"),
            smtp_password=g("SMTP_PASSWORD"),
            from_email=g("FROM_EMAIL"),
            from_name=g("FROM_NAME", "SREL Bot"),
            sheet_id=g("GOOGLE_SHEET_ID"),
            sa_json=g("GOOGLE_SA_JSON", "./service-account.json"),
            roster_sync_min=int(g("ROSTER_SYNC_INTERVAL_MIN", "60")),
            db_path=g("DB_PATH", "./bot.db"),
            code_ttl_min=int(g("CODE_TTL_MIN", "15")),
            code_rate_limit=int(g("CODE_RATE_LIMIT_PER_HOUR", "3")),
        )


# ---------------------------------------------------------------------- views

class WelcomeView(discord.ui.View):
    def __init__(self, bot: "SrelBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="I'm a club member",
        style=discord.ButtonStyle.primary,
        custom_id="srel:member",
    )
    async def member_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(MemberModal(self.bot))

    @discord.ui.button(
        label="I'm just visiting",
        style=discord.ButtonStyle.secondary,
        custom_id="srel:guest",
    )
    async def guest_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self.bot.grant_guest(interaction)


class MemberModal(discord.ui.Modal, title="Club member verification"):
    callsign_field = discord.ui.TextInput(
        label="Your callsign",
        placeholder="e.g. KD8AAA",
        max_length=10,
        required=True,
    )
    email_field = discord.ui.TextInput(
        label="Email on file with the club",
        placeholder="the address the club has for you",
        max_length=254,
        required=True,
    )

    def __init__(self, bot: "SrelBot"):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await self.bot.handle_member_submit(
            interaction,
            str(self.callsign_field.value),
            str(self.email_field.value),
        )


class EnterCodeView(discord.ui.View):
    def __init__(self, bot: "SrelBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(
        label="Enter code",
        style=discord.ButtonStyle.success,
        custom_id="srel:entercode",
    )
    async def enter_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(CodeModal(self.bot))


class CodeModal(discord.ui.Modal, title="Enter your verification code"):
    code_field = discord.ui.TextInput(
        label="6-digit code from the email",
        min_length=6,
        max_length=6,
        required=True,
    )

    def __init__(self, bot: "SrelBot"):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        await self.bot.handle_code_submit(interaction, str(self.code_field.value).strip())


# --- Review queue buttons (persistent, per-case via DynamicItem) ---


class ReviewApprove(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"srel:review:approve:(?P<qid>\d+)",
):
    def __init__(self, qid: int):
        self.qid = qid
        super().__init__(
            discord.ui.Button(
                label="Approve",
                style=discord.ButtonStyle.success,
                custom_id=f"srel:review:approve:{qid}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["qid"]))

    async def callback(self, interaction: discord.Interaction):
        bot: SrelBot = interaction.client  # type: ignore[assignment]
        await bot.handle_review_decision(interaction, self.qid, "approve")


class ReviewDeny(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"srel:review:deny:(?P<qid>\d+)",
):
    def __init__(self, qid: int):
        self.qid = qid
        super().__init__(
            discord.ui.Button(
                label="Deny",
                style=discord.ButtonStyle.danger,
                custom_id=f"srel:review:deny:{qid}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(int(match["qid"]))

    async def callback(self, interaction: discord.Interaction):
        bot: SrelBot = interaction.client  # type: ignore[assignment]
        await bot.handle_review_decision(interaction, self.qid, "deny")


def _review_view(qid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    v.add_item(ReviewApprove(qid))
    v.add_item(ReviewDeny(qid))
    return v


# ------------------------------------------------------------------------ bot

class SrelBot(discord.Client):
    def __init__(self, cfg: Config):
        intents = discord.Intents.default()
        intents.members = True  # for on_member_join
        super().__init__(intents=intents)
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(WelcomeView(self))
        self.add_view(EnterCodeView(self))
        self.add_dynamic_items(ReviewApprove, ReviewDeny)
        guild = discord.Object(id=self.cfg.guild_id)
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)
        asyncio.create_task(self._roster_sync_loop())

    async def on_ready(self):
        log.info("logged in as %s (id=%s)", self.user, self.user.id)

    # --- roster ---

    async def _roster_sync_loop(self):
        while not self.is_closed():
            try:
                await asyncio.to_thread(self._do_roster_sync)
            except Exception as e:
                log.exception("roster sync failed: %s", e)
            await asyncio.sleep(self.cfg.roster_sync_min * 60)

    def _do_roster_sync(self) -> int:
        rows = fetch_roster(self.cfg.sa_json, self.cfg.sheet_id)
        current = self.store.roster_count()
        # ponytail: naive 25% deletion guard. Upgrade to a diff-and-alert if
        # officers start editing the sheet frequently.
        if current > 0 and len(rows) < current * 0.75:
            log.warning(
                "roster sync rejected: %d -> %d rows (>25%% drop, keeping snapshot)",
                current, len(rows),
            )
            return current
        self.store.replace_roster(rows)
        log.info("roster synced: %d rows", len(rows))
        return len(rows)

    # --- events ---

    async def on_member_join(self, member: discord.Member):
        if member.guild.id != self.cfg.guild_id:
            return
        role = member.guild.get_role(self.cfg.unverified_role_id)
        if not role:
            log.warning("unverified role %s not found", self.cfg.unverified_role_id)
            return
        try:
            await member.add_roles(role, reason="onboarding: auto-unverified on join")
        except discord.Forbidden:
            log.warning("cannot assign unverified role to %s", member)

    # --- flow ---

    async def grant_guest(self, interaction: discord.Interaction):
        member = interaction.user
        guest = interaction.guild.get_role(self.cfg.guest_role_id)
        unverified = interaction.guild.get_role(self.cfg.unverified_role_id)
        if not guest:
            await interaction.response.send_message(
                "Guest role isn't configured. Ping a mod.", ephemeral=True
            )
            return
        try:
            await member.add_roles(guest, reason="guest self-select")
            if unverified and unverified in member.roles:
                await member.remove_roles(unverified, reason="guest self-select")
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to change your roles. Ping a mod.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            "Welcome — glad you're here. Take a look around; ask anything.",
            ephemeral=True,
        )

    async def handle_member_submit(
        self, interaction: discord.Interaction, callsign_raw: str, email_raw: str
    ):
        callsign = callsign_raw.strip().upper()
        email = email_raw.strip().lower()

        if not CALLSIGN_RE.match(callsign):
            await interaction.response.send_message(
                f"`{callsign}` doesn't look like a valid callsign. Check for typos and try again.",
                ephemeral=True,
            )
            return
        if not EMAIL_RE.match(email):
            await interaction.response.send_message(
                "That email address doesn't look right. Try again.", ephemeral=True
            )
            return

        # rejoin: same user re-verifying an already-claimed callsign
        claim = self.store.claim_by_callsign(callsign)
        if claim and claim["discord_user_id"] == interaction.user.id:
            await self._grant_member(interaction, callsign)
            return
        if claim:
            await interaction.response.send_message(
                "That callsign is already registered. If this looks wrong, contact an officer.",
                ephemeral=True,
            )
            return

        # email already used by a different Discord user
        email_claim = self.store.claim_by_email(email)
        if email_claim and email_claim["discord_user_id"] != interaction.user.id:
            await interaction.response.send_message(
                "That email is already in use. If this looks wrong, contact an officer.",
                ephemeral=True,
            )
            return

        # roster match
        row = self.store.roster_lookup(callsign)
        if not row:
            await interaction.response.send_message(
                "That callsign isn't on our roster. If you're a member, contact an officer to be added.",
                ephemeral=True,
            )
            return
        if row["email"].lower() != email:
            # Don't disclose the on-file address.
            await interaction.response.send_message(
                "That email doesn't match what we have on file for that callsign. "
                "If your email changed, contact an officer.",
                ephemeral=True,
            )
            return

        # rate limit
        if self.store.recent_code_count(interaction.user.id, 1) >= self.cfg.code_rate_limit:
            await interaction.response.send_message(
                "You've requested too many codes recently. Try again in an hour.",
                ephemeral=True,
            )
            return

        # generate + send
        code = f"{secrets.randbelow(1_000_000):06d}"
        self.store.invalidate_active_codes(interaction.user.id)
        self.store.create_code(
            interaction.user.id, callsign, email, code, self.cfg.code_ttl_min
        )
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await asyncio.to_thread(
                send_code,
                self.cfg.smtp_host,
                self.cfg.smtp_port,
                self.cfg.smtp_user,
                self.cfg.smtp_password,
                self.cfg.from_email,
                self.cfg.from_name,
                email,
                callsign,
                code,
                self.cfg.code_ttl_min,
            )
        except Exception as e:
            log.exception("email send failed for user %s: %s", interaction.user.id, e)
            await interaction.followup.send(
                "Something went wrong sending the email. Try again shortly, or contact an officer.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            f"Sent a 6-digit code to the email on file for `{callsign}`. "
            f"It expires in {self.cfg.code_ttl_min} minutes.",
            view=EnterCodeView(self),
            ephemeral=True,
        )

    async def handle_code_submit(self, interaction: discord.Interaction, code: str):
        result = self.store.consume_code(interaction.user.id, code)
        if not result:
            await interaction.response.send_message(
                "That code didn't match or has expired. Request a new one from the welcome channel.",
                ephemeral=True,
            )
            return
        callsign, email = result

        # race: someone else may have claimed the callsign in the meantime.
        existing = self.store.claim_by_callsign(callsign)
        if existing and existing["discord_user_id"] != interaction.user.id:
            await interaction.response.send_message(
                "That callsign was just claimed by someone else. Contact an officer.",
                ephemeral=True,
            )
            return

        # Officer path: identity is proven by the email code, but the officer
        # role itself requires human approval (v2 doc §9). We do NOT grant
        # @member yet either — per doc, officer grants queue unconditionally.
        row = self.store.roster_lookup(callsign)
        if row and row["is_officer"]:
            if not existing:
                self.store.add_claim(callsign, interaction.user.id, email)
            qid = self.store.open_review(
                interaction.user.id, callsign, email, "officer_grant"
            )
            await self._post_review(interaction, qid, callsign, email, "officer_grant")
            await interaction.response.send_message(
                f"Email verified for `{callsign}`. Officer role requires human approval — "
                "an officer or the server owner will review shortly. You'll get a DM.",
                ephemeral=True,
            )
            return

        if not existing:
            self.store.add_claim(callsign, interaction.user.id, email)
        await self._grant_member(interaction, callsign)

    async def _grant_member(
        self,
        interaction: discord.Interaction,
        callsign: str,
        *,
        also_officer: bool = False,
        target_member: discord.Member | None = None,
        notify_target_dm: bool = False,
    ):
        """Grant @member (and optionally @officer). If target_member is given,
        role changes apply to that member instead of interaction.user — used
        when an officer approves a review case for someone else."""
        member = target_member or interaction.user
        guild = interaction.guild
        member_role = guild.get_role(self.cfg.member_role_id)
        officer_role = guild.get_role(self.cfg.officer_role_id) if also_officer else None
        unverified = guild.get_role(self.cfg.unverified_role_id)
        guest = guild.get_role(self.cfg.guest_role_id)
        if not member_role or (also_officer and not officer_role):
            await self._reply(interaction, "Role IDs aren't configured. Ping a mod.", True)
            return
        add = [member_role] + ([officer_role] if officer_role else [])
        try:
            await member.add_roles(*add, reason=f"verified: {callsign}")
            to_remove = [r for r in (unverified, guest) if r and r in member.roles]
            if to_remove:
                await member.remove_roles(*to_remove, reason="verified")
        except discord.Forbidden:
            await self._reply(interaction, "Role update failed. Bot role position?", True)
            return

        if target_member is None:
            await self._reply(interaction, f"Verified as `{callsign}`. Welcome.", True)
            return

        if notify_target_dm:
            try:
                await member.send(
                    f"You've been approved as `{callsign}`. Welcome"
                    + (" — officer role granted." if also_officer else ".")
                )
            except discord.Forbidden:
                pass  # DMs closed. Nothing we can do.

    async def _post_review(
        self,
        interaction: discord.Interaction,
        qid: int,
        callsign: str,
        email: str,  # kept for audit only; do not embed it (§6.5 privacy)
        reason_code: str,
    ):
        channel = interaction.guild.get_channel(self.cfg.review_channel_id)
        if not channel:
            log.error("review channel %s not found", self.cfg.review_channel_id)
            return
        row = self.store.roster_lookup(callsign)
        user = interaction.user
        created = user.created_at.strftime("%Y-%m-%d") if user.created_at else "?"
        joined = (
            user.joined_at.strftime("%Y-%m-%d")
            if isinstance(user, discord.Member) and user.joined_at
            else "?"
        )
        embed = discord.Embed(
            title=f"Review needed — {reason_code}",
            description=f"{user.mention} claims `{callsign}`",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Submitted callsign", value=f"`{callsign}`", inline=True)
        embed.add_field(
            name="Roster row",
            value=(
                f"{row['full_name']}"
                + ("  •  officer" if row["is_officer"] else "")
            )
            if row
            else "NOT ON ROSTER",
            inline=True,
        )
        embed.add_field(
            name="Discord user",
            value=f"{user} (id `{user.id}`)",
            inline=False,
        )
        embed.add_field(name="Account created", value=created, inline=True)
        embed.add_field(name="Server joined", value=joined, inline=True)
        embed.set_footer(text=f"review id {qid}")
        await channel.send(embed=embed, view=_review_view(qid))

    async def handle_review_decision(
        self, interaction: discord.Interaction, qid: int, decision: str
    ):
        # authz: server owner, administrators, or officer role holders
        if not self._can_review(interaction):
            await interaction.response.send_message(
                "You don't have permission to resolve review cases.", ephemeral=True
            )
            return

        review = self.store.get_review(qid)
        if not review:
            await interaction.response.send_message(
                f"Review #{qid} not found.", ephemeral=True
            )
            return
        if review["resolved_at"]:
            await interaction.response.send_message(
                f"Review #{qid} was already resolved by <@{review['resolved_by']}>: "
                f"{review['resolution']}.",
                ephemeral=True,
            )
            return

        if not self.store.resolve_review(qid, interaction.user.id, decision):
            # Race: someone else resolved it between the check and now.
            await interaction.response.send_message(
                "Someone else just resolved this case.", ephemeral=True
            )
            return

        target = interaction.guild.get_member(review["discord_user_id"])
        callsign = review["callsign"]

        if decision == "approve":
            if not target:
                await interaction.response.send_message(
                    "Approved in the ledger, but the target user left the server.",
                    ephemeral=True,
                )
            else:
                await self._grant_member(
                    interaction,
                    callsign,
                    also_officer=(review["reason_code"] == "officer_grant"),
                    target_member=target,
                    notify_target_dm=True,
                )
            log.info(
                "review %s approved by %s for user %s callsign %s",
                qid, interaction.user.id, review["discord_user_id"], callsign,
            )
        else:  # deny
            if target:
                try:
                    await target.send(
                        f"Your verification for `{callsign}` was not approved. "
                        "If this looks wrong, contact the server owner or another officer."
                    )
                except discord.Forbidden:
                    pass
            # Denial for officer_grant means we do NOT grant the member role either.
            # The email code proved inbox control, not that this is the right person.
            log.info(
                "review %s denied by %s for user %s callsign %s",
                qid, interaction.user.id, review["discord_user_id"], callsign,
            )

        # Update the embed to reflect resolution and strip the buttons.
        try:
            msg = interaction.message
            if msg and msg.embeds:
                e = msg.embeds[0]
                e.color = (
                    discord.Color.green()
                    if decision == "approve"
                    else discord.Color.red()
                )
                e.title = f"{e.title}  —  {decision.upper()} by {interaction.user}"
                await msg.edit(embed=e, view=None)
        except discord.HTTPException:
            pass

        await interaction.response.send_message(
            f"Marked review #{qid} as {decision}.", ephemeral=True
        )

    def _can_review(self, interaction: discord.Interaction) -> bool:
        user = interaction.user
        guild = interaction.guild
        if guild and user.id == guild.owner_id:
            return True
        if isinstance(user, discord.Member):
            if user.guild_permissions.administrator:
                return True
            if any(r.id == self.cfg.officer_role_id for r in user.roles):
                return True
        return False

    @staticmethod
    async def _reply(interaction: discord.Interaction, text: str, ephemeral: bool):
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(text, ephemeral=ephemeral)


# ------------------------------------------------------------- slash commands

def register_commands(bot: SrelBot) -> None:

    @bot.tree.command(
        name="post-welcome",
        description="Post the persistent verification message to the welcome channel.",
    )
    @app_commands.default_permissions(administrator=True)
    async def post_welcome(interaction: discord.Interaction):
        channel = interaction.guild.get_channel(bot.cfg.welcome_channel_id)
        if not channel:
            await interaction.response.send_message(
                "Welcome channel not found.", ephemeral=True
            )
            return
        await channel.send(
            "**Welcome to SREL.**\n"
            "Click below to get started.\n\n"
            "• **Club member?** We'll verify by emailing a short code to the address on file.\n"
            "• **Just visiting?** No callsign needed — say hello.",
            view=WelcomeView(bot),
        )
        await interaction.response.send_message("Posted.", ephemeral=True)

    @bot.tree.command(
        name="roster-sync",
        description="Force a roster resync from the Google Sheet.",
    )
    @app_commands.default_permissions(administrator=True)
    async def roster_sync(interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            count = await asyncio.to_thread(bot._do_roster_sync)
            await interaction.followup.send(
                f"Synced. Roster now has {count} rows.", ephemeral=True
            )
        except Exception as e:
            log.exception("manual roster sync failed")
            await interaction.followup.send(f"Sync failed: {e}", ephemeral=True)

    @bot.tree.command(
        name="claim-release",
        description="Release a claimed callsign so it can be re-verified.",
    )
    @app_commands.default_permissions(administrator=True)
    async def claim_release(interaction: discord.Interaction, callsign: str):
        callsign = callsign.strip().upper()
        row = bot.store.claim_by_callsign(callsign)
        if not row:
            await interaction.response.send_message(
                f"`{callsign}` is not claimed.", ephemeral=True
            )
            return
        bot.store.release_claim(callsign)
        await interaction.response.send_message(
            f"Released `{callsign}` (was claimed by <@{row['discord_user_id']}>).",
            ephemeral=True,
        )

    @bot.tree.command(
        name="claim-who",
        description="Show who holds a callsign.",
    )
    @app_commands.default_permissions(administrator=True)
    async def claim_who(interaction: discord.Interaction, callsign: str):
        callsign = callsign.strip().upper()
        row = bot.store.claim_by_callsign(callsign)
        if not row:
            await interaction.response.send_message(
                f"`{callsign}` is not claimed.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"`{callsign}` → <@{row['discord_user_id']}> (claimed {row['claimed_at']}).",
            ephemeral=True,
        )

    @bot.tree.command(
        name="roster-status",
        description="Show roster + claim + review queue counters.",
    )
    @app_commands.default_permissions(administrator=True)
    async def roster_status(interaction: discord.Interaction):
        s = bot.store
        last = s.roster_last_sync() or "never"
        msg = (
            f"**Roster:** {s.roster_count()} rows  "
            f"({s.roster_officer_count()} officers)\n"
            f"**Last sync:** {last}\n"
            f"**Claimed callsigns:** {s.claimed_count()}\n"
            f"**Open review cases:** {s.open_review_count()}"
        )
        await interaction.response.send_message(msg, ephemeral=True)


# --------------------------------------------------------------------- entry

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = Config.from_env()
    bot = SrelBot(cfg)
    register_commands(bot)
    bot.run(cfg.token, log_handler=None)


if __name__ == "__main__":
    main()
