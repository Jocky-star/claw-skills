# 小红书自动发布 (xhs-publisher)

一站式小红书内容创作、图片卡片渲染、自动发布工具。

## 能力概览

| 功能 | 说明 |
|------|------|
| 📝 内容渲染 | Markdown → 精美图片卡片（8 套主题） |
| 📤 自动发布 | CDP 浏览器自动化，稳定可靠 |
| 🏷️ 话题标签 | 自动识别并选择话题 |
| 👤 多账号 | 支持多个小红书账号切换 |
| 🔍 数据运营 | 搜索笔记、评论、数据看板 |

## 环境要求

- Python 3.12（venv 位于 `.venv/`）
- Playwright + Chromium（图片渲染）
- Google Chrome（CDP 发布）
- 依赖：`markdown pyyaml playwright requests websockets python-dotenv`

## 使用方法

### 1. 渲染图片卡片

将 Markdown 文件渲染为小红书标准 3:4 比例图片（1080×1440，DPR=2）。

```bash
PYTHON=~/.openclaw/skills/xhs-publisher/.venv/bin/python
RENDER=~/.openclaw/skills/xhs-publisher/scripts/render_xhs.py

# 基本用法
$PYTHON $RENDER content.md -o output/

# 指定主题（8 种可选）
$PYTHON $RENDER content.md -t terminal -o output/
$PYTHON $RENDER content.md -t neo-brutalism -o output/

# 指定分页模式
$PYTHON $RENDER content.md -t terminal -m auto-split -o output/
```

**可用主题：** `default` `minimal` `elegant` `tech` `terminal` `neo-brutalism` `gradient` `magazine`

**分页模式：** `separator`（手动用 `---` 分页）| `auto-fit`（自动填充）| `auto-split`（自动分割）| `dynamic`（动态）

**Markdown 格式要求：**

```markdown
---
title: 封面标题
subtitle: 副标题（可选）
author: 作者名（可选）
---

正文内容，支持标准 Markdown 语法...
```

### 2. 发布笔记

通过 CDP 浏览器自动化发布到小红书。

```bash
PYTHON=~/.openclaw/skills/xhs-publisher/.venv/bin/python
PUBLISH=~/.openclaw/skills/xhs-publisher/scripts/publish_pipeline.py

# 公开发布（图片笔记）
$PYTHON $PUBLISH \
  --title "标题" \
  --content "正文内容

#话题1 #话题2" \
  --images cover.png card_1.png card_2.png

# 预览模式（只填表不发布，人工确认）
$PYTHON $PUBLISH \
  --title "标题" \
  --content "正文" \
  --images *.png \
  --preview

# Headless 模式（无 GUI，适合自动化）
$PYTHON $PUBLISH \
  --title "标题" \
  --content "正文" \
  --images *.png \
  --headless
```

**话题标签：** 在 content 最后一行写 `#标签1 #标签2`，会自动识别并在发布页选择。

### 3. 账号管理

```bash
PYTHON=~/.openclaw/skills/xhs-publisher/.venv/bin/python
CDP=~/.openclaw/skills/xhs-publisher/scripts/cdp_publish.py

# 登录（打开浏览器扫码）
$PYTHON $CDP login

# 检查登录状态
$PYTHON $CDP check-login

# 多账号
$PYTHON $CDP login --account work
$PYTHON $CDP list-accounts
```

### 4. 数据运营

```bash
# 搜索笔记
$PYTHON $CDP search-feeds --keyword "关键词"

# 查看笔记数据
$PYTHON $CDP content-data

# 查看通知/提及
$PYTHON $CDP get-notification-mentions
```

## 完整工作流示例

**从 Markdown 到发布一条龙：**

```bash
PYTHON=~/.openclaw/skills/xhs-publisher/.venv/bin/python
SKILL_DIR=~/.openclaw/skills/xhs-publisher

# Step 1: 写内容（Markdown）
cat > /tmp/post.md << 'EOF'
---
title: 标题
subtitle: 副标题
author: 作者
---

正文内容...
EOF

# Step 2: 渲染图片
$PYTHON $SKILL_DIR/scripts/render_xhs.py /tmp/post.md -t terminal -m auto-split -o /tmp/post_output/

# Step 3: 发布
$PYTHON $SKILL_DIR/scripts/publish_pipeline.py \
  --title "标题" \
  --content "正文摘要

#标签1 #标签2" \
  --images /tmp/post_output/cover.png /tmp/post_output/card_*.png
```

## 目录结构

```
xhs-publisher/
├── SKILL.md              # 本文件
├── openclaw.plugin.json  # OpenClaw 插件配置
├── .venv/                # Python 3.12 虚拟环境
├── scripts/
│   ├── render_xhs.py     # Markdown → 图片渲染引擎
│   ├── publish_pipeline.py  # 发布管线（推荐入口）
│   ├── cdp_publish.py    # CDP 发布核心（2690行）
│   ├── chrome_launcher.py   # Chrome 启动/管理
│   ├── account_manager.py   # 多账号管理
│   ├── feed_explorer.py     # 笔记搜索/数据
│   ├── image_downloader.py  # 图片下载工具
│   └── run_lock.py          # 单实例锁
├── assets/
│   ├── card.html         # 正文卡片模板
│   ├── cover.html        # 封面模板
│   ├── styles.css        # 基础样式
│   ├── example.md        # 示例 Markdown
│   └── themes/           # 8 套主题 CSS
└── output/               # 默认输出目录
```

## 注意事项

- **首次使用**需要扫码登录（`cdp_publish.py login`），登录态缓存 12 小时
- **Chrome 必须关闭**普通窗口才能启动 CDP 调试模式（或用独立 profile）
- 图片渲染需要 Playwright Chromium，发布需要系统 Chrome，两者不冲突
- 标题不超过 20 字，超出自动截断
- 单篇最多 18 张图片
- Headless 模式下如需登录会自动切回有界面模式

## 来源

整合自两个开源项目：
- 渲染引擎：[Auto-Redbook-Skills](https://github.com/comeonzhj/Auto-Redbook-Skills)
- 发布引擎：[XiaohongshuSkills](https://github.com/white0dew/XiaohongshuSkills)
