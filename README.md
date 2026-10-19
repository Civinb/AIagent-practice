# 多 Gmail 邮件分拣工具

从多个 Gmail 账号拉取新邮件 → 用 Gemini 给每封邮件分类、定优先级、写"该做什么" → 在本地网页界面上勾选需要的放进待办，其余丢弃。

- 只在本机运行，界面只监听 `127.0.0.1`，同一局域网的其他设备打不开。
- 只读 Gmail：不发送、不删除、不修改 Gmail 里的任何邮件。

## 文件说明

| 文件 | 作用 |
| --- | --- |
| `credentials.json` | Google OAuth 客户端文件（从 Google Cloud 控制台下载） |
| `accounts.json` | 账号清单：别名、邮箱、用途说明、是否启用 |
| `tokens/` | 各账号的授权令牌（`别名.json`），以及授权日期记录 `authorized_dates.json` |
| `authorize.py` | 首次授权 / 重新授权，为每个账号生成令牌 |
| `fetch_mail.py` | 拉取各账号收件箱"主要"标签页的新邮件，存进数据库 |
| `analyze.py` | 用 Gemini 分析新邮件；也可导出 `eval.csv` 供人工评估 |
| `prompt_v1.md` | 分析用的提示词 |
| `db.py` | 数据库读写（所有 SQL 都在这里） |
| `test_db.py` | `db.py` 的测试，用临时数据库，不碰 `mail.db` |
| `app.py` | 网页界面：分拣、待办、已丢弃三个标签页 |
| `start.bat` | 双击启动界面（Windows） |
| `requirements.txt` | 依赖清单（固定版本） |
| `mail.db` | SQLite 数据库：邮件、分析结果、待办（运行后自动生成） |
| `eval.csv` | `analyze.py --export` 导出的评估表（可选） |
| `agent.py` | 早期练习用的 Gemini agent 脚本，本工具运行时不使用 |

## 首次使用

### 0. 准备（只做一次）

1. 项目文件夹里已有虚拟环境 `.venv`，并已安装依赖：
   ```bash
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
2. `credentials.json`、`accounts.json` 已放在项目文件夹里。
3. 设置环境变量 `GEMINI_API_KEY`（双击 `start.bat` 时需要它是"用户变量"，只在某个终端里临时设置的不算）：
   开始菜单搜索"编辑账户的环境变量" → 用户变量 → 新建 → 变量名 `GEMINI_API_KEY`，变量值填 key → 确定。
   设置后要重新打开终端或重新双击 `start.bat` 才能生效。

### 1. 授权

在项目文件夹里打开终端：

```bash
.venv\Scripts\activate
```

```bash
python authorize.py
```

浏览器会依次打开每个账号的授权页，按终端提示选对邮箱。成功后令牌保存在 `tokens/别名.json`。

### 2. 启动

双击 `start.bat`。它会进入项目文件夹、检查 `GEMINI_API_KEY`、激活 `.venv`，然后运行 `python app.py`。
窗口里出现地址后，用浏览器打开（一般是 http://127.0.0.1:7860 ，端口被占用时 Gradio 会换一个，以窗口显示的为准）。

关闭界面：直接关掉 `start.bat` 的窗口；或按 Ctrl+C，出现"终止批处理操作吗(Y/N)?"时按 N 可以留下窗口查看输出。

## ⚠ 每 7 天重新授权

Google Cloud 项目处于"测试"发布状态时，授权令牌大约 **7 天** 后失效，拉取会报授权错误。
每 7 天（或看到授权错误时）对每个账号重新运行一次：

```bash
python authorize.py work
```

把 `work` 换成 `accounts.json` 里的每个别名，逐个运行。每个账号上次的授权时间记录在 `tokens/authorized_dates.json`。
注意：不带别名的 `python authorize.py` 只处理"还没有令牌"的账号，已有（但过期）令牌的账号要带别名。

## 备份 mail.db

1. 先关闭界面（关掉 `start.bat` 窗口），确保没有程序正在写数据库。
2. 复制 `mail.db`，文件名带上日期，例如在项目文件夹的 PowerShell 里运行：
   ```powershell
   Copy-Item mail.db "$HOME\Documents\mail_$(Get-Date -Format yyyy-MM-dd).db"
   ```
   会得到 `文档\mail_2026-10-18.db` 这样的文件。也可以在资源管理器里直接复制粘贴后改名。

备份文件里有邮件正文，建议放在项目文件夹以外（如上面的"文档"），以免被误提交到 git。
恢复时：关闭界面，用备份文件替换 `mail.db`（改回这个名字）。

## .gitignore

以下文件含有密钥、令牌或邮件内容，必须写在 `.gitignore` 里，不要提交：

```text
credentials.json
accounts.json
tokens/
mail.db
eval.csv
```
