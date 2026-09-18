# 达人抓取 & 邮件发送工作台

一个本地网页工具，把「找 YouTube 达人 → 筛选 → 建联 → 自动发合作邮件」整个流程串起来。

## 功能

- **关键词挖掘**：按关键词自动搜达人，带粉丝数/播放量/邮箱
- **达人库**：相似达人推荐 + 历史挖掘记录
- **建联库**：手动添加达人、选产品/合作类型、一键发邮件、查看发送记录

## 安装（一次性）

### 1. 安装 Python 3

**macOS**：系统自带，无需安装。

**Windows**：
1. 打开 [python.org/downloads](https://www.python.org/downloads/)
2. 下载安装包，双击运行
3. ⚠️ **底部一定勾选「Add Python to PATH」**
4. 点 Install Now

### 2. 安装依赖

打开终端，运行：

**macOS**（终端 App）：
```bash
pip3 install streamlit pandas requests openpyxl
```

**Windows**（按 `Win + R` 输入 `cmd` 回车，打开命令提示符）：
```
pip install streamlit pandas requests openpyxl
```

### 3. 配置（必须）

把 `email_config.ini.example` 复制一份，改名为 `email_config.ini`，用文本编辑器打开填上配置：

**macOS**：
```bash
cp email_config.ini.example email_config.ini
```

**Windows**：在文件夹里右键 `email_config.ini.example` → 复制 → 粘贴 → 改名为 `email_config.ini`，用记事本打开。

编辑 `email_config.ini`：

```ini
[youtube]
api_key = 你的YouTube_API_key

[smtp]
server = smtp.qiye.aliyun.com
port = 465
email = 你的发件邮箱
auth_code = 你的SMTP授权码
```

- **YouTube API key**：在 [Google Cloud Console](https://console.cloud.google.com) 启用 YouTube Data API v3 后申请。
- **SMTP 授权码**：阿里邮箱「设置 → 账户与安全 → 三方客户端安全密码」生成（不是登录密码）。

## 启动

**macOS**：
```bash
python3 -m streamlit run youtube_workbench.py
```

**Windows**（在命令提示符里，先 cd 到文件夹）：
```
python -m streamlit run youtube_workbench.py
```

浏览器打开 http://localhost:8501

## 使用流程

1. **关键词挖掘**：输关键词 → 左侧设筛选 → 点「开始挖掘」→ 选产品/类型 → 勾选加入建联库
2. **相似达人**：在达人库页输入视频/频道链接，反推同类达人
3. **建联库**：确认每个达人的邮箱/产品/类型
4. **发送邮件**：点发送 → 确认列表 → 确认发送（自动抄送 + 记录）

## 注意事项

- 发邮件会先弹确认列表，确认无误再发送
- 邮箱会自动清洗（去掉 `mailto:`/`http://` 等前缀）
- 每封邮件自动抄送给团队邮箱（在 `email_module.py` 的 `CC_RECIPIENTS` 里改）
- 数据存在本地 `达人库.csv`、`建联库.csv`、`邮件库.csv`，别删

## 安全

`email_config.ini`（含 API key 和 SMTP 授权码）已被 `.gitignore` 忽略，**不要提交到仓库**。泄露后请去对应后台重新生成。
