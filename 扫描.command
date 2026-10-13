#!/bin/bash
# macOS 双击入口。第一次双击被拦：右键 → 打开 → 打开。
cd "$(dirname "$0")" || exit 1

if ! python3 -c "import sys" >/dev/null 2>&1; then
  echo "没找到 python3。去 https://www.python.org/downloads/ 装一个再双击。"
  read -r -p "按回车关闭"; exit 1
fi
# 依赖装进本目录的 .venv：Homebrew 版 Python 不许往系统里 pip install
PY=python3
[ -x .venv/bin/python ] && PY=.venv/bin/python
if ! "$PY" -c "import requests, bs4" >/dev/null 2>&1; then
  echo "第一次运行：安装两个依赖包（约半分钟）..."
  python3 -m venv .venv && .venv/bin/python -m pip install -q -r requirements.txt || {
    echo "装包失败，把上面的报错发给帮你装的人。"
    read -r -p "按回车关闭"; exit 1; }
  PY=.venv/bin/python
fi

echo "开始扫 qms.avs.gov.sg，大约 5-8 分钟（请求之间故意随机停 3-7 秒，别关窗口）"
echo
"$PY" qms_scan.py
rc=$?
echo
case $rc in
  0) echo "完成。看板已在浏览器打开，快照 JSON 存在本目录。" ;;
  2) echo "_cookie.txt 里没有有效登录态，没有扫描。教程页已在浏览器打开。" ;;
  4) echo "config.txt 有问题（见上面的提示），没有扫描。" ;;
  *) echo "扫描失败（代码 $rc），详情看 _probe.log。" ;;
esac
read -r -p "按回车关闭"
