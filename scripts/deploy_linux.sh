#!/usr/bin/env bash
# HACOO 平台公司 Linux 部署脚本
# 用法: bash scripts/deploy_linux.sh [install_dir]
set -euo pipefail

INSTALL_DIR="${1:-$HOME/hacoo-platform}"
HACOO_PORT="${HACOO_PORT:-9877}"

echo "==> [1/5] 同步项目到 ${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}"
rsync -a --delete --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
  ./ "${INSTALL_DIR}/"

echo "==> [2/5] 检查 Python 环境 (>= 3.8, 核心层零第三方依赖)"
python3 -c "import sys; assert sys.version_info >= (3, 8), sys.version; print('python', sys.version.split()[0])"

echo "==> [3/5] 配置环境变量 (写入 ${INSTALL_DIR}/hacoo.env)"
cat > "${INSTALL_DIR}/hacoo.env" <<EOF
# vLLM / KIMI 2.6
export VLLM_BASE_URL="${VLLM_BASE_URL:-http://127.0.0.1:8000/v1}"
export VLLM_API_KEY="${VLLM_API_KEY:-EMPTY}"
export KIMI_MODEL="${KIMI_MODEL:-kimi2.6}"
# HACOO 平台
export HACOO_SERVER="127.0.0.1:${HACOO_PORT}"
export HACOO_TOKEN="${HACOO_TOKEN:-dev-token}"
# 可选: 授权配置文件 (不设则用默认 dev-token/admin)
# export HACOO_AUTH_FILE="${INSTALL_DIR}/auth.json"
EOF
echo "    请按公司环境编辑 ${INSTALL_DIR}/hacoo.env 中的 VLLM_BASE_URL"

echo "==> [4/5] 生成 systemd 单元 (可选) / nohup 启动脚本"
cat > "${INSTALL_DIR}/start-server.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "${INSTALL_DIR}"
source hacoo.env
export PYTHONPATH="${INSTALL_DIR}:\${PYTHONPATH:-}"
exec python3 -m hacoo serve --host 127.0.0.1 --port ${HACOO_PORT}
EOF
chmod +x "${INSTALL_DIR}/start-server.sh"

echo "==> [5/5] 冒烟测试: 启动服务 + SDK ping"
cd "${INSTALL_DIR}"
export PYTHONPATH="${INSTALL_DIR}:${PYTHONPATH:-}"
python3 -m hacoo serve --host 127.0.0.1 --port "${HACOO_PORT}" &
SERVER_PID=$!
trap 'kill ${SERVER_PID} 2>/dev/null || true' EXIT
sleep 1.5
HACOO_SERVER="127.0.0.1:${HACOO_PORT}" python3 -m hacoo doctor || true

echo ""
echo "部署完成. 使用方式:"
echo "  1) 启动平台:    ${INSTALL_DIR}/start-server.sh   (或配置 systemd)"
echo "  2) OpenCode:    cd ${INSTALL_DIR} && source hacoo.env && opencode"
echo "     -> 选择 hacoo agent, 直接对话即可 (MCP 自动连接)"
echo "  3) LangChain:   source hacoo.env && python3 -m hacoo.agent_runner \"<任务>\""
echo "  4) Python SDK:  from hacoo.access.sdk import HacooClient"
