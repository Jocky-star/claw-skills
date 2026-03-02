#!/bin/bash
# A股智能盯盘助手 - 一键安装（venv 隔离）
set -e

echo "🔧 A股智能盯盘助手 - 开始安装..."

# 定位脚本所在目录（兼容各种调用方式）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SOURCE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILL_DIR="${HOME}/.openclaw/skills/a-share-monitor"
VENV_DIR="${SKILL_DIR}/.venv"

# 1. 检查 Python
echo "📋 检查环境..."
if ! command -v python3 &> /dev/null && ! command -v python &> /dev/null; then
    echo "❌ 未找到 Python，请先安装 Python 3.9+"
    exit 1
fi

PYTHON=$(command -v python3 || command -v python)
PY_VERSION=$($PYTHON --version 2>&1)
echo "   ✅ $PY_VERSION"

# 2. 复制 Skill 文件到 OpenClaw 技能目录（先部署，再在目标位置建 venv）
echo "📂 部署 Skill 文件..."
mkdir -p "${SKILL_DIR}/scripts" "${SKILL_DIR}/references"
cp "${SOURCE_DIR}/SKILL.md" "${SKILL_DIR}/"
cp "${SOURCE_DIR}/openclaw.plugin.json" "${SKILL_DIR}/"
cp "${SOURCE_DIR}/scripts/stock_monitor.py" "${SKILL_DIR}/scripts/"
cp "${SOURCE_DIR}/references/signal_glossary.md" "${SKILL_DIR}/references/"
echo "   ✅ 文件已部署到 ${SKILL_DIR}"

# 3. 创建虚拟环境并安装依赖
echo "🐍 创建虚拟环境..."
$PYTHON -m venv "${VENV_DIR}"
echo "   ✅ venv 已创建: ${VENV_DIR}"

VENV_PYTHON="${VENV_DIR}/bin/python"
echo "📦 安装 Python 依赖（在 venv 中）..."
"${VENV_PYTHON}" -m pip install --upgrade pip -q
"${VENV_PYTHON}" -m pip install akshare pandas ta requests -q
echo "   ✅ 依赖安装完成"

# 4. 快速验证
echo "🧪 验证安装..."
"${VENV_PYTHON}" "${SKILL_DIR}/scripts/stock_monitor.py" search --code 600519 > /dev/null 2>&1 && echo "   ✅ 验证通过" || echo "   ⚠️ 验证失败，可能需要检查网络"

echo ""
echo "========================================="
echo "✅ 安装完成！重启 OpenClaw 后即可使用。"
echo ""
echo "试试对 OpenClaw 说："
echo "  「帮我盯一下贵州茅台」"
echo "  「分析 300750 宁德时代」"
echo "========================================="
