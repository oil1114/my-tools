# 場地釋出提醒

一支自動監控程式：每隔約 10 分鐘登入線上訂場系統，
檢查有沒有「新的可預約時段」（新開放的日子、或有人取消空出來的場地），
有的話就**用 Telegram 推播到你手機**。

程式跑在 GitHub 的免費雲端（GitHub Actions），**不需要你的電腦開著**。

---

## 你要準備的東西

1. 手機裝 **Telegram**（通知只會進你自己的聊天室，只有你看得到）
2. 一個免費的 **GitHub** 帳號
3. 你在訂場系統的**帳號**與**密碼**（就是你平常登入 App/網站用的）

> 你的密碼與 Telegram 金鑰會存在 GitHub 的「加密保險箱（Secrets）」裡，
> 只有程式讀得到，不會出現在程式碼、也不會被別人看到。

---

## 步驟一：建立你的 Telegram 機器人

1. 打開 Telegram，搜尋 **@BotFather**（有藍色勾勾的官方帳號）。
2. 對它輸入 `/newbot` → 依指示取一個顯示名稱、再取一個以 `bot` 結尾的帳號名稱。
3. 完成後它會給你一段**金鑰（token）**，長得像
   `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`——先保留著。
4. 點開你剛建立的機器人，按 **Start**（或隨便傳一句話給它）。
   > 這一步很重要：機器人只能傳訊息給「先跟它說過話」的人。

---

## 步驟二：填入 Secrets

在你的 repo 頁面：**Settings** → 左側 **Secrets and variables** → **Actions**
→ **New repository secret**，依序新增四個：

| Name（名稱，要一字不差）| Secret（值）|
|---|---|
| `FE_ACCOUNT`   | 你的訂場帳號 |
| `FE_PASSWORD`  | 你的訂場密碼 |
| `TG_BOT_TOKEN` | 步驟一 BotFather 給你的金鑰 |
| `TG_CHAT_ID`   | 你的聊天室 ID（見下方說明） |

**怎麼拿 `TG_CHAT_ID`：** 在 Telegram 搜尋 **@userinfobot**，按 Start，
它會回你一組數字（你的 ID），那組數字就是要填的值。

---

## 步驟三：啟用並測試

1. repo 上方 **Actions** 分頁 → 若看到提示，點 **I understand my workflows, enable them**。
2. 左側點 **Monitor** → 右邊 **Run workflow** 手動跑一次。
3. 點進那次執行看綠色勾勾。第一次執行是「建立基準」，**不會推播**（正常）。
4. 之後它每約 10 分鐘自己跑。等下次有場地釋出，Telegram 就會收到通知。

---

## 想縮小範圍（只盯特定星期／時段）

預設是**所有日子、所有時段**只要有新場地就通知。若想只盯特定範圍，
到 **Settings → Secrets and variables → Actions → Variables** 分頁 → **New repository variable**：

| Name | 值 | 說明 |
|---|---|---|
| `WATCH_DOWS` | 例：`6,7` | 只盯這些星期（週一=1 … 週日=7）。留空或不設=全部 |
| `WATCH_START_HOUR` | 例：`18` | 只盯這個鐘點（含）之後開始的時段，24 小時制 |
| `WATCH_END_HOUR` | 例：`22` | 只盯這個鐘點（不含）之前開始的時段 |

例如「只要週六日晚上 18:00 之後的場地」= `WATCH_DOWS=6,7`、
`WATCH_START_HOUR=18`、`WATCH_END_HOUR=22`。

---

## 小提醒

- GitHub 排程在尖峰時可能延遲幾分鐘到十幾分鐘才跑，屬正常現象；
  熱門時段被取消的空位，有可能在你收到通知前又被別人搶走。
- 這支程式**只會讀取、比對、通知**，不會幫你按下預約或送出任何訂單。
  收到通知後，請自己打開 App/網站完成預約。
- 若之後改了訂場密碼，記得回到 Secrets 更新 `FE_PASSWORD`。
