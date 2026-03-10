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

CONFIG_FILE="config.json"

def load_config():
    try:
        with open(CONFIG_FILE,"r") as f:
            return json.load(f)
    except:
        return {}

def save_config(data):
    with open(CONFIG_FILE,"w") as f:
        json.dump(data,f,indent=4)

config=load_config()

class NyankoSignature:

    def __init__(self,inquiry_code:str,data:str):
        self.inquiry_code=inquiry_code
        self.data=data

    def generate_signature(self)->str:
        random_hex=secrets.token_hex(32)
        key=(self.inquiry_code+random_hex).encode()
        signature=hmac.new(key,self.data.encode(),hashlib.sha256).hexdigest()
        return random_hex+signature

    def generate_signature_v1(self)->str:
        data_double=self.data+self.data
        random_hex=secrets.token_hex(20)
        key=(self.inquiry_code+random_hex).encode()
        signature=hmac.new(key,data_double.encode(),hashlib.sha1).hexdigest()
        return random_hex+signature


class CloudEditor:
    AUTH_URL="https://nyanko-auth.ponosgames.com"
    SAVE_URL="https://nyanko-save.ponosgames.com"

    def __init__(self,transfer_code:str,pin:str,user,guild_id):
        self.transfer_code=transfer_code
        self.pin=pin
        self.session=requests.Session()
        self.save_file:Optional[Any]=None
        self.password=""
        self.last_error=""
        self.user=user
        self.guild_id=guild_id
        self.actions=[]

    def get_common_headers(self,iq:str,data:str)->dict:
        return{
        "Content-Type":"application/json",
        "Nyanko-Signature":NyankoSignature(iq,data).generate_signature(),
        "Nyanko-Timestamp":str(int(time.time())),
        "Nyanko-Signature-Version":"1",
        "Nyanko-Signature-Algorithm":"HMACSHA256",
        "User-Agent":"Dalvik/2.1.0"
        }

    def download_save(self)->bool:
        nonce=secrets.token_hex(16)
        url=f"{self.SAVE_URL}/v2/transfers/{self.transfer_code}/reception"
        payload={
        "clientInfo":{
        "client":{"version":"15.2.1","countryCode":"ja"},
        "os":{"type":"android","version":"13"},
        "device":{"model":"SM-S918B"}
        },
        "pin":self.pin,
        "nonce":nonce
        }
        body=json.dumps(payload,separators=(",",":"))
        headers={"Content-Type":"application/json"}
        try:
            res=self.session.post(url,headers=headers,data=body)
            if res.status_code==200 and res.headers.get("Content-Type")=="application/octet-stream":
                try:
                    from bcsfe.core.save_file import SaveFile as bSaveFile
                except:
                    bSaveFile=core.SaveFile
                self.save_file=bSaveFile(core.Data(res.content),cc=core.CountryCode("jp"))
                self.password=res.headers.get("Nyanko-Password","")
                return True
            self.last_error=res.text
        except Exception as e:
            self.last_error=str(e)
        return False

    def upload_save(self)->Tuple[Optional[str],Optional[str]]:

        if not self.save_file:
            return None,None
        
        if not hasattr(self.save_file, 'local_manager'):
            self.save_file.local_manager = None
            # 整合性チェック。エラーが出ても無視して次に進む
            try:
                self.save_file.patch()
            except:
                pass
        inq=self.save_file.inquiry_code
        try:
            login_data={
            "accountCode":inq,
            "password":self.password,
            "clientInfo":{
            "client":{"version":"15.2.1","countryCode":"ja"},
            "os":{"type":"android","version":"9"},
            "device":{"model":"SM-G955F"}
            },
            "nonce":secrets.token_hex(16)
            }
            login_body=json.dumps(login_data,separators=(",",":"))
            h1=self.get_common_headers(inq,login_body)
            res1=self.session.post(f"{self.AUTH_URL}/v1/tokens",headers=h1,data=login_body)
            if res1.status_code!=200:
                self.last_error=res1.text
                return None,None
            token=res1.json()["payload"]["token"]
            nonce_aws=secrets.token_hex(16)
            h2=self.get_common_headers(inq,"")
            h2["Authorization"]=f"Bearer {token}"
            res2=self.session.get(f"{self.SAVE_URL}/v2/save/key?nonce={nonce_aws}",headers=h2)
            aws=res2.json()["payload"]
            modified_bytes=self.save_file.to_data().to_bytes()
            files={"file":("file.sav",modified_bytes,"application/octet-stream")}
            s3_data={k:v for k,v in aws.items() if k!="url"}
            requests.post(aws["url"],data=s3_data,files=files)
            meta_payload={
            "managedItemDetails":[],
            "nonce":secrets.token_hex(16),
            "playTime":self.save_file.officer_pass.play_time,
            "rank":self.save_file.calculate_user_rank(),
            "receiptLogIds":[],
            "saveKey":aws["key"],
            "signature_v1":NyankoSignature(inq,"[]").generate_signature_v1()
            }
            meta_body=json.dumps(meta_payload,separators=(",",":"))
            h4=self.get_common_headers(inq,meta_body)
            h4["Authorization"]=f"Bearer {token}"
            res4=self.session.post(f"{self.SAVE_URL}/v2/transfers",headers=h4,data=meta_body)
            p=res4.json()["payload"]
            return p.get("transferCode"),p.get("pin")
        except Exception as e:
            self.last_error=str(e)
            return None,None

class MultiValueModal(ui.Modal):
    def __init__(self,editor,values):
        super().__init__(title="数値入力")
        self.editor=editor
        self.values=values
        self.inputs={}
        if "catfood" in values:
            t=ui.TextInput(label="ネコカン")
            self.inputs["catfood"]=t
            self.add_item(t)
        if "xp" in values:
            t=ui.TextInput(label="XP")
            self.inputs["xp"]=t
            self.add_item(t)
        if "rare" in values:
            t=ui.TextInput(label="レアチケット")
            self.inputs["rare"]=t
            self.add_item(t)
        if "normal" in values:
            t = ui.TextInput(label="にゃんこチケット")
            self.inputs["normal"] = t
            self.add_item(t)
        if "platinum" in values:
            t = ui.TextInput(label="プラチナチケット")
            self.inputs["platinum"] = t
            self.add_item(t)
        if "legend" in values:
            t = ui.TextInput(label="レジェンドチケット")
            self.inputs["legend"] = t
            self.add_item(t)
        if "np" in values:
            t=ui.TextInput(label="NP")
            self.inputs["np"]=t
            self.add_item(t)
        if "lead" in values:
            t=ui.TextInput(label="リーダーシップ")
            self.inputs["lead"]=t
            self.add_item(t)
        if "battleitem" in values:
            t = ui.TextInput(label="戦闘アイテム")
            self.inputs["battleitem"] = t
            self.add_item(t)
        if "unlock_cats" in values:
            t = ui.TextInput(label="全キャラ解放")
            self.inputs["unlock_cats"] = t
            self.add_item(t)
        if "remove_error_cats" in values:
            t = ui.TextInput(label="エラーキャラ削除")
            self.inputs["remove_error_cats"] = t
            self.add_item(t)
        if "unlock_stages" in values:
            t = ui.TextInput(label="全ステージ解放")
            self.inputs["unlock_stages"] = t
            self.add_item(t)
        if "catseye" in values:
            t = ui.TextInput(label="キャッツアイ")
            self.inputs["catseye"] = t
            self.add_item(t)
        if "event_ticket" in values:
            t = ui.TextInput(label="イベントチケット", default="999")
            self.inputs["event_ticket"] = t
            self.add_item(t)

    async def on_submit(self,interaction:discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        s=self.editor.save_file
        actions=[]
        for k,v in self.inputs.items():
            if v.value=="":
                continue
            num=int(v.value)
            if k=="catfood":
                s.set_catfood(num)
                actions.append(f"ネコカン {num}")
            elif k=="xp":
                s.set_xp(num)
                actions.append(f"XP {num}")
            elif k=="rare":
                s.set_rare_tickets(num)
                actions.append(f"レアチケット {num}")
            elif k=="normal":
                s.set_normal_tickets(num)
                actions.append(f"にゃんこチケット {num}")
            elif k=="platinum":
                s.set_platinum_tickets(num)
                actions.append(f"プラチナチケット {num}")
            elif k=="legend":
                s.set_legend_tickets(num)
                actions.append(f"レジェンドチケット {num}")
            elif k=="np":
                s.set_np(num)
                actions.append(f"NP {num}")
            elif k=="lead":
                s.set_leadership(num)
                actions.append(f"リーダーシップ {num}")
            elif k == "battleitem":  # あなたが設定した名前に合わせました
                try:
                    # s.battle_items.items を全ループして数値をセット
                    for i in range(len(s.battle_items.items)):
                        s.battle_items.items[i] = num
                    actions.append(f"バトルアイテム全種 {num}")
                except Exception as e:
                    print(f"Battle Item Error: {e}")
            elif "unlock_cats" in self.values:
                for cat in s.cats.cats:
                    # 1. 解放フラグ
                    cat.unlocked = True
                    
                    # 2. 所持状態にする
                    if hasattr(cat, 'set_obtained'):
                        cat.set_obtained(True)
                    
                    # 3. レベル設定 (Upgradeオブジェクト内の変数名を柔軟に探す)
                    if hasattr(cat, 'upgrade') and cat.upgrade is not None:
                        # 内部では base_lv という名前が使われていることが多いです
                        if hasattr(cat.upgrade, 'base_lv'):
                            if cat.upgrade.base_lv < 0:
                                cat.upgrade.base_lv = 0
                        elif hasattr(cat.upgrade, 'level'):
                            if cat.upgrade.level < 0:
                                cat.upgrade.level = 0
                                
                actions.append("全キャラ解放")
            elif k == "remove_error_cats":
                # 1. 存在しないIDや異常なデータを特定して削除
                # s.cats.cats は全キャラのリスト
                original_count = len(s.cats.cats)
                
                # 正常なキャラだけを残すフィルタリング例
                # IDが負、または極端に大きいものを除外する場合
                s.cats.cats = [cat for cat in s.cats.cats if 0 <= cat.id < 1000]
                
                # 2. あるいは、特定の「エラーキャラ」フラグを持つものをリセット
                for cat in s.cats.cats:
                    # 名前が取得できない、あるいはデータが空のキャラを未所持に戻す
                    if not hasattr(cat, 'upgrade') or cat.upgrade is None:
                        cat.unlocked = False
                        if hasattr(cat, 'set_obtained'):
                            cat.set_obtained(False)
                
                actions.append("エラーキャラ削除・リセット完了")

            elif k == "unlock_stages":
             core.StoryChapters.clear_tutorial(self.editor.save_file)
             story_chapters = self.editor.save_file.story.get_real_chapters()
             for chapter in story_chapters:
                 chapter.clear_chapter()
                 for stage in chapter.get_valid_treasure_stages():
                     stage.set_treasure(3)
                     print("全ステージ解放・お宝コンプ完了")
                     actions.append("全ステージ解放・お宝コンプ完了")
            
            elif k == "catseye":
                raw_val = self.inputs["catseye"].value
                amount = int(raw_val) if raw_val.isdigit() else 999
                num_categories = len(self.editor.save_file.catseyes)
                self.editor.save_file.catseyes = [amount] * num_categories
                print(f"全種類のキャッツアイを {amount} 個に設定しました")

            elif k == "event_ticket":
                try:
                    
                    user_input = self.inputs["event_ticket"].value
                    amount = int(user_input) if user_input.isdigit() else 999
                except Exception:
                    
                    amount = 999

            # 1. サーバーから生のイベントデータを直接取得
            try:
                handler = core.ServerHandler(self.editor.save_file)
                gatya_data_raw = handler.download_gatya_data()
                
                if gatya_data_raw is None:
                    print("Log: イベントデータのダウンロードに失敗しました。")
                    continue
                
                # ServerGatyaDataをパース
                gatya_data = core.ServerGatyaData.from_data(gatya_data_raw)
            except Exception as e:
                print(f"Log: イベントデータ取得中にエラー: {e}")
                continue

            # 2. 現在のセーブデータ内のリストを直接書き換え
            # ガチャデータから「イベントチケット」に関連するIDを探し、枚数を設定する
            updated = False
            for item in gatya_data.items:
                # 開催中の全ガチャセットを確認
                for gset in item.sets:
                    if gset.number == -1: continue
                    
                    # チケットIDを取得 (bcsfeの内部ID体系を使用)
                    # ここでは安全に、セーブデータの全チケット枠をamountに設定する「一括モード」を適用します
                    updated = True

            # 3. セーブデータの各チケット配列を直接一括更新
            # 多くのイベントチケットは以下の3つのリストに格納されています
            try:
                # 福引ガチャチケットなど
                self.editor.save_file.lucky_tickets = [amount] * len(self.editor.save_file.lucky_tickets)
                # イベントガチャチケット1
                self.editor.save_file.event_capsules = [amount] * len(self.editor.save_file.event_capsules)
                # イベントガチャチケット2
                self.editor.save_file.event_capsules_2 = [amount] * len(self.editor.save_file.event_capsules_2)
                
                print(f"Log: すべてのイベントチケット枠を {amount} 枚に設定しました。")
            except Exception as e:
                print(f"Log: チケット書き換え中にエラー: {e}")

        t_code,pin=self.editor.upload_save()
        if t_code:
             dm=discord.Embed(title="代行完了",color=0x2ecc71)
             dm.add_field(name="引継ぎコード",value=f"`{t_code}`",inline=False)
             dm.add_field(name="認証コード",value=f"`{pin}`",inline=False)
             dm.set_footer(text="必ず保存してください")
        try:
                await interaction.user.send(embed=dm)
        except:
                pass
        done=discord.Embed(title="代行完了",description="DMに引継ぎコードを送信しました",color=0x2ecc71)
        await interaction.followup.send(embed=done,ephemeral=True)
        gid=str(self.editor.guild_id)
        if gid in config:
                ch=interaction.client.get_channel(config[gid])
                if ch:
                    log=discord.Embed(title="にゃんこ大戦争代行ログ",color=0x3498db)
                    log.set_author(name=self.editor.user.name,icon_url=self.editor.user.display_avatar.url)
                    log.add_field(name="購入者",value=self.editor.user.mention)
                    log.add_field(name="内容",value="\n".join(actions))
                    log.add_field(name="日時",value=f"<t:{int(time.time())}:F>")
                    await ch.send(embed=log)
                else:err=discord.Embed(title="エラー",description=f"```{self.editor.last_error}```",color=0xff0000)
        await interaction.followup.send(embed=err,ephemeral=True)

class ModDropdown(ui.Select):
    def __init__(self,editor):
        self.editor=editor
        options=[
        discord.SelectOption(label="1,猫缶",value="catfood"),
        discord.SelectOption(label="2,XP",value="xp"),
        discord.SelectOption(label="3,レアチケット",value="rare"),
        discord.SelectOption(label="4,にゃんこチケット", value="normal"),
        discord.SelectOption(label="5,プラチナチケット", value="platinum"),
        discord.SelectOption(label="6,レジェンドチケット", value="legend"),
        discord.SelectOption(label="7,NP",value="np"),
        discord.SelectOption(label="8,リーダーシップ",value="lead"),
        discord.SelectOption(label="9,戦闘アイテム", value="battleitem"),
        discord.SelectOption(label="10,全キャラ解放", value="unlock_cats"),
        discord.SelectOption(label="11,エラーキャラ削除", value="remove_error_cats"),
        discord.SelectOption(label="12,全ステージ解放", value="unlock_stages"),
        discord.SelectOption(label="13,キャッツアイ", value="catseye"),
        discord.SelectOption(label="14,イベントチケット", value="event_ticket"),
        ]
        super().__init__(placeholder="適用する項目をすべて選んでください...",min_values=1,max_values=len(options),options=options)
    async def callback(self,interaction:discord.Interaction):
        await interaction.response.send_modal(MultiValueModal(self.editor,self.values))


class LoginModal(ui.Modal, title="代行ログイン"):
    t = ui.TextInput(label="引き継ぎコード")
    p = ui.TextInput(label="認証コード", min_length=4, max_length=4)

    async def on_submit(self, interaction: discord.Interaction):
        # 1. ログイン開始のログ
        print(f"--- ログイン処理開始 ---")
        print(f"ユーザー: {interaction.user} (ID: {interaction.user.id})")
        print(f"引き継ぎコード: {self.t.value}")
        print(f"認証コード: {self.p.value}")
        print("ステータス: ログイン中...")

        await interaction.response.defer(ephemeral=True)

        # CloudEditorの初期化
        editor = CloudEditor(self.t.value, self.p.value, interaction.user, interaction.guild.id)

        # 2. ダウンロード処理の実行と結果ログ
        if editor.download_save():
            print("ステータス: ログイン完了")
            print(f"------------------------")

            embed = discord.Embed(title="ログイン完了", description="適用する項目を選択してください", color=0x5865F2)
            view = ui.View()
            view.add_item(ModDropdown(editor))
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            # 3. 失敗時のログ
            print(f"ステータス: ログイン失敗")
            print(f"エラー内容: {editor.last_error}")
            print(f"------------------------")

            err = discord.Embed(title="ログインエラー", description=f"```{editor.last_error}```", color=0xff0000)
            await interaction.followup.send(embed=err, ephemeral=True)


class MyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!",intents=discord.Intents.all())
    async def setup_hook(self):
        await self.tree.sync()

bot=MyBot()

class PersistentLoginView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(
        label="ログイン", 
        style=discord.ButtonStyle.success, 
        custom_id="persistent_bc_login" # これが再起動後の識別に必須です
    )
    async def login_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 前に作成した LoginModal を呼び出す
        await interaction.response.send_modal(LoginModal())

@bot.tree.command(name="チャンネル設定")
@app_commands.checks.has_permissions(administrator=True)
async def channel_set(interaction:discord.Interaction,channel:discord.TextChannel):
    config[str(interaction.guild.id)]=channel.id
    save_config(config)
    embed=discord.Embed(title="ログチャンネル設定",description= "設定しました",color=0x2ecc71)
    await interaction.response.send_message(embed=embed,ephemeral=True)

@bot.tree.command(name="にゃんこ大戦争代行")
@app_commands.checks.has_permissions(administrator=True)
async def battlecats(interaction:discord.Interaction):
    embed=discord.Embed(title="にゃんこ大戦争自動代行",description="引き継ぎコードと認証コードに間違いがないようにしてください\n\n1,猫缶 150円\n2,XP 400円\n3,レアチケットカンスト 400円\n4,にゃんこチケットカンスト 200円\n5,プラチナチケット 500円\n6,レジェンドチケット  500円\n7,NP 300円\n8,リーダーシップ 500円\n9,戦闘アイテム 400円\n10,全キャラ解放 400円\n11,エラーキャラ削除 200円\n12,全ステージ解放 200円\n13,キャッツアイ 500円\n14,イベントチケット 500円\n\nお支払い方法 PayPay",color=0x2b2d31)
    view=ui.View()
    btn=ui.Button(label="ログイン",style=discord.ButtonStyle.success)
    async def login_cb(it):
        await it.response.send_modal(LoginModal())
    btn.callback=login_cb
    view.add_item(btn)
    await interaction.response.send_message(embed=embed, view=PersistentLoginView())

@bot.event
async def on_ready():
    bot.add_view(PersistentLoginView())
    print(f"ログインしました: {bot.user}")

if TOKEN:
    bot.run(TOKEN)
else:
    print("ログインエラー")
