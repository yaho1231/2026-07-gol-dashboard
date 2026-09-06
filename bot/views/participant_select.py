import discord
import httpx
import asyncio
import io
from bot.config import BACKEND_URL
from bot.services.clash_api import generate_custom_code
from bot.services.deeplol_images import tier_emblem_url
from bot.presence import presence

BOT_MESSAGE_DELETE_AFTER_SECONDS = 300

_DIVISION_ROMAN = {1: "I", 2: "II", 3: "III", 4: "IV"}


def _main_account(player: dict) -> dict | None:
    accounts = player.get("accounts") or []
    if not accounts:
        return None
    return next((a for a in accounts if a.get("main_account")), accounts[0])


def _tier_text(account: dict | None) -> str:
    """솔로랭크 티어를 'EMERALD IV' 형태의 짧은 라벨로. 언랭/무정보는 '언랭'."""
    if not account:
        return "언랭"
    tier = account.get("solo_tier")
    if not tier or str(tier).upper() == "UNRANKED":
        return "언랭"
    division = _DIVISION_ROMAN.get(account.get("solo_division"), "")
    return f"{tier} {division}".strip()

BALANCE_MODE_OPTIONS = [
    ("mmr", "평균 MMR", "내전 MMR 평균을 맞춰 팀을 구성합니다."),
    ("tier", "티어", "솔로랭크 티어를 중심으로 팀을 구성합니다."),
    ("lane", "주 라인", "주 라인 중복을 최소화하며 MMR을 맞춥니다."),
]

async def delete_message_later(message: discord.Message, delay: int = BOT_MESSAGE_DELETE_AFTER_SECONDS):
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def send_response_autodelete(interaction: discord.Interaction, *args, **kwargs):
    """오류·안내 메시지(ephemeral 포함)를 보낸 뒤 일정 시간 후 자동 삭제한다."""
    await interaction.response.send_message(*args, **kwargs)
    try:
        message = await interaction.original_response()
    except discord.HTTPException:
        return
    asyncio.create_task(delete_message_later(message))


async def send_followup_autodelete(interaction: discord.Interaction, *args, **kwargs):
    message = await interaction.followup.send(*args, **kwargs)
    if message is not None:
        asyncio.create_task(delete_message_later(message))
    return message

class BalanceModeSelect(discord.ui.Select):
    def __init__(self, row: int):
        options = [
            discord.SelectOption(label=label, value=value, description=desc, default=(value == "mmr"))
            for value, label, desc in BALANCE_MODE_OPTIONS
        ]
        super().__init__(
            placeholder="팀 구성 방식 선택 (기본: 평균 MMR)...",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="select_balance_mode",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.balance_mode = self.values[0]
        label = next(l for v, l, _ in BALANCE_MODE_OPTIONS if v == self.view.balance_mode)
        await send_response_autodelete(
            interaction,
            f"팀 구성 방식: **{label}**",
            ephemeral=True,
        )

class VoiceParticipantSelect(discord.ui.Select):
    """음성 채널 멤버 셀렉트 — 등록 플레이어 목록을 못 받아왔을 때의 폴백 전용."""

    def __init__(self, members: list[discord.Member], row: int = 0):
        options = []
        for member in members:
            if len(options) >= 25:
                break
            options.append(discord.SelectOption(
                label=member.display_name,
                value=str(member.id),
                description=f"계정명: {member.name}"
            ))

        super().__init__(
            placeholder="음성 채널 멤버 중 선택...",
            min_values=0,
            max_values=min(len(options), 10),
            options=options,
            custom_id="select_voice_member",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selections[self.custom_id] = list(self.values)
        await self.view.announce_selection(interaction)


class MemberSelect(discord.ui.UserSelect):
    """디스코드 유저 검색 셀렉트 — 등록 플레이어 목록을 못 받아왔을 때의 폴백 전용.

    목록을 디스코드 클라이언트가 자기 멤버 캐시로 채우기 때문에 서버 멤버 전원이
    보이지 않고, 검색도 서버 닉네임과 안 맞을 때가 많다. 그래서 평상시에는
    RegisteredPlayerSelect(봇이 직접 옵션 제공)를 쓴다."""

    def __init__(self, row: int):
        super().__init__(
            placeholder="추가로 참가시킬 플레이어를 선택하세요 (서버 전체 검색 가능)...",
            min_values=0,
            max_values=10,
            custom_id="user_select_member",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selections[self.custom_id] = [
            str(user.id) for user in self.values if not user.bot
        ]
        await self.view.announce_selection(interaction)


class RegisteredPlayerSelect(discord.ui.Select):
    """등록 플레이어 셀렉트 — 매칭 가능한 전원을 봇이 직접 옵션으로 제공한다.

    유저 검색(UserSelect)은 클라이언트 캐시에 있는 멤버만 보여줘 일부가 누락되는
    문제가 있었다. 어차피 미등록 유저는 매치메이킹이 불가능하므로, 백엔드의 등록
    플레이어 목록(25명 초과 시 페이지 분할)을 그대로 보여주고 음성 채널에 있는
    등록 플레이어는 미리 선택해둔다."""

    def __init__(self, players: list[dict], preselected_ids: set[str], row: int,
                 page: int, total_pages: int):
        options = []
        for player in players:
            uid = str(player["discord_user_id"])
            options.append(discord.SelectOption(
                label=str(player.get("display_name") or uid)[:100],
                value=uid,
                description=f"MMR {player.get('mmr', '?')} · {_tier_text(_main_account(player))}",
                default=uid in preselected_ids,
            ))
        placeholder = "참가할 등록 플레이어 선택 (체크/해제)..."
        if total_pages > 1:
            placeholder += f" ({page}/{total_pages})"
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=len(options),
            options=options,
            custom_id=f"select_registered_p{page}",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        self.view.selections[self.custom_id] = list(self.values)
        await self.view.announce_selection(interaction)


def build_team_embed(result: dict, ai_commentary: str | None = None) -> discord.Embed:
    blue = result.get("blue_team", {})
    red = result.get("red_team", {})
    mmr_diff = result.get("mmr_difference", 0.0)
    mode_label = result.get("balance_mode_label", "평균 MMR")

    embed = discord.Embed(
        title="⚔️ GOL League Inhouse Matchup ⚔️",
        description=(
            f"🎛️ **구성 방식** {mode_label}\n"
            f"⚖️ **양 팀 평균 MMR 차이** `{mmr_diff:.1f}`"
        ),
        color=discord.Color.blurple(),
    )

    def _team_lines(players: list[dict]) -> str:
        lines = []
        for p in players:
            acc = _main_account(p)
            riot_name = f"{acc['riot_game_name']}#{acc['riot_tag']}" if acc else "미연결"
            lines.append(
                f"`{p['mmr']:>4}` **{p['display_name']}** · {_tier_text(acc)}\n"
                f"┗ {riot_name}"
            )
        return "\n".join(lines) or "—"

    embed.add_field(
        name="🔵 블루팀 · 평균 MMR {:.0f}".format(blue.get("avg_mmr") or 0.0),
        value=_team_lines(blue.get("players", [])),
        inline=True,
    )
    embed.add_field(
        name="🔴 레드팀 · 평균 MMR {:.0f}".format(red.get("avg_mmr") or 0.0),
        value=_team_lines(red.get("players", [])),
        inline=True,
    )

    # 최고 MMR 플레이어의 티어 엠블럼을 썸네일로 얹어 시각적 앵커를 준다.
    all_players = list(blue.get("players", [])) + list(red.get("players", []))
    if all_players:
        top_player = max(all_players, key=lambda p: p.get("mmr", 0))
        emblem = tier_emblem_url((_main_account(top_player) or {}).get("solo_tier"))
        if emblem:
            embed.set_thumbnail(url=emblem)

    if ai_commentary:
        # 1024자 필드 한도로 나눠 담되, 사용자에겐 모델명 대신 깔끔한 제목만 보인다.
        chunks = [ai_commentary[i:i + 1024] for i in range(0, min(len(ai_commentary), 2048), 1024)]
        for idx, chunk in enumerate(chunks):
            suffix = "" if len(chunks) == 1 else f" ({idx + 1}/{len(chunks)})"
            embed.add_field(name=f"🤖 AI 매치 예측 해설{suffix}", value=chunk, inline=False)
        embed.set_footer(text="AI 해설 · qwen3:8b")
    else:
        embed.set_footer(text="🤖 AI 해설 생성 중...")

    return embed


async def fetch_ai_commentary(client: httpx.AsyncClient, result: dict) -> str:
    commentary_payload = {
        "blue_team": result.get("blue_team"),
        "red_team": result.get("red_team"),
        "mmr_difference": result.get("mmr_difference", 0.0),
        "balance_mode": result.get("balance_mode", "mmr"),
    }
    resp = await client.post(
        f"{BACKEND_URL}/api/matchmake/commentary",
        json=commentary_payload,
        timeout=150.0,
    )
    if resp.status_code == 200:
        return resp.json().get("ai_commentary", "AI 코멘터리 분석에 실패했습니다.")
    return "AI 코멘터리 분석에 실패했습니다."


class MatchmakeView(discord.ui.View):
    def __init__(self, voice_members: list[discord.Member],
                 registered_players: list[dict] | None = None):
        super().__init__(timeout=600.0)
        self.voice_members = voice_members
        self.registered_players = registered_players or []
        # 셀렉트별 현재 선택값. 최종 참가자 = 모든 셀렉트 선택값의 합집합(selected_users).
        # 셀렉트가 값을 직접 append 하던 기존 방식은 체크 해제가 반영되지 않는 버그가 있었다.
        self.selections: dict[str, list[str]] = {}
        self.balance_mode = "mmr"
        self.name_by_id: dict[str, str] = {str(m.id): m.display_name for m in voice_members}

        row = 0
        voice_ids = {str(m.id) for m in voice_members}
        valid_players = [p for p in self.registered_players if p.get("discord_user_id")]
        if valid_players:
            for player in valid_players:
                self.name_by_id[str(player["discord_user_id"])] = str(player.get("display_name") or "?")
            players_sorted = sorted(valid_players, key=lambda p: str(p.get("display_name") or ""))
            # 셀렉트 1개당 25명, 최대 3페이지(75명) — 남는 행은 밸런스 모드 + 버튼용.
            pages = [players_sorted[i:i + 25] for i in range(0, len(players_sorted), 25)][:3]
            for i, page_players in enumerate(pages):
                select = RegisteredPlayerSelect(
                    page_players, voice_ids, row=row, page=i + 1, total_pages=len(pages))
                self.add_item(select)
                defaults = [opt.value for opt in select.options if opt.default]
                if defaults:
                    self.selections[select.custom_id] = defaults
                row += 1
        else:
            # 등록 플레이어 목록을 못 받아온 경우(백엔드 장애 등)의 폴백.
            if voice_members:
                self.add_item(VoiceParticipantSelect(voice_members, row=row))
                row += 1
            self.add_item(MemberSelect(row=row))
            row += 1

        self.add_item(BalanceModeSelect(row=row))

    @property
    def selected_users(self) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for values in self.selections.values():
            for uid in values:
                if uid not in seen:
                    seen.add(uid)
                    ordered.append(uid)
        return ordered

    def _display_name(self, guild: discord.Guild | None, uid: str) -> str:
        if uid in self.name_by_id:
            return self.name_by_id[uid]
        member = guild.get_member(int(uid)) if guild else None
        return member.display_name if member else f"ID: {uid}"

    async def announce_selection(self, interaction: discord.Interaction) -> None:
        names = [self._display_name(interaction.guild, uid) for uid in self.selected_users]
        await send_response_autodelete(
            interaction,
            f"현재 선택된 플레이어 ({len(names)}명): " + (", ".join(names) if names else "없음"),
            ephemeral=True,
        )

    @discord.ui.button(label="선택 초기화 🔄", style=discord.ButtonStyle.secondary, custom_id="btn_reset_selection", row=4)
    async def reset_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 내부 목록만 비우면 셀렉트의 체크 표시가 화면에 남으므로,
        # View 를 통째로 새로 달아 초기 상태(음성 채널 미리선택 포함)로 되돌린다.
        fresh = MatchmakeView(self.voice_members, self.registered_players)
        await interaction.response.edit_message(view=fresh)
        self.stop()

    @discord.ui.button(label="팀 생성하기 🎮", style=discord.ButtonStyle.green, custom_id="btn_matchmake", row=4)
    async def matchmake_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        player_count = len(self.selected_users)
        if player_count != 10:
            await send_response_autodelete(
                interaction,
                f"❌ 팀을 매칭하려면 정확히 10명의 플레이어가 필요합니다. (현재 {player_count}명 선택됨)",
                ephemeral=True
            )
            return

        original_message = interaction.message
        mode_label = next(l for v, l, _ in BALANCE_MODE_OPTIONS if v == self.balance_mode)
        await interaction.response.edit_message(
            content=f"⏳ **{mode_label}** 기준으로 팀 밸런스를 맞추는 중입니다...\n잠시만 기다려주세요.",
            view=None,
        )
        self.stop()

        payload = {
            "discord_user_ids": self.selected_users,
            "balance_mode": self.balance_mode,
        }

        async with presence("정보 불러오는중.."), httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{BACKEND_URL}/api/matchmake",
                    json=payload,
                    timeout=15.0,
                )

                if response.status_code != 200:
                    err_msg = response.json().get("detail", "알 수 없는 에러가 발생했습니다.")
                    await original_message.edit(
                        content=(
                            f"❌ **팀 매칭 실패:** {err_msg}\n"
                            "선택한 유저들이 백엔드 DB에 정상적으로 등록되어 있는지 확인한 뒤 "
                            "`/팀생성` 명령으로 다시 시도해주세요."
                        ),
                        view=None,
                    )
                    return

                result = response.json()

                try:
                    await original_message.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

                embed = build_team_embed(result)

                # 로스터 배너 카드 — 실패해도 임베드만으로 진행한다.
                card_file = None
                try:
                    card_resp = await client.post(
                        f"{BACKEND_URL}/api/matchmake/roster-card",
                        json=result,
                        timeout=60.0,  # 첫 호출은 챔피언 아이콘 다운로드로 수 초 걸릴 수 있다.
                    )
                    if card_resp.status_code == 200:
                        card_file = discord.File(
                            io.BytesIO(card_resp.content), filename="gol_roster.png"
                        )
                except Exception:
                    card_file = None

                if card_file:
                    result_message = await interaction.channel.send(embed=embed, file=card_file)
                else:
                    result_message = await interaction.channel.send(embed=embed)
                asyncio.create_task(delete_message_later(result_message))

                try:
                    ai_commentary = await fetch_ai_commentary(client, result)
                    updated_embed = build_team_embed(result, ai_commentary=ai_commentary)
                    await result_message.edit(embed=updated_embed, view=CustomCodeView())
                except Exception:
                    fail_embed = build_team_embed(result, ai_commentary="AI 코멘터리 생성에 실패했습니다.")
                    await result_message.edit(embed=fail_embed, view=CustomCodeView())

            except Exception as e:
                await original_message.edit(
                    content=(
                        f"❌ **API 통신 연결 실패:** {str(e)}\n"
                        "`/팀생성` 명령으로 다시 시도해주세요."
                    ),
                    view=None,
                )


class CustomCodeView(discord.ui.View):
    """팀 결과 임베드 아래에 붙는 커스텀 코드 생성 버튼."""

    def __init__(self):
        super().__init__(timeout=600.0)

    @discord.ui.button(
        label="🎮 커스텀 코드 생성",
        style=discord.ButtonStyle.blurple,
        custom_id="btn_generate_custom_code",
    )
    async def generate_code_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=False, thinking=True)
        try:
            code = await generate_custom_code()
            button.disabled = True
            await interaction.message.edit(view=self)
            await send_followup_autodelete(
                interaction,
                f"🎮 **커스텀 게임 코드가 생성되었습니다!**\n"
                f"```\n{code}\n```\n"
                f"롤 클라이언트 → 커스텀 게임 → 코드로 참가 에서 위 코드를 입력하세요.",
                ephemeral=False,
            )
        except Exception as e:
            await send_followup_autodelete(
                interaction,
                f"❌ **커스텀 코드 생성 실패:** {e}\n잠시 후 다시 시도해주세요.",
                ephemeral=True,
            )
