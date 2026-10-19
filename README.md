# 多 Gmail 邮件分拣工具

从多个 Gmail 账号拉取新邮件 → 用 Gemini 给每封邮件分类、定优先级、写"该做什么" → 在本地网页界面上勾选需要的放进待办，其余丢弃。

- 只在本机运行，界面只监听 `127.0.0.1`，同一局域网的其他设备打不开。
- 只读 Gmail：不发送、不删除、不修改 Gmail 里的任何邮件。

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


