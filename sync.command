#!/bin/zsh

set -u

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR" || exit 1

echo "Apple Books ↔ BOOX Sync / 同步工具"
echo
echo "1) Read-only check / 只读检查"
echo "2) Full sync / 完整同步"
echo "3) Catalog only / 只同步书籍目录"
echo "4) Collections only / 只同步 Collection"
echo "5) Progress only / 只同步阅读进度"
echo "6) List backups / 查看备份"
echo "7) Environment check / 环境检查"
echo
read "choice?Choose / 请选择 [1-7]："

case "$choice" in
  1) python3 apple_boox_sync.py check ;;
  2) python3 apple_boox_sync.py sync ;;
  3) python3 apple_boox_sync.py catalog ;;
  4) python3 apple_boox_sync.py collections ;;
  5) python3 apple_boox_sync.py progress ;;
  6) python3 apple_boox_sync.py backups ;;
  7) python3 apple_boox_sync.py doctor ;;
  *) echo "Invalid choice / 无效选项"; exit 2 ;;
esac

status=$?
echo
echo "Finished / 命令结束，exit / 退出码：$status"
read "reply?Press Enter to close / 按回车关闭窗口……"
exit "$status"
