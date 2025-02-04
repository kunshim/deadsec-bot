import asyncio
import logging
import subprocess
import tempfile
from datetime import datetime
from typing import Callable, Optional

import aiohttp
import discord
from aiohttp.client_exceptions import ClientError
from bson import ObjectId
from discord import app_commands
from discord.app_commands import Choice
from discord.ext import commands
import time
import sys

from config import (
    CHALLENGE_COLLECTION,
    CTF_COLLECTION,
    DATE_FORMAT,
    DBNAME,
    MAX_CONTENT_SIZE,
    MONGO,
)
from lib.discord_util import (
    add_challenge_worker,
    get_challenge_category_channel,
    is_deferred,
    mark_if_maxed,
    parse_challenge_solvers,
    parse_member_mentions,
    remove_challenge_worker,
    send_scoreboard,
)
from lib.platforms import PlatformCTX, match_platform
from lib.types import CTFStatusMode, Permissions, Privacy
from lib.util import (
    get_challenge_info,
    get_ctf_info,
    sanitize_channel_name,
    strip_url_components,
)
from msg_components.buttons.workon import WorkonButton
from msg_components.forms.credentials import create_credentials_modal_for_platform
from msg_components.forms.flag import FlagSubmissionForm

_log = logging.getLogger(__name__)


class AsyncRemote:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.reader = None
        self.writer = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        return self

    async def sendline(self, data):
        if isinstance(data, str):
            data = data.encode()
        self.writer.write(data + b'\n')
        await self.writer.drain()

    async def recvline(self):
        try:
            data = await self.reader.readline()
            return data
        except Exception as e:
            print(f"Error receiving data: {e}")
            return None

    async def close(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()

async def remote(host, port):
    return AsyncRemote(host, port)


class CTF(app_commands.Group):
    """Manage a CTF competition."""

    def __init__(self) -> None:
        self._chat_export_tasks = []
        self.ctf_join_messages = {}
        super().__init__(name="ctf")

    async def on_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        _log.exception(
            "Exception occurred due to `/%s %s`",
            interaction.command.parent.name,
            interaction.command.name,
            exc_info=error,
        )
        msg = {"content": "An exception has occurred", "ephemeral": True}
        if is_deferred(interaction):
            await interaction.followup.send(**msg)
        elif interaction.response.type is None:
            await interaction.response.send_message(**msg)

    @staticmethod
    def _in_ctf_channel() -> Callable[..., bool]:
        """Wrapper function to check if a command was issued from a CTF channel."""

        async def predicate(interaction: discord.Interaction) -> bool:
            if get_ctf_info(guild_category=interaction.channel.category_id):
                return True

            await interaction.response.send_message(
                "You must be in a CTF channel to use this command.", ephemeral=True
            )
            return False

        return app_commands.check(predicate)

    async def _ctf_autocompletion_func(
        self, _: discord.Interaction, current: str
    ) -> list[Choice[str]]:
        """Autocomplete CTF name.
        This function is inefficient, might improve it later.

        Args:
            _: The interaction that triggered this command.
            current: The CTF name typed so far.

        Returns:
            A list of suggestions.
        """
        suggestions = []
        for ctf in MONGO[DBNAME][CTF_COLLECTION].find(
            {"archived": False, "ended": False}
        ):
            if current.lower() in ctf["name"].lower():
                suggestions.append(Choice(name=ctf["name"], value=ctf["name"]))
            if len(suggestions) == 25:
                break
        return suggestions

    async def _challenge_autocompletion_func(
        self, interaction: discord.Interaction, current: str
    ) -> list[Choice[str]]:
        """Autocomplete challenge name.

        This function is inefficient, might improve it later.

        Args:
            interaction: The interaction that triggered this command.
            current: The challenge name typed so far.

        Returns:
            A list of suggestions.
        """
        ctf = get_ctf_info(guild_category=interaction.channel.category_id)
        if ctf is None:
            return []

        suggestions = []
        for challenge_id in ctf["challenges"]:
            challenge = get_challenge_info(_id=challenge_id)
            if challenge is None or challenge["solved"]:
                continue

            display_name = f"{challenge['name']} ({challenge['category']})"
            if not current.strip() or current.lower() in display_name.lower():
                suggestions.append(Choice(name=display_name, value=challenge["name"]))

            if len(suggestions) == 25:
                break
        return suggestions
    def has_required_roles():
        async def predicate(interaction: discord.Interaction) -> bool:
            ROLES = [981520041663164486, 1305820255771430954, 1049604803564666950]
            member = interaction.user
            user_role_ids = {role.id for role in member.roles}
            
            # ROLES 리스트의 권한 중 하나라도 있는지 확인
            if not any(role_id in user_role_ids for role_id in ROLES):
                await interaction.response.send_message(
                    "Admin only 😒",
                    ephemeral=False
                )
                return True
            return True
        return app_commands.check(predicate)
    

    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    @app_commands.checks.has_permissions(manage_channels=True, manage_roles=True)
    @app_commands.command()
    #@has_required_roles()
    async def createctf(self, interaction: discord.Interaction, name: str) -> None:
    # 응답을 지연시켜 긴 작업 수행 가능하게 함
        await interaction.response.defer()
        
        # CTF 생성 시도
        ctf = await interaction.client.create_ctf(name)
        if not ctf:
            # 동일한 이름의 CTF가 이미 존재하는 경우
            await interaction.followup.send(
                "Another CTF with similar name already exists, please choose another name",
                ephemeral=True,
            )
            return

        await interaction.followup.send(f"✅ CTF `{name}` has been created.")
        
        # 공지 채널에 메시지 전송        
        announcement_channel = interaction.guild.get_channel(1320032654065930311)  # Replace with your channel ID
        announcement_message = await announcement_channel.send(
            f"🚩 New CTF Created: **{name}**\n"
            f"React with ⚔️ to join this CTF!"
        )
        
        # 메시지에 리액션 추가
        await announcement_message.add_reaction("⚔️")
        
        # 리액션 이벤트를 처리하는 이벤트 리스너 설정
        @interaction.client.event
        async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
            # 봇의 리액션은 무시
            if payload.user_id == interaction.client.user.id:
                return
                
            # 해당 메시지의 ⚔️ 리액션인지 확인
            if (payload.message_id == announcement_message.id and 
                str(payload.emoji) == "⚔️"):
                
                guild = interaction.client.get_guild(payload.guild_id)
                if not guild:
                    return
                    
                member = guild.get_member(payload.user_id)
                if not member:
                    return
                    
                # CTF 정보 가져오기
                ctf_info = get_ctf_info(name=name)
                if not ctf_info:
                    return
                    
                # private CTF 체크
                if ctf_info.get("private"):
                    return
                    
                # role 할당
                role = discord.utils.get(guild.roles, id=ctf_info["guild_role"])
                if not role:
                    return
                    
                # 유저에게 role 추가
                await member.add_roles(role)
                
                # general 채널에 참가 메시지 전송
                ctf_general_channel = discord.utils.get(
                    guild.text_channels,
                    category_id=ctf_info["guild_category"],
                    name="general",
                )
                if ctf_general_channel:
                    await ctf_general_channel.send(
                        f"{member.mention} joined the battle ⚔️"
                    )
        @interaction.client.event
        async def on_raw_reaction_remove(payload: discord.RawReactionActionEvent):
            # 봇의 리액션 제거는 무시합니다
            if payload.user_id == interaction.client.user.id:
                return
        
            # 해당 메시지의 ⚔️ 리액션이 제거되었는지 확인합니다
            if (payload.message_id == announcement_message.id and 
                str(payload.emoji) == "⚔️"):
                
                # 서버 정보를 가져옵니다
                guild = interaction.client.get_guild(payload.guild_id)
                if not guild:
                    return
                    
                # 멤버 정보를 가져옵니다
                member = guild.get_member(payload.user_id)
                if not member:
                    return
                    
                # CTF 정보를 가져옵니다
                ctf_info = get_ctf_info(name=name)
                if not ctf_info:
                    return
                    
                # private CTF인 경우 처리하지 않습니다
                if ctf_info.get("private"):
                    return
                    
                # 제거할 role을 찾습니다
                role = discord.utils.get(guild.roles, id=ctf_info["guild_role"])
                if not role:
                    return
                    
                # 유저의 role을 제거합니다
                await member.remove_roles(role)
                
                # general 채널에 퇴장 메시지를 전송합니다
                ctf_general_channel = discord.utils.get(
                    guild.text_channels,
                    category_id=ctf_info["guild_category"],
                    name="general",
                )
                if ctf_general_channel:
                    await ctf_general_channel.send(
                        f"{member.mention} left the battle 🏃"
                    )

    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.command()
    @has_required_roles()
    @_in_ctf_channel()
    async def renamectf(self, interaction: discord.Interaction, new_name: str) -> None:
        """Rename a previously created CTF.

        Args:
            interaction: The interaction that triggered this command.
            new_name: New CTF name (case insensitive).
        """
        ctf = get_ctf_info(guild_category=interaction.channel.category_id)
        old_name = ctf["name"]
        ctf["name"] = new_name

        category_channel = discord.utils.get(
            interaction.guild.categories, id=interaction.channel.category_id
        )

        # Update CTF name in the database.
        MONGO[DBNAME][CTF_COLLECTION].update_one(
            {"_id": ctf["_id"]}, {"$set": {"name": ctf["name"]}}
        )
        await interaction.response.send_message(
            f"✅ CTF `{old_name}` has been renamed to `{new_name}`."
        )

        # Rename category channel for the CTF.
        await category_channel.edit(
            name=category_channel.name.replace(old_name, new_name)
        )

    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    @app_commands.checks.has_permissions(manage_channels=True, manage_roles=True)
    @app_commands.command()
    @app_commands.autocomplete(name=_ctf_autocompletion_func)  # type: ignore
    async def archivectf(
        self,
        interaction: discord.Interaction,
        permissions: Optional[Permissions] = Permissions.RDONLY,
        members: Optional[str] = None,
        name: Optional[str] = None,
    ):
        """Archive a CTF by making its channels read-only by default.

        Args:
            interaction: The interaction that triggered this command.
            permissions: Whether channels should be read-only or writable
               as well (default: read only).
            members: A list of member or role mentions to be granted access into the
               private threads.
            name: CTF name (default: current channel's CTF).
        """

        async def get_confirmation() -> bool:
            class Prompt(discord.ui.View):
                def __init__(self) -> None:
                    super().__init__(timeout=None)
                    self.add_item(
                        discord.ui.Button(style=discord.ButtonStyle.green, label="Yes")
                    )
                    self.add_item(
                        discord.ui.Button(style=discord.ButtonStyle.red, label="No")
                    )
                    self.children[0].callback = self.yes_callback
                    self.children[1].callback = self.no_callback

                async def yes_callback(_, interaction: discord.Interaction) -> None:
                    await interaction.response.edit_message(
                        content="🔄 Starting CTF archival...",
                        view=None,
                    )
                    await self.archivectf.callback(
                        self,
                        interaction=interaction,
                        permissions=permissions,
                        members=members or "",
                        name=name,
                    )

                async def no_callback(_, interaction: discord.Interaction) -> None:
                    await interaction.response.edit_message(
                        content="Aborting CTF archival.", view=None
                    )

            await interaction.response.send_message(
                content=(
                    "It appears that you forgot to set the `members` parameter, this "
                    "is important if you want to grant people access to private "
                    "threads that they weren't part of.\n"
                    "⚠️ This action cannot be undone, would you like to continue?"
                ),
                view=Prompt(),
                ephemeral=True,
            )

        if name is not None:
            ctf = get_ctf_info(name=name)
        else:
            ctf = get_ctf_info(guild_category=interaction.channel.category_id)

        if not ctf:
            await interaction.response.send_message(
                (
                    "Run this command from within a CTF channel, or provide the name "
                    "of the CTF you wish to archive."
                )
                if name is None
                else "No such CTF.",
                ephemeral=True,
            )
            return

        # In case CTF was already archived.
        if ctf["archived"]:
            await interaction.response.send_message(
                "This CTF was already archived.", ephemeral=True
            )
            return

        if members is None:
            return await get_confirmation()

        if not interaction.response.is_done():
            await interaction.response.defer()

        category_channel = discord.utils.get(
            interaction.guild.categories, id=ctf["guild_category"]
        )
        role = discord.utils.get(interaction.guild.roles, id=ctf["guild_role"])

        # Get all challenges for the CTF.
        challenges = [
            get_challenge_info(_id=challenge_id) for challenge_id in ctf["challenges"]
        ]

        # Sort by category, then by name.
        challenges = sorted(
            challenges, key=lambda challenge: (challenge["category"], challenge["name"])
        )
        if challenges:
            name_field_width = (
                max(len(challenge["name"]) for challenge in challenges) + 10
            )

            # Post challenge solves summary in the scoreboard channel.
            head = (
                "```diff\n"
                f"  {'Challenge':<{name_field_width}}"
                f"{'Category':<30}{'Solved':<30}{'Blood'}\n\n{{}}"
                "```"
            )
            summaries = []
            summary = ""
            for challenge in challenges:
                content = (
                    f"{['-', '+'][challenge['solved']]} "
                    f"{challenge['name']:<{name_field_width}}"
                    f"{challenge['category']:<30}"
                    f"{['❌', '✔️'][challenge['solved']]:<30}"
                    f"{['', '🩸'][challenge['blooded']]}\n"
                )
                if len(head) - 2 + len(summary) + len(content) > MAX_CONTENT_SIZE:
                    summaries.append(summary)
                    summary = content
                else:
                    summary += content

            summaries.append(summary)

            scoreboard_channel = discord.utils.get(
                interaction.guild.text_channels, id=ctf["guild_channels"]["scoreboard"]
            )
            for summary in summaries:
                await scoreboard_channel.send(head.format(summary))

        # Make threads invitable and lock them if needed.
        locked = permissions == Permissions.RDONLY
        for thread in interaction.guild.threads:
            if thread.parent is None or thread.category_id != ctf["guild_category"]:
                continue
            await thread.edit(locked=locked, invitable=True)

            if not members:
                continue

            # XXX Until Discord supports changing threads privacy, this is the only
            # workaround in order to add members to a thread without bombing them
            # with notifications.
            message = await thread.send(content="Adding members...", silent=True)
            await message.edit(content=members)
            await message.delete()

        # Change channels permissions according to the `permissions` parameter.
        members = [
            member
            async for member in interaction.guild.fetch_members(limit=None)
            if role in member.roles
        ]

        perm_rdwr = discord.PermissionOverwrite(read_messages=True, send_messages=True)
        perm_rdonly = discord.PermissionOverwrite(
            read_messages=True, send_messages=False
        )

        ctf_general_channel = discord.utils.get(
            interaction.guild.text_channels,
            category_id=ctf["guild_category"],
            name="general",
        )
        overwrites = {member: perm_rdwr for member in members}
        overwrites[interaction.guild.default_role] = discord.PermissionOverwrite(
            read_messages=False
        )
        await ctf_general_channel.edit(overwrites=overwrites)

        for member in members:
            overwrites[member] = (
                perm_rdonly if permissions == Permissions.RDONLY else perm_rdwr
            )

        await category_channel.edit(name=f"🔒 {ctf['name']}", overwrites=overwrites)
        for ctf_channel in category_channel.channels:
            if ctf_channel.name == "general":
                continue
            await ctf_channel.edit(sync_permissions=True)

        # Delete the CTF role.
        if role:
            await role.delete()

        # Delete all challenges for that CTF from the database.
        for challenge_id in ctf["challenges"]:
            MONGO[DBNAME][CHALLENGE_COLLECTION].delete_one({"_id": challenge_id})

        # Update status of the CTF.
        MONGO[DBNAME][CTF_COLLECTION].update_one(
            {"_id": ctf["_id"]}, {"$set": {"archived": True, "ended": True}}
        )

        msg = f"✅ CTF `{ctf['name']}` has been archived."
        if interaction.response.is_done():
            await interaction.edit_original_response(content=msg)
            return
        await interaction.followup.send(content=msg)

    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    @app_commands.checks.has_permissions(manage_channels=True, manage_roles=True)
    @has_required_roles()
    @app_commands.command()
    @app_commands.autocomplete(name=_ctf_autocompletion_func)  # type: ignore
    async def deletectf(
        self, interaction: discord.Interaction, name: Optional[str] = None
    ) -> None:
        """Delete a CTF.

        Args:
            interaction: The interaction that triggered this command.
            name: Name of the CTF to delete (default: CTF associated with the
                category channel from which the command was issued).
        """
        await interaction.response.defer()

        if name is not None:
            ctf = get_ctf_info(name=name)
        else:
            ctf = get_ctf_info(guild_category=interaction.channel.category_id)

        if not ctf:
            await interaction.followup.send(
                (
                    "Run this command from within a CTF channel, or provide the name "
                    "of the CTF you wish to delete."
                )
                if name is None
                else "No such CTF.",
                ephemeral=True,
            )
            return

        category_channel = discord.utils.get(
            interaction.guild.categories, id=ctf["guild_category"]
        )
        role = discord.utils.get(interaction.guild.roles, id=ctf["guild_role"])

        # Delete all channels.
        for ctf_channel in category_channel.channels:
            await ctf_channel.delete()

        # Delete the category channel.
        await category_channel.delete()

        # Delete the CTF role.
        if role:
            await role.delete()

        # Delete all challenges for that CTF from the database.
        for challenge_id in ctf["challenges"]:
            MONGO[DBNAME][CHALLENGE_COLLECTION].delete_one({"_id": challenge_id})

        # Delete the CTF from the database.
        MONGO[DBNAME][CTF_COLLECTION].delete_one({"_id": ctf["_id"]})

        # Only send a followup message if the channel from which the command was issued
        # still exists, otherwise we will fail with a 404 not found.
        if name and interaction.channel.category_id != category_channel.id:
            await interaction.followup.send(f"✅ CTF `{ctf['name']}` has been deleted.")

    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    @app_commands.checks.has_permissions(manage_channels=True, manage_roles=True)
    @app_commands.command()
    async def setprivacy(
        self,
        interaction: discord.Interaction,
        privacy: Privacy,
        name: Optional[str] = None,
    ) -> None:
        """Toggle a CTF privacy. Making the CTF private disallows others from joining
        the CTF and would require an admin invitation.

        Args:
            interaction: The interaction that triggered this command.
            name: Name of the CTF to delete (default: CTF associated with the
                category channel from which the command was issued).
            privacy: The CTF privacy.
        """
        if name is not None:
            ctf = get_ctf_info(name=name)
        else:
            ctf = get_ctf_info(guild_category=interaction.channel.category_id)

        if not ctf:
            await interaction.followup.send(
                (
                    "Run this command from within a CTF channel, or provide the name "
                    "of the CTF for which you wish to change the privacy."
                )
                if name is None
                else "No such CTF.",
                ephemeral=True,
            )
            return

        MONGO[DBNAME][CTF_COLLECTION].update_one(
            {"_id": ctf["_id"]}, {"$set": {"private": bool(privacy.value)}}
        )
        await interaction.response.send_message(
            f"CTF privacy changed to `{privacy.name}`", ephemeral=True
        )

    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.command()
    @app_commands.autocomplete(name=_ctf_autocompletion_func)  # type: ignore
    async def addplayers(
        self, interaction: discord.Interaction, name: str, members: Optional[str] = None
    ) -> None:
        """Add members to a CTF.

        Args:
            interaction: The interaction that triggered this command.
            name: Name of the CTF to add people into (case insensitive).
            members: List of member mentions that you wish to add.
        """
        await interaction.response.defer()

        ctf = get_ctf_info(name=name)
        if ctf is None:
            await interaction.followup.send("No such CTF.", ephemeral=True)
            return

        role = discord.utils.get(interaction.guild.roles, id=ctf["guild_role"])
        if role is None:
            await interaction.followup.send(
                "CTF role was (accidentally?) deleted by an admin, aborting.",
                ephemeral=True,
            )
            return

        ctf_general_channel = discord.utils.get(
            interaction.guild.text_channels,
            category_id=ctf["guild_category"],
            name="general",
        )

        if members is None:
            for scheduled_event in interaction.guild.scheduled_events:
                if scheduled_event.name == ctf["name"]:
                    break
            else:
                await interaction.followup.send(
                    "No event matching the provided CTF name was found.",
                    ephemeral=True,
                )
                return

            async for user in scheduled_event.users():
                member = await interaction.guild.fetch_member(user.id)
                await member.add_roles(role)
        else:
            for member in await parse_member_mentions(interaction, members):
                await member.add_roles(role)
                await ctf_general_channel.send(
                    f"{member.mention} was added by {interaction.user.mention} 🔫"
                )

        await interaction.followup.send(
            f"✅ Added players to `{ctf['name']}`.", ephemeral=True
        )

    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @app_commands.command()
    @app_commands.autocomplete(name=_ctf_autocompletion_func)  # type: ignore
    async def join(self, interaction: discord.Interaction, name: str) -> None:
        """Join and ongoing CTF competition.

        Args:
            interaction: The interaction that triggered this command.
            name: Name of the CTF to join (case insensitive).
        """
        ctf = get_ctf_info(name=name)
        return 
        if ctf is None:
            await interaction.response.send_message("No such CTF.", ephemeral=True)
            return

        if ctf.get("private"):
            await interaction.response.send_message(
                "This CTF is private and requires invitation by an admin.",
                ephemeral=True,
            )
            return

        role = discord.utils.get(interaction.guild.roles, id=ctf["guild_role"])
        if role is None:
            await interaction.response.send_message(
                "CTF role was (accidentally?) deleted by an admin, aborting.",
                ephemeral=True,
            )
            return

        ctf_general_channel = discord.utils.get(
            interaction.guild.text_channels,
            category_id=ctf["guild_category"],
            name="general",
        )
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f"✅ Added to `{ctf['name']}`.")
        await ctf_general_channel.send(
            f"{interaction.user.mention} joined the battle ⚔️"
        )

    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @app_commands.command()
    @_in_ctf_channel()
    async def leave(self, interaction: discord.Interaction) -> None:
        """Leave the current CTF.

        Args:
            interaction: The interaction that triggered this command.
        """
        ctf = get_ctf_info(guild_category=interaction.channel.category_id)
        if not ctf:
            return

        role = discord.utils.get(interaction.guild.roles, id=ctf["guild_role"])

        # Announce that the user left the CTF.
        ctf_general_channel = discord.utils.get(
            interaction.guild.text_channels,
            category_id=ctf["guild_category"],
            name="general",
        )
        await interaction.response.send_message(
            f"✅ Removed from `{ctf['name']}`.", ephemeral=True
        )
        await ctf_general_channel.send(
            f"{interaction.user.mention} abandonned the ship :frowning:"
        )

        # Remove user from the list of players.
        for challenge in MONGO[DBNAME][CHALLENGE_COLLECTION].find():
            if interaction.user.name in challenge["players"]:
                challenge["players"].remove(interaction.user.name)
                MONGO[DBNAME][CHALLENGE_COLLECTION].update_one(
                    {"_id": challenge["_id"]},
                    {"$set": {"players": challenge["players"]}},
                )

        # Remove CTF role.
        await interaction.user.remove_roles(role)

    @app_commands.checks.bot_has_permissions(manage_channels=True, send_messages=True)
    @app_commands.command()
    #@has_required_roles()
    @_in_ctf_channel()
    async def createchallenge(
        self,
        interaction: discord.Interaction,
        name: str,
        category: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        
        try:
            category = category.title().strip()

            if get_challenge_info(name=name, category=category):
                await interaction.followup.send("This challenge already exists.", ephemeral=True)
                return

            ctf = get_ctf_info(guild_category=interaction.channel.category_id)

            if ctf["archived"]:
                await interaction.followup.send("This CTF is archived.", ephemeral=True)
                return

            category_channel = discord.utils.get(
                interaction.guild.categories, id=interaction.channel.category_id
            )

            text_channel = await get_challenge_category_channel(
                interaction.guild, category_channel, category
            )

            thread_name = sanitize_channel_name(name)
            challenge_thread = await text_channel.create_thread(
                name=f"❌-{thread_name}", invitable=False,
                type=discord.ChannelType.public_thread  
            )
                        
                
            print(f"Thread created: {challenge_thread.id}")
            challenge_oid = ObjectId()

            announcements_channel = discord.utils.get(
                interaction.guild.text_channels, id=ctf["guild_channels"]["announcements"]
            )
            role = discord.utils.get(interaction.guild.roles, id=ctf["guild_role"])

            embed = discord.Embed(
                title="🔔 New challenge created!",
                description=(
                    f"**Challenge name:** {name}\n"
                    f"**Category:** {category}\n\n"
                    f"Use `/ctf workon {name}` or the button to join.\n"
                    f"{role.mention}"
                ),
                colour=discord.Colour.dark_gold(),
                timestamp=datetime.now(),
            )
            announcement = await announcements_channel.send(
                embed=embed, view=WorkonButton(oid=challenge_oid)
            )

            # DB 작업을 transaction으로 묶음
            try:
                MONGO[DBNAME][CHALLENGE_COLLECTION].insert_one(
                    {
                        "_id": challenge_oid,
                        "id": None,
                        "name": name,
                        "category": category,
                        "thread": challenge_thread.id,
                        "solved": False,
                        "blooded": False,
                        "players": [],
                        "announcement": announcement.id,
                        "solve_time": None,
                        "solve_announcement": None,
                        "flag": None,
                    }
                )

                ctf["challenges"].append(challenge_oid)
                MONGO[DBNAME][CTF_COLLECTION].update_one(
                    {"_id": ctf["_id"]}, {"$set": {"challenges": ctf["challenges"]}}
                )
            except Exception as e:
                print(f"Database error: {e}")
                await interaction.followup.send("Failed to update database.", ephemeral=True)
                return

            await interaction.followup.send(f"✅ Challenge `{name}` has been created.", ephemeral=True)
            
            # channel edit를 마지막으로 이동하고 에러 처리 추가
            try:
                await text_channel.edit(
                    name=text_channel.name.replace("💤", "🔄").replace("🎯", "🔄")
                )
            except Exception as e:
                print(f"Channel edit error: {e}")
                # 채널 이름 변경 실패는 크리티컬한 에러가 아니므로 사용자에게 알리지 않음
                
        except Exception as e:
            print(f"Unexpected error: {str(e)}")
            await interaction.followup.send(
                f"An unexpected error occurred: {str(e)}", ephemeral=True
            )

    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.command()
    @_in_ctf_channel()
    async def renamechallenge(
        self,
        interaction: discord.Interaction,
        new_name: str,
    ) -> None:
        """Rename a challenge.
        Args:
            interaction: The interaction that triggered this command.
            new_name: New challenge name.
        """
        challenge = get_challenge_info(thread=interaction.channel_id)
        if challenge is None:
            await interaction.response.send_message(
                "Run this command from within a challenge thread.",
                ephemeral=True,
            )
            return

        challenge_thread = discord.utils.get(
            interaction.guild.threads, id=interaction.channel_id
        )
        new_thread_name = sanitize_channel_name(new_name)

        if challenge["blooded"]:
            new_thread_name = f"🩸-{new_thread_name}"
        elif challenge["solved"]:
            new_thread_name = f"✅-{new_thread_name}"
        else:
            new_thread_name = f"❌-{new_thread_name}"

        MONGO[DBNAME][CHALLENGE_COLLECTION].update_one(
            {"_id": challenge["_id"]},
            {"$set": {"name": new_name}},
        )
        await interaction.response.send_message("✅ Challenge renamed.")
        await challenge_thread.edit(name=new_thread_name)

    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.command()
    @app_commands.autocomplete(name=_challenge_autocompletion_func)  # type: ignore
    @_in_ctf_channel()
    async def deletechallenge(
        self, interaction: discord.Interaction, name: Optional[str] = None
    ) -> None:
        """Delete a challenge from the CTF.

        Args:
            interaction: The interaction that triggered this command.
            name: Name of the challenge to delete (default: current threads's
                challenge).
        """
        if name is None:
            challenge = get_challenge_info(thread=interaction.channel_id)
            if challenge is None:
                await interaction.response.send_message(
                    (
                        "Run this command from within a challenge thread, "
                        "or provide the name of the challenge you wish to delete."
                    ),
                    ephemeral=True,
                )
                return
        else:
            challenge = get_challenge_info(name=name)
            if challenge is None:
                await interaction.response.send_message(
                    "No such challenge.", ephemeral=True
                )
                return

        # Get CTF to which the challenge is associated.
        ctf = get_ctf_info(guild_category=interaction.channel.category_id)

        # Delete challenge from the database.
        MONGO[DBNAME][CHALLENGE_COLLECTION].delete_one(challenge)

        # Delete reference to that challenge from the CTF.
        ctf["challenges"].remove(challenge["_id"])
        MONGO[DBNAME][CTF_COLLECTION].update_one(
            {"_id": ctf["_id"]}, {"$set": {"challenges": ctf["challenges"]}}
        )

        # Delete announcement message.
        announcements_channel = discord.utils.get(
            interaction.guild.text_channels,
            id=ctf["guild_channels"]["announcements"],
        )
        announcement = await announcements_channel.fetch_message(
            challenge["announcement"]
        )
        if announcement:
            await announcement.delete()

        await interaction.response.send_message(
            f"✅ Challenge `{challenge['name']}` has been deleted."
        )

        # Delete thread associated with the challenge.
        challenge_thread = discord.utils.get(
            interaction.guild.threads, id=challenge["thread"]
        )
        await challenge_thread.delete()

        # Indicate that the CTF category is empty in case that are no challenge threads
        # inside.
        text_channel = challenge_thread.parent
        if len(text_channel.threads) == 0:
            await text_channel.edit(
                name=text_channel.name.replace("🔄", "💤").replace("🎯", "💤")
            )

    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.command()
    @_in_ctf_channel()
    async def solve(
        self, interaction: discord.Interaction, members: Optional[str] = None
    ) -> None:
        """Mark the challenge as solved.

        Args:
            interaction: The interaction that triggered this command.
            members: List of member mentions who contributed in solving the challenge.
        """
        await interaction.response.defer()

        challenge = get_challenge_info(thread=interaction.channel_id)
        if challenge is None:
            # If we didn't find any challenge that corresponds to the thread from which
            # the command was run, then we're probably in the wrong thread.
            await interaction.followup.send(
                "You may only run this command in the thread associated to the "
                "challenge.",
                ephemeral=True,
            )
            return

        # If the challenge was already solved.
        if challenge["solved"]:
            await interaction.followup.send(
                "This challenge was already solved.", ephemeral=True
            )
            return

        challenge["solved"] = True
        challenge["solve_time"] = int(datetime.now().timestamp())

        solvers = await parse_challenge_solvers(interaction, challenge, members)

        ctf = get_ctf_info(guild_category=interaction.channel.category_id)
        solves_channel = interaction.client.get_channel(ctf["guild_channels"]["solves"])
        embed = discord.Embed(
            title="🎉 Challenge solved!",
            description=(
                f"**{', '.join(solvers)}** just solved "
                f"**{challenge['name']}** from the "
                f"**{challenge['category']}** category!"
            ),
            colour=discord.Colour.dark_gold(),
            timestamp=datetime.now(),
        ).set_thumbnail(url=interaction.user.display_avatar.url)
        solve_announcement = await solves_channel.send(embed=embed)

        challenge["solve_announcement"] = solve_announcement.id
        MONGO[DBNAME][CHALLENGE_COLLECTION].update_one(
            {"_id": challenge["_id"]},
            {
                "$set": {
                    "solved": challenge["solved"],
                    "solve_time": challenge["solve_time"],
                    "solve_announcement": challenge["solve_announcement"],
                    "players": challenge["players"],
                }
            },
        )

        # Disable workon button for this challenge.
        announcements_channel = discord.utils.get(
            interaction.guild.text_channels,
            id=ctf["guild_channels"]["announcements"],
        )
        announcement = await announcements_channel.fetch_message(
            challenge["announcement"]
        )
        await announcement.edit(view=WorkonButton(oid=challenge["_id"], disabled=True))

        await interaction.followup.send("✅ Challenge solved.")
        await interaction.followup.send(
            "💡 Make sure to use `/ctf submit` instead in order to track first bloods.",
            ephemeral=True,
        )

        # We leave editing the channel name till the end since we might get rate
        # limited, causing a sleep that will block this function call.
        await interaction.channel.edit(name=interaction.channel.name.replace("❌", "✅"))

        # Mark the CTF category maxed if all its challenges were solved.
        await mark_if_maxed(interaction.channel.parent, challenge["category"])

    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.command()
    @_in_ctf_channel()
    async def unsolve(self, interaction: discord.Interaction) -> None:
        """Mark the challenge as not solved.

        Args:
            interaction: The interaction that triggered this command.
        """
        await interaction.response.defer()

        challenge = get_challenge_info(thread=interaction.channel_id)
        if challenge is None:
            # If we didn't find any challenge that corresponds to the channel from which
            # the command was run, then we're probably in a non-challenge channel.
            await interaction.followup.send(
                "You may only run this command in the thread associated to the "
                "challenge.",
                ephemeral=True,
            )
            return

        # Check if challenge is already not solved.
        if not challenge["solved"]:
            await interaction.followup.send(
                "This challenge is already marked as not solved.", ephemeral=True
            )
            return

        # Delete the challenge solved announcement we made.
        ctf = get_ctf_info(guild_category=interaction.channel.category_id)
        solves_channel = discord.utils.get(
            interaction.guild.text_channels, id=ctf["guild_channels"]["solves"]
        )
        announcement = await solves_channel.fetch_message(
            challenge["solve_announcement"]
        )
        if announcement:
            await announcement.delete()

        MONGO[DBNAME][CHALLENGE_COLLECTION].update_one(
            {"_id": challenge["_id"]},
            {
                "$set": {
                    "solved": False,
                    "blooded": False,
                    "solve_time": None,
                    "solve_announcement": None,
                }
            },
        )

        # Enable workon button for this challenge.
        announcements_channel = discord.utils.get(
            interaction.guild.text_channels,
            id=ctf["guild_channels"]["announcements"],
        )
        announcement = await announcements_channel.fetch_message(
            challenge["announcement"]
        )
        await announcement.edit(view=WorkonButton(oid=challenge["_id"]))

        await interaction.followup.send("✅ Challenge unsolved.")

        # We leave editing the channel name till the end since we might get rate
        # limited, causing a sleep that will block this function call.
        await interaction.channel.edit(
            name=interaction.channel.name.replace("✅", "❌").replace("🩸", "❌")
        )

        # In case the CTF category was maxed.
        text_channel = interaction.channel.parent
        if text_channel.name.startswith("🎯"):
            await text_channel.edit(name=text_channel.name.replace("🎯", "🔄"))

    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.command()
    @app_commands.autocomplete(name=_challenge_autocompletion_func)  # type: ignore
    @_in_ctf_channel()
    async def workon(self, interaction: discord.Interaction, name: str) -> None:
        """Start working on a challenge and join its thread.

        Args:
            interaction: The interaction that triggered this command.
            name: Challenge name (case insensitive).
        """
        challenge = get_challenge_info(name=name)
        if challenge is None:
            await interaction.response.send_message(
                "No such challenge.", ephemeral=True
            )
            return

        if interaction.user.name in challenge["players"]:
            await interaction.response.send_message(
                "You're already working on this challenge.", ephemeral=True
            )
            return

        if challenge["solved"]:
            await interaction.response.send_message(
                "You can't work on a challenge that has been solved.", ephemeral=True
            )
            return

        challenge_thread = discord.utils.get(
            interaction.guild.threads, id=challenge["thread"]
        )
        await add_challenge_worker(challenge_thread, challenge, interaction.user)

        await interaction.response.send_message(
            f"✅ Added to the `{challenge['name']}` challenge."
        )
        await challenge_thread.send(
            f"{interaction.user.mention} wants to collaborate 🤝"
        )

    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.command()
    @app_commands.autocomplete(name=_challenge_autocompletion_func)  # type: ignore
    @_in_ctf_channel()
    async def unworkon(
        self, interaction: discord.Interaction, name: Optional[str] = None
    ) -> None:
        """Stop working on a challenge and leave its thread (default: current
        thread's challenge).

        Args:
            interaction: The interaction that triggered this command.
            name: Challenge name (case insensitive).
        """
        if name is None:
            challenge = get_challenge_info(thread=interaction.channel_id)
            if challenge is None:
                await interaction.response.send_message(
                    (
                        "Run this command from within a challenge thread, "
                        "or provide the name of the challenge you wish to stop "
                        "working on."
                    ),
                    ephemeral=True,
                )
                return
        else:
            challenge = get_challenge_info(name=name)
            if challenge is None:
                await interaction.response.send_message(
                    "No such challenge.", ephemeral=True
                )
                return

        if interaction.user.name not in challenge["players"]:
            await interaction.response.send_message(
                "You're not working on this challenge in the first place.",
                ephemeral=True,
            )
            return

        challenge_thread = discord.utils.get(
            interaction.guild.threads, id=challenge["thread"]
        )
        await remove_challenge_worker(challenge_thread, challenge, interaction.user)
        await challenge_thread.send(
            f"{interaction.user.name} left you alone, what a chicken! 🐥"
        )

        await interaction.response.send_message(
            f"✅ Removed from the `{challenge['name']}` challenge.", ephemeral=True
        )

    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.command()
    @app_commands.autocomplete(name=_ctf_autocompletion_func)  # type: ignore
    async def status(
        self,
        interaction: discord.Interaction,
        name: str = None,
        mode: Optional[CTFStatusMode] = CTFStatusMode.active,
    ) -> None:
        """Display CTF status.

        Args:
            interaction: The interaction that triggered this command.
            name: CTF name (default: current channel's CTF).
            mode: Whether to display all challenges or only those not
                solved (default: active).
        """
        await interaction.response.defer()

        ctf = get_ctf_info(guild_category=interaction.channel.category_id)

        # CTF name wasn't provided, and we're outside a CTF category channel, so
        # we display statuses of all running CTFs.
        if ctf is None and name is None:
            ctfs = MONGO[DBNAME][CTF_COLLECTION].find(
                {"archived": False, "ended": False}
            )
        # CTF name wasn't provided, and we're inside a CTF category channel, so
        # we display status of the CTF related to this category channel.
        elif name is None:
            ctfs = [ctf]
        # CTF name was provided, and we're inside a CTF category channel, so
        # the priority here is for the provided CTF name.
        # - or -
        # CTF name was provided, and we're outside a CTF category channel, so
        # we display status of the requested CTF only.
        else:
            ctfs = get_ctf_info(name=name, archived=False, ended=False)
            if ctfs is None:
                await interaction.followup.send("No such CTF.", ephemeral=True)
                return
            ctfs = [ctfs]

        no_running_ctfs = True
        for ctf in ctfs:
            no_running_ctfs = False
            # Let the user know that they should join the CTF first to see its
            # details in case the command was run from outside the CTF channel.
            if ctf["guild_category"] != interaction.channel.category_id:
                embed = discord.Embed(
                    title=f"{ctf['name']} status",
                    colour=discord.Colour.blue(),
                    description=(
                        "You must run the command in one of the CTF's channels to see "
                        "its details."
                    ),
                )
                await interaction.followup.send(embed=embed)
                continue

            # Otherwise, display details about the CTF status.
            challenges = ctf["challenges"]
            if not challenges:
                embed = discord.Embed(
                    title=f"{ctf['name']} status",
                    description="No challenges added yet.",
                    colour=discord.Colour.blue(),
                )
                await interaction.followup.send(embed=embed)
                return

            embed = None
            num_fields = 0
            for idx, challenge_id in enumerate(challenges):
                # If we reached Discord's maximum number of fields per
                # embed, we send the previous one and create a new one.
                if num_fields % 25 == 0:
                    if num_fields != 0:
                        await interaction.followup.send(embed=embed)
                        embed = None

                    if embed is None:
                        embed = discord.Embed(
                            title=f"{ctf['name']} status",
                            colour=discord.Colour.blue(),
                        )

                challenge = get_challenge_info(_id=challenge_id)
                if challenge["solved"] and mode == CTFStatusMode.all:
                    icon = "🩸" if challenge["blooded"] else "✅"
                    solve_time = datetime.utcfromtimestamp(
                        challenge["solve_time"]
                    ).strftime(DATE_FORMAT)
                    embed.add_field(
                        name=f"{icon} {challenge['name']} ({challenge['category']})",
                        value=(
                            "```diff\n"
                            f"+ Solver{['', 's'][len(challenge['players']) > 1]}:"
                            f" {', '.join(challenge['players']).strip()}\n"
                            f"+ Date: {solve_time}\n"
                            "```"
                        ),
                        inline=False,
                    )
                    num_fields += 1
                elif not challenge["solved"]:
                    workers = (
                        "```diff\n- No workers.\n```"
                        if len(challenge["players"]) == 0
                        else (
                            "```fix\n"
                            f"! Worker{['', 's'][len(challenge['players']) > 1]}:"
                            f" {', '.join(challenge['players']).strip()}\n"
                            "```"
                        )
                    )
                    embed.add_field(
                        name=(
                            f"❌ {idx + 1:2} - "
                            f"{challenge['name']} ({challenge['category']})"
                        ),
                        value=workers,
                        inline=False,
                    )
                    num_fields += 1

            # Send the remaining embed.
            await interaction.followup.send(embed=embed)

        if no_running_ctfs:
            if name is None:
                await interaction.followup.send("No running CTFs.", ephemeral=True)
            else:
                await interaction.followup.send("No such CTF.", ephemeral=True)

    @app_commands.checks.bot_has_permissions(manage_messages=True)
    @app_commands.command()
    @_in_ctf_channel()
    async def addcreds(self, interaction: discord.Interaction, url: str) -> None:
        """Add credentials for the current CTF.

        Args:
            interaction: The interaction that triggered this command.
            url: Base URL of the CTF platform.
        """
        ctx = PlatformCTX(base_url=strip_url_components(url.strip()))
        try:
            platform = await match_platform(ctx)
        except aiohttp.client_exceptions.InvalidURL:
            await interaction.response.send_message(
                "The provided URL was invalid.",
                ephemeral=True,
            )
            return
        except ClientError:
            await interaction.response.send_message(
                "Could not communicate with the CTF platform, please try again.",
                ephemeral=True,
            )
            return

        modal = await create_credentials_modal_for_platform(url, platform, interaction)
        if modal is not None:
            await interaction.response.send_modal(modal)

    @app_commands.checks.bot_has_permissions(manage_messages=True)
    @app_commands.command()
    @_in_ctf_channel()
    async def showcreds(self, interaction: discord.Interaction) -> None:
        """Show credentials for the current CTF.

        Args:
            interaction: The interaction that triggered this command.
        """
        ctf = get_ctf_info(guild_category=interaction.channel.category_id)
        if (message := ctf["credentials"].get("_message")) is None:
            await interaction.response.send_message(
                "No credentials set for this CTF.", ephemeral=True
            )
            return
        await interaction.response.send_message(message)

    @app_commands.checks.bot_has_permissions(manage_messages=True)
    @app_commands.command()
    @_in_ctf_channel()
    async def pull(self, interaction: discord.Interaction) -> None:
        """Pull challenges from the platform.

        Args:
            interaction: The interaction that triggered this command.
        """
        if interaction.client.challenge_puller_is_running:
            await interaction.response.send_message(
                "Challenge puller is already running.",
                ephemeral=True,
            )
            return
        interaction.client.challenge_puller.restart()
        await interaction.response.send_message("✅ Started challenge puller.")

    @app_commands.command()
    @_in_ctf_channel()
    async def submit(
        self, interaction: discord.Interaction, members: Optional[str] = None
    ) -> None:
        """Submit a flag to the platform.

        Args:
            interaction: The interaction that triggered this command.
            members: List of member mentions who contributed in solving the challenge.
        """
        await interaction.response.send_modal(FlagSubmissionForm(members=members))

    @app_commands.command()
    @_in_ctf_channel()
    async def scoreboard(self, interaction: discord.Interaction) -> None:
        """Display scoreboard for the current CTF.

        Args:
            interaction: The interaction that triggered this command.
        """
        await interaction.response.defer()

        ctf = get_ctf_info(guild_category=interaction.channel.category_id)

        await send_scoreboard(ctf, interaction=interaction)

    @app_commands.command()
    @_in_ctf_channel()
    async def remaining(self, interaction: discord.Interaction) -> None:
        """Show remaining time for the CTF.

        Args:
            interaction: The interaction that triggered this command.
        """
        await interaction.response.defer()

        ctf = get_ctf_info(guild_category=interaction.channel.category_id)

        for scheduled_event in interaction.guild.scheduled_events:
            if scheduled_event.name == ctf["name"]:
                break
        else:
            await interaction.followup.send(
                "🏁 This CTF has ended or we don't know its end time."
            )
            return

        await interaction.followup.send(
            f"⏲️ This CTF ends <t:{scheduled_event.end_time.timestamp():.0f}:R>."
        )

    @app_commands.command()
    @_in_ctf_channel()
    async def register(
        self,
        interaction: discord.Interaction,
        url: str,
    ) -> None:
        """Register a team account in the platform.

        Args:
            interaction: The interaction that triggered this command.
            url: Platform base url.
        """
        url = strip_url_components(url.strip())
        ctx: PlatformCTX = PlatformCTX(base_url=url)
        try:
            platform = await match_platform(ctx)
        except aiohttp.client_exceptions.InvalidURL:
            await interaction.response.send_message(
                "The provided URL was invalid.",
                ephemeral=True,
            )
            return
        except ClientError:
            await interaction.response.send_message(
                "Could not communicate with the CTF platform, please try again.",
                ephemeral=True,
            )
            return

        form = await create_credentials_modal_for_platform(
            url=url, platform=platform, interaction=interaction, is_registration=True
        )

        if not form:
            await interaction.response.send_message(
                "Invalid URL set for this CTF, or platform isn't supported.",
                ephemeral=True,
            )
            return

        await interaction.response.send_modal(form)

    @app_commands.checks.bot_has_permissions(manage_channels=True, manage_roles=True)
    @app_commands.checks.has_permissions(manage_channels=True, manage_roles=True)
    @app_commands.command()
    async def exportchat(self, interaction: discord.Interaction) -> None:
        """Export CTF chat logs to a static site.

        Args:
            interaction: The interaction that triggered this command.
        """

        async def _handle_process(process: asyncio.subprocess.Process):
            _, _ = await process.communicate()
            channel, _, _ = self._chat_export_tasks.pop(0)
            message = (
                "Chat export task finished successfully, "
                f"{len(self._chat_export_tasks)} items remaining in the queue."
            )
            try:
                await channel.send(content=message)
            except discord.errors.HTTPException as err:
                _log.error("Failed to send message: %s", err)

            _log.info(message)
            if len(self._chat_export_tasks) == 0:
                return

            _, tmp, output_dirname = self._chat_export_tasks[0]
            asyncio.create_task(
                _handle_process(
                    await asyncio.create_subprocess_exec(
                        "chat_exporter",
                        tmp,
                        output_dirname,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )
            )

        await interaction.response.defer()

        guild_category = interaction.channel.category
        exportable = set()
        for channel in guild_category.text_channels:
            exportable.add(channel.id)

            for thread in channel.threads:
                exportable.add(thread.id)

            for private in (True, False):
                async for thread in channel.archived_threads(
                    private=private, limit=None
                ):
                    exportable.add(thread.id)

        tmp = tempfile.mktemp()
        output_dirname = (
            f"[{guild_category.id}] {guild_category.name.replace('/', '_')}"
        )
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(map(str, exportable)))

        self._chat_export_tasks.append((interaction.channel, tmp, output_dirname))
        if len(self._chat_export_tasks) == 1:
            asyncio.create_task(
                _handle_process(
                    await asyncio.create_subprocess_exec(
                        "chat_exporter",
                        tmp,
                        output_dirname,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )
            )

        await interaction.followup.send(
            "Export task started, chat logs will be available shortly.", silent=True
        )
