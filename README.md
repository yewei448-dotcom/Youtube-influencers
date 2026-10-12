# YouTube 达人合作工作台

一条龙服务：找达人 → 建联发信 → 回信跟踪 → 议价 → 合作效果评估 → 数据分析。

## 功能模块

- **🔍 关键词挖掘**：按关键词找 YouTube 达人（可筛粉丝/英语/活跃度/互动率），结果含 VPH、邮箱、订阅数
- **📚 达人库**：相似达人推荐 + 历史达人库（去重）
- **📧 建联库**：手动/批量发邮件、回信跟踪（IMAP）、议价（报价表 + 规则引擎）
- **📊 数据分析**：合作效果评估（ROI/CPM/二次合作判断）、回复率、热点追踪、GA4 数据

## 环境要求

- Python 3.8+
- macOS / Windows / Linux 均可

## 安装步骤

1. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

2. 配置密钥：把 `email_config.ini.example` 复制一份，改名为 `email_config.ini`，填入你自己的信息：
   ```ini
   [youtube]
   api_key = 你的YouTube_API_key

   [smtp]
   server = smtp.qiye.aliyun.com
   port = 465
   email = 你的发件邮箱
   auth_code = 你的SMTP授权码
   ```

   > 也可以把公司的 `email_config.ini` 直接复制过来（不要上传到 GitHub）。

## 运行

```bash
python3 -m streamlit run youtube_workbench.py
```

浏览器会自动打开（默认 http://localhost:8501）。

## 可选配置

- **GA4 数据分析**（拉会话/下载）：需要额外配置
  1. `email_config.ini` 里加 `[ga4]` 段：`credentials_file = ga4_credentials.json` + `property_id = 你的资源ID`
  2. 把 GA4 服务账号私钥 `ga4_credentials.json` 放到项目目录
- **Reddit 热点**：`email_config.ini` 里加 `[reddit]` 段（client_id/client_secret）

## 数据文件说明

运行后会自动生成以下本地数据文件（已在 .gitignore 里排除，不上传）：

- `达人库.csv`、`建联库.csv`、`邮件库.csv`、`合作效果.csv`

## 注意事项

- `email_config.ini` 和 `ga4_credentials.json` 含密钥，**绝不能提交到 GitHub**
- YouTube API 每日免费配额 1 万单位，搜索每词 100 单位，用完次日重置
