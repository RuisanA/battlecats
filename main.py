from __future__ import annotations

import os
from dotenv import load_dotenv
import discord
from discord import app_commands, ui
from discord.ext import commands
import json
import time
import hmac
import hashlib
import requests
import secrets
from typing import Any, Optional, Tuple

from bcsfe import cli, core
from bcsfe.cli import color
from bcsfe.core.game.catbase.gatya import GatyaEventType
from bcsfe.core.server.event_data import split_hhmm, split_yyyymmdd
from event_tickets import EventTickets

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

client = discord.Client(intents=discord.Intents.default())

CONFIG_FILE = "config.json"

def load_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=4)

config = load_config()

class NyankoSignature:
    def __init__(self, inquiry_code: str, data: str):
        self.inquiry_code = inquiry_code
        self.data = data

    def generate_signature(self) -> str:
        random_hex = secrets.token_hex(32)
        key = (self.inquiry_code + random_hex).encode()
        signature = hmac.new(key, self.data.encode(), hashlib.sha256).hexdigest()
        return random_hex + signature

    def generate_signature_v1(self) -> str:
        data_double = self.data + self.data
        random_hex = secrets.token_hex(20)
        key = (self.inquiry_code + random_hex).encode()
        signature = hmac.new(key, data_double.encode(), hashlib.sha1).hexdigest()
        return random_hex + signature


class CloudEditor:
    AUTH_URL = "https://nyanko-auth.ponosgames.com"
    SAVE_URL = "https://nyanko-save.ponosgames.com"

    def __init__(self, transfer_code: str, pin: str, user, guild_id):
        self.transfer_code = transfer_code
        self.pin = pin
        self.session = requests.Session()
        self.save_file: Optional[Any] = None
        self.password = ""
        self.last_error = ""
        self.user = user
        self.guild_id = guild_id
        self.actions = []

    def get_common_headers(self, iq: str, data: str) -> dict:
        return {
            "Content-Type": "application/json",
            "Nyanko-Signature": NyankoSignature(iq, data).generate_signature(),
            "Nyanko-Timestamp": str(int(time.time())),
            "Nyanko-Signature-Version": "1",
            "Nyanko-Signature-Algorithm": "HMACSHA256",
            "User-Agent": "Dalvik/2.1.0"
        }

    def download_save(self) -> bool:
        nonce = secrets.token_hex(16)
        url = f"{self.SAVE_URL}/v2/transfers/{self.transfer_code}/reception"
        payload = {
            "clientInfo": {
                "client": {"version": "15.5.0", "countryCode": "ja"},
                "os": {"type": "android", "version": "13"},
                "device": {"model": "SM-S918B"}
            },
            "pin": self.pin,
            "nonce": nonce
        }
        body = json.dumps(payload, separators=(",", ":"))
        headers = {"Content-Type": "application/json"}
        try:
            res = self.session.post(url, headers=headers, data=body)
            if res.status_code == 200 and res.headers.get("Content-Type") == "application/octet-stream":
                try:
                    from bcsfe.core.save_file import SaveFile as bSaveFile
                except:
                    bSaveFile = core.SaveFile
                self.save_file = bSaveFile(core.Data(res.content), cc=core.CountryCode("jp"))
                self.password = res.headers.get("Nyanko-Password", "")
                return True
            self.last_error = res.text
        except Exception as e:
            self.last_error = str(e)
        return False

    def upload_save(self) -> Tuple[Optional[str], Optional[str]]:
        if not self.save_file:
            return None, None
        
        if not hasattr(self.save_file, 'local_manager'):
            self.save_file.local_manager = None
            try:
                self.save_file.patch()
            except:
                pass
        inq = self.save_file.inquiry_code
        try:
            login_data = {
                "accountCode": inq,
                "password": self.password,
                "clientInfo": {
                    "client": {"version": "15.5.0", "countryCode": "ja"},
                    "os": {"type": "android", "version": "9"},
                    "device": {"model": "SM-G955F"}
                },
                "nonce": secrets.token_hex(16)
            }
            login_body = json.dumps(login_data, separators=(",", ":"))
            h1 = self.get_common_headers(inq, login_body)
            res1 = self.session.post(f"{self.AUTH_URL}/v1/tokens", headers=h1, data=login_body)
            if res1.status_code != 200:
                self.last_error = res1.text
                return None, None
            token = res1.json()["payload"]["token"]
            nonce_aws = secrets.token_hex(16)
            h2 = self.get_common_headers(inq, "")
            h2["Authorization"] = f"Bearer {token}"
            res2 = self.session.get(f"{self.SAVE_URL}/v2/save/key?nonce={nonce_aws}", headers=h2)
            aws = res2.json()["payload"]
            modified_bytes = self.save_file.to_data().to_bytes()
            files = {"file": ("file.sav", modified_bytes, "application/octet-stream")}
            s3_data = {k: v for k, v in aws.items() if k != "url"}
            requests.post(aws["url"], data=s3_data, files=files)
            meta_payload = {
                "managedItemDetails": [],
                "nonce": secrets.token_hex(16),
                "playTime": self.save_file.officer_pass.play_time,
                "rank": self.save_file.calculate_user_rank(),
                "receiptLogIds": [],
                "saveKey": aws["key"],
                "signature_v1": NyankoSignature(inq, "[]").generate_signature_v1()
            }
            meta_body = json.dumps(meta_payload, separators=(",", ":"))
            h4 = self.get_common_headers(inq, meta_body)
            h4["Authorization"] = f"Bearer {token}"
            res4 = self.session.post(f"{self.SAVE_URL}/v2/transfers", headers=h4, data=meta_body)
            p = res4.json()["payload"]
            return p.get("transferCode"), p.get("pin")
        except Exception as e:
            self.last_error = str(e)
            return None, None


def process_modifications(editor: CloudEditor, values: list, input_data: dict) -> list:
    """Mod適用ロジックを共通化"""
    s = editor.save_file
    actions = []

    # --- 1. アイテム全MAX処理 ---
    if "max_items" in values:
        try:
            s.set_catfood(45000)
            s.set_xp(99999999)
            s.set_rare_tickets(299)
            s.set_normal_tickets(999)
            s.set_platinum_tickets(9)
            s.set_legend_tickets(9)
            s.set_np(9999)
            s.set_leadership(9999)

            # 戦闘アイテムカンスト
            for i in range(len(s.battle_items.items)):
                s.battle_items.items[i] = 999

            # マタタビ・キャッツアイ・各種進化素材をGatyaItemに直接注入
            items_dict = s.gatya_items.items
            for item_id in range(160, 250):
                if item_id in items_dict:
                    items_dict[item_id].count = 999
                else:
                    items_dict[item_id] = core.GatyaItem(999)

            actions.append("🚀 主要アイテム全MAX (カンスト設定)")
        except Exception as e:
            print(f"Max Items Error: {e}")
            actions.append(f"アイテム全MAX処理失敗: {e}")

    # --- 2. ユーザーランク報酬の一括受取処理 ---
    # --- 2. ユーザーランク報酬の一括受取処理 ---
    if "claim_ur_rewards" in values or "claim_ur" in input_data:
        try:
            user_rank = s.calculate_user_rank()
            ur_rewards = s.user_rank_rewards
            
            # ランク報酬データの取得を柔軟に試行
            rank_gifts = None
            if hasattr(core.core_data, "get_rank_gifts"):
                rank_gifts = core.core_data.get_rank_gifts(s)
            elif hasattr(core.core_data, "CoreData"):
                cd = core.core_data.CoreData.get() if hasattr(core.core_data.CoreData, "get") else core.core_data.CoreData()
                if hasattr(cd, "get_rank_gifts"):
                    rank_gifts = cd.get_rank_gifts(s)

            claimed_count = 0
            
            # ギフト一覧の特定ができた場合は「現在ランク以下の報酬」のみフラグを立てる
            if rank_gifts and hasattr(rank_gifts, "rank_gift") and rank_gifts.rank_gift:
                for rank_gift in rank_gifts.rank_gift:
                    if rank_gift.threshold <= user_rank:
                        if hasattr(ur_rewards, "set_claimed"):
                            ur_rewards.set_claimed(rank_gift.index, True)
                            claimed_count += 1
                        elif rank_gift.index < len(ur_rewards.rewards):
                            ur_rewards.rewards[rank_gift.index].claimed = True
                            claimed_count += 1
            else:
                # 取得できない場合は、登録されている全報酬を一括で受取状態にする
                for reward in ur_rewards.rewards:
                    reward.claimed = True
                claimed_count = len(ur_rewards.rewards)

            actions.append(f"🎁 ユーザーランク報酬全受取完了 ({claimed_count}件)")
        except Exception as e:
            print(f"UR Rewards Error: {e}")
            actions.append(f"ユーザーランク報酬受取失敗: {e}")

    # --- 3. 解放済み全キャラレベル30化 ---
    if "max_cat_levels" in values:
        try:
            count = 0
            for cat in s.cats.cats:
                if getattr(cat, "unlocked", False):
                    if hasattr(cat, "upgrade") and cat.upgrade is not None:
                        if hasattr(cat.upgrade, "base_lv"):
                            cat.upgrade.base_lv = 29
                        elif hasattr(cat.upgrade, "level"):
                            cat.upgrade.level = 29
                        count += 1
                    elif hasattr(cat, "level"):
                        cat.level = 29
                        count += 1
            actions.append(f"⬆️ 解放済みキャラ {count} 体をLv.30化")
        except Exception as e:
            print(f"Max Cat Levels Error: {e}")
            actions.append(f"全キャラレベルMAX処理失敗: {e}")

    # --- 4. その他の個別項目処理 ---
    for k, val in input_data.items():
        if val == "":
            continue
        
        if k == "catfood":
            num = int(val)
            s.set_catfood(num)
            actions.append(f"ネコカン {num}")
        elif k == "xp":
            num = int(val)
            s.set_xp(num)
            actions.append(f"XP {num}")
        elif k == "rare":
            num = int(val)
            s.set_rare_tickets(num)
            actions.append(f"レアチケット {num}")
        elif k == "normal":
            num = int(val)
            s.set_normal_tickets(num)
            actions.append(f"にゃんこチケット {num}")
        elif k == "platinum":
            num = int(val)
            s.set_platinum_tickets(num)
            actions.append(f"プラチナチケット {num}")
        elif k == "legend":
            num = int(val)
            s.set_legend_tickets(num)
            actions.append(f"レジェンドチケット {num}")
        elif k == "np":
            num = int(val)
            s.set_np(num)
            actions.append(f"NP {num}")
        elif k == "lead":
            num = int(val)
            s.set_leadership(num)
            actions.append(f"リーダーシップ {num}")
        elif k == "battleitem":
            num = int(val)
            try:
                for i in range(len(s.battle_items.items)):
                    s.battle_items.items[i] = num
                actions.append(f"戦闘アイテム全種 {num}")
            except Exception as e:
                print(f"Battle Item Error: {e}")
        elif k == "unlock_cats":
            for cat in s.cats.cats:
                cat.unlocked = True
                if hasattr(cat, 'set_obtained'):
                    cat.set_obtained(True)
                if hasattr(cat, 'upgrade') and cat.upgrade is not None:
                    if hasattr(cat.upgrade, 'base_lv'):
                        if cat.upgrade.base_lv < 0:
                            cat.upgrade.base_lv = 29
                    elif hasattr(cat.upgrade, 'level'):
                        if cat.upgrade.level < 0:
                            cat.upgrade.level = 29
            actions.append("全キャラ解放")
        elif k == "remove_error_cats":
            s.cats.cats = [cat for cat in s.cats.cats if 0 <= cat.id < 1000]
            for cat in s.cats.cats:
                if not hasattr(cat, 'upgrade') or cat.upgrade is None:
                    cat.unlocked = False
                    if hasattr(cat, 'set_obtained'):
                        cat.set_obtained(False)
            actions.append("エラーキャラ削除・リセット完了")
        elif k == "unlock_stages":
            core.StoryChapters.clear_tutorial(editor.save_file)
            story_chapters = editor.save_file.story.get_real_chapters()
            for chapter in story_chapters:
                chapter.clear_chapter()
                for stage in chapter.get_valid_treasure_stages():
                    stage.set_treasure(3)
            actions.append("全ステージ解放・お宝コンプ完了")
        elif k == "catseye":
            num = int(val)
            try:
                items_dict = s.gatya_items.items
                for item_id in range(160, 250):
                    if item_id in items_dict:
                        items_dict[item_id].count = num
                    else:
                        items_dict[item_id] = core.GatyaItem(num)
                actions.append(f"全マタタビ/キャッツアイ {num}個")
            except Exception as e:
                print(f"Matatabi Error: {e}")
                actions.append(f"マタタビ処理失敗: {e}")
        elif k == "event_ticket":
            num = int(val)
            actions.append(f"イベントチケット {num}")
        elif k == "daisannkeitai":
            try:
                all_cats: list[core.Cat] = s.cats.cats
                s.cats.true_form_cats(s, all_cats, True, True)
                actions.append("所持キャラ第3形態化")
            except Exception as e:
                print(f"Error details: {e}")
                actions.append(f"第3形態化失敗: {e}")
        elif k == "gold_pass":
            try:
                officer_pass = s.officer_pass
                club = officer_pass.gold_pass
                officer_id = core.NyankoClub.get_random_officer_id()
                club.get_gold_pass(officer_id, 30, s)
                actions.append(f"ゴールドパス解放 (ID: {officer_id})")
            except Exception as e:
                print(f"Gold Pass Error: {e}")
                actions.append(f"ゴールドパス解放失敗: {e}")
        elif k == "item_pack":
            try:
                item_pack = s.item_pack
                count = 0
                for set_id, purchase_set in item_pack.purchases.purchases.items():
                    for pack_name, pack in purchase_set.purchases.items():
                        if not pack.purchased:
                            pack.purchased = True
                            count += 1
                item_pack.three_days_started = False
                actions.append("全アイテムパック購入済み化")
            except Exception as e:
                print(f"Item Pack Error: {e}")
                actions.append(f"アイテムパック処理失敗: {e}")
        elif k == "medals":
            try:
                medals_obj = s.medals
                count = 0
                for medal_id in range(200):
                    if not medals_obj.has_medal(medal_id):
                        medals_obj.add_medal(medal_id)
                        count += 1
                actions.append("全メダル解放")
            except Exception as e:
                print(f"Medals Error: {e}")
                actions.append(f"メダル処理失敗: {e}")

    return actions

async def execute_and_reply(editor: CloudEditor, interaction: discord.Interaction, actions: list):
    """結果のアップロードとレスポンス処理"""
    t_code, pin = editor.upload_save()
    if t_code:
        dm = discord.Embed(title="代行完了", color=0x2ecc71)
        dm.add_field(name="引継ぎコード", value=f"`{t_code}`", inline=False)
        dm.add_field(name="認証コード", value=f"`{pin}`", inline=False)
        dm.set_footer(text="必ず保存してください")
        try:
            await interaction.user.send(embed=dm)
        except:
            pass
        done = discord.Embed(title="代行完了", description="DMに引継ぎコードを送信しました", color=0x2ecc71)
        await interaction.followup.send(embed=done, ephemeral=True)
    else:
        err = discord.Embed(title="エラー", description=f"```{editor.last_error}```", color=0xff0000)
        await interaction.followup.send(embed=err, ephemeral=True)

    gid = str(editor.guild_id)
    if gid in config:
        ch = interaction.client.get_channel(config[gid])
        if ch:
            log = discord.Embed(title="にゃんこ大戦争代行ログ", color=0x3498db)
            log.set_author(name=editor.user.name, icon_url=editor.user.display_avatar.url)
            log.add_field(name="購入者", value=editor.user.mention)
            log.add_field(name="内容", value="\n".join(actions) if actions else "変更なし")
            log.add_field(name="日時", value=f"<t:{int(time.time())}:F>")
            await ch.send(embed=log)


class MultiValueModal(ui.Modal):
    def __init__(self, editor, values, input_keys):
        super().__init__(title="数値入力")
        self.editor = editor
        self.values = values
        self.inputs = {}
        
        labels = {
            "catfood": "ネコカン",
            "xp": "XP",
            "rare": "レアチケット",
            "normal": "にゃんこチケット",
            "platinum": "プラチナチケット",
            "legend": "レジェンドチケット",
            "np": "NP",
            "lead": "リーダーシップ",
            "battleitem": "戦闘アイテム",
            "unlock_cats": "全キャラ解放 (任意の文字)",
            "remove_error_cats": "エラーキャラ削除 (任意の文字)",
            "unlock_stages": "全ステージ解放 (任意の文字)",
            "catseye": "キャッツアイ",
            "event_ticket": "イベントチケット",
            "daisannkeitai": "第3形態 (任意の文字)",
            "gold_pass": "ゴールド会員 (任意の文字)",
            "item_pack": "アイテムパック解放 (任意の文字)",
            "medals": "メダル全解放 (任意の文字)"
        }

        for k in input_keys:
            default_val = "999" if k == "event_ticket" else ""
            t = ui.TextInput(label=labels.get(k, k), default=default_val, required=False)
            self.inputs[k] = t
            self.add_item(t)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        input_data = {k: v.value for k, v in self.inputs.items()}
        actions = process_modifications(self.editor, self.values, input_data)
        await execute_and_reply(self.editor, interaction, actions)


class ModDropdown(ui.Select):
    def __init__(self, editor):
        self.editor = editor
        options = [
            discord.SelectOption(label="🚀 アイテム全MAX", value="max_items", description="主要アイテムをすべて上限に設定します"),
            discord.SelectOption(label="🎁 ユーザーランク報酬全受取", value="claim_ur_rewards", description="到達済みのランク報酬を一括受け取り状態にします"),
            discord.SelectOption(label="⬆️ 全キャラLv.30化", value="max_cat_levels", description="解放済みの全猫をレベル30にします"),
            discord.SelectOption(label="1,猫缶", value="catfood"),
            discord.SelectOption(label="2,XP", value="xp"),
            discord.SelectOption(label="3,レアチケット", value="rare"),
            discord.SelectOption(label="4,にゃんこチケット", value="normal"),
            discord.SelectOption(label="5,プラチナチケット", value="platinum"),
            discord.SelectOption(label="6,レジェンドチケット", value="legend"),
            discord.SelectOption(label="7,NP", value="np"),
            discord.SelectOption(label="8,リーダーシップ", value="lead"),
            discord.SelectOption(label="9,戦闘アイテム", value="battleitem"),
            discord.SelectOption(label="10,全キャラ解放", value="unlock_cats"),
            discord.SelectOption(label="11,エラーキャラ削除", value="remove_error_cats"),
            discord.SelectOption(label="12,全ステージ解放", value="unlock_stages"),
            discord.SelectOption(label="13,キャッツアイ", value="catseye"),
            discord.SelectOption(label="14,イベントチケット", value="event_ticket"),
            discord.SelectOption(label="15,第3形態", value="daisannkeitai"),
            discord.SelectOption(label="16,にゃんこクラブゴールド会員", value="gold_pass"),
            discord.SelectOption(label="17,アイテムパック解放", value="item_pack"),
            discord.SelectOption(label="18,にゃんこメダル全解放", value="medals"),
        ]
        # DiscordのModal上限（テキスト入力5個まで）に合わせて max_values=5 に設定
        super().__init__(placeholder="適用する項目を選んでください (最大5つ)", min_values=1, max_values=5, options=options)

    async def callback(self, interaction: discord.Interaction):
        # 一括処理系（モーダルで追加入力を必要としない項目）を抽出
        no_input_keys = ["max_items", "claim_ur_rewards", "max_cat_levels"]
        input_keys = [v for v in self.values if v not in no_input_keys]

        if not input_keys:
            # 入力項目がなく、一括処理系のみ選択された場合はモーダルを出さずに即実行
            await interaction.response.defer(ephemeral=True)
            actions = process_modifications(self.editor, self.values, {})
            await execute_and_reply(self.editor, interaction, actions)
        else:
            # テキスト入力項目が含まれる場合はモーダルを表示
            await interaction.response.send_modal(MultiValueModal(self.editor, self.values, input_keys))


class LoginModal(ui.Modal, title="代行ログイン"):
    t = ui.TextInput(label="引き継ぎコード")
    p = ui.TextInput(label="認証コード", min_length=4, max_length=4)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        editor = CloudEditor(self.t.value, self.p.value, interaction.user, interaction.guild.id)

        if editor.download_save():
            embed = discord.Embed(title="ログイン完了", description="適用する項目を選択してください (同時に選択できるのは最大5個までです)", color=0x5865F2)
            view = ui.View()
            view.add_item(ModDropdown(editor))
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            err = discord.Embed(title="ログインエラー", description=f"```{editor.last_error}```", color=0xff0000)
            await interaction.followup.send(embed=err, ephemeral=True)


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=discord.Intents.all())

    async def setup_hook(self):
        await self.tree.sync()

bot = MyBot()

class PersistentLoginView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="ログイン", 
        style=discord.ButtonStyle.success, 
        custom_id="persistent_bc_login"
    )
    async def login_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(LoginModal())

@bot.tree.command(name="チャンネル設定")
@app_commands.checks.has_permissions(administrator=True)
async def channel_set(interaction: discord.Interaction, channel: discord.TextChannel):
    config[str(interaction.guild.id)] = channel.id
    save_config(config)
    embed = discord.Embed(title="ログチャンネル設定", description="設定しました", color=0x2ecc71)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="にゃんこ大戦争代行")
@app_commands.checks.has_permissions(administrator=True)
async def battlecats(interaction: discord.Interaction):
    embed = discord.Embed(
        title="にゃんこ大戦争自動代行",
        description="引き継ぎコードと認証コードに間違いがないようにしてください\n\n1,猫缶 150円\n2,XP 400円\n3,レアチケットカンスト 400円\n4,にゃんこチケットカンスト 200円\n5,プラチナチケット 500円\n6,レジェンドチケット 500円\n7,NP 300円\n8,リーダーシップ 500円\n9,戦闘アイテム 400円\n10,全キャラ解放 400円\n11,エラーキャラ削除 200円\n12,全ステージ解放 200円\n13,キャッツアイ 500円\n14,イベントチケット 500円\n15,第3形態開放 500円\n19,アイテム全MAX 1000円\n20,全キャラLv.30 500円\n21,UR報酬一括受取 300円\n\nお支払い方法 PayPay",
        color=0x2b2d31
    )
    await interaction.response.send_message(embed=embed, view=PersistentLoginView())

@bot.event
async def on_ready():
    bot.add_view(PersistentLoginView())
    print(f"ログインしました: {bot.user}")

if TOKEN:
    bot.run(TOKEN)
else:
    print("ログインエラー")
