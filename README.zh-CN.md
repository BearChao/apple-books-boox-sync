# Apple Books ↔ BOOX 同步工具

这是一个在本地运行的实验性工具，用于安全对齐 macOS Apple Books 与通过 USB 连接的 BOOX 阅读器。

它可以双向补齐缺失的无 DRM 书籍文件、把 Apple Books 自建 Collection 对齐到 BOOX，并按不会回退的规则合并阅读百分比和完成状态。所有数据库写入前都会创建备份，写入后会重新打开两端阅读器并回读验证。

> [!WARNING]
> Apple Books 与 BOOX 数据结构都不是公开稳定接口。请先运行只读 `check`，认真检查结果，并另外保存自己的书库备份。

[English README](README.md)

## 功能

- 双向补齐缺失书籍文件，只增加、不自动删除。
- 自动发现 Apple Books 的自建 Collection，也可以显式选择或排除。
- 幂等创建 BOOX Collection、对齐成员关系并清理重复关系。
- 阅读进度采用“较大当前进度胜出、已读胜出、0% 不覆盖”的单调合并规则。
- 写入前备份 Apple SQLite 数据库和 BOOX ContentProvider 快照。
- 阅读进度先试同步 5 本，回读成功后才批量执行。
- 写入后重启 Apple Books 和 BOOX 阅读器并再次校验。
- 仅使用 Python 标准库。

## 兼容范围

- 安装了 Apple Books 和 Python 3.10 以上版本的 macOS。
- 已开启 USB 调试、允许当前 Mac 调试，并能访问 ONYX ContentProvider 的 BOOX 设备。
- 已在 BOOX Leaf2_P 上验证。不同型号和固件的数据结构可能不同；环境检查失败时不要绕过。
- 支持无 DRM 的 EPUB、PDF、MOBI、AZW/AZW3、DJVU、FB2、CBZ/CBR、RTF、DOC 和 DOCX。项目不会移除或绕过 DRM。

## 安装

```bash
git clone https://github.com/BearChao/apple-books-boox-sync.git
cd apple-books-boox-sync
brew install android-platform-tools libmtp
python3 apple_boox_sync.py doctor
```

只有需要把 BOOX 独有的 MOBI 转为 Apple Books 可导入格式时，才需要 Calibre：

```bash
brew install --cask calibre
```

## 使用

可以双击 `sync.command`，也可以在终端执行：

```bash
python3 apple_boox_sync.py check
python3 apple_boox_sync.py sync
```

所有写入命令默认要求输入大写 `SYNC`。明确需要无人值守运行时可以使用：

```bash
python3 apple_boox_sync.py sync --yes
```

| 命令 | 行为 |
| --- | --- |
| `doctor` | 检查依赖、本地数据库、ADB 授权和 BOOX 数据接口。 |
| `check` | 只读核对书目、Collection 和阅读进度。 |
| `catalog` | 双向补齐缺失书籍文件。 |
| `collections` | 备份并将 Apple Books 自建 Collection 对齐到 BOOX。 |
| `progress` | 备份、试同步、批量合并、重启并校验阅读进度。 |
| `sync` | 依次同步书目、Collection 和阅读进度。 |
| `backups` | 列出本工具创建的备份。 |

Apple Books 自动导入未完成时，可以改用手动导入：

```bash
python3 apple_boox_sync.py catalog --manual-apple-import
```

## 配置

默认不需要配置文件。如需定制：

```bash
cp config.example.json config.json
```

```json
{
  "collections": null,
  "exclude_collections": [],
  "boox_books_dir": "/sdcard/Books",
  "match_threshold": 0.8,
  "require_collection_for_every_book": false
}
```

- `collections: null` 自动发现全部自建 Collection；也可以填写需要同步的名称数组。
- `exclude_collections` 排除不需要同步的名称。
- `boox_books_dir` 设置 Apple 独有书籍推送到 BOOX 的目录。
- `match_threshold` 是 0 到 1 的书名模糊匹配阈值；降低前必须检查匹配结果。
- `require_collection_for_every_book` 开启后，存在未分类书籍时会停止 Collection 写入。

也可以通过环境变量覆盖本地路径：

```bash
export APPLE_BOOX_CONFIG=/path/to/config.json
export APPLE_BOOX_SYNC_STATE_DIR=/path/to/state
export APPLE_BOOKS_DB=/path/to/BKLibrary.sqlite
export APPLE_BOOKS_ANNOTATION_DB=/path/to/AEAnnotation_local.sqlite
```

## 同步边界

书籍通过标准化标题、EPUB 元数据、BOOX 元数据、文件名和连续刊物编号进行匹配。默认阈值为 `0.8`；匹配无法闭环时，书目和 Collection 写入会停止。

Apple 当前进度读取 `ZREADINGPROGRESS`，不会把历史最远位置 `ZBOOKHIGHWATERMARKPROGRESS` 当成当前位置。BOOX 使用当前/总量进度字段和 `readingStatus`。

工具不会强行转换 Apple EPUB CFI 与 BOOX NeoReader 内部锚点，因此百分比和完成状态可以一致，但跨端打开时不保证精确落到同一段文字。

## 安全与隐私

- 全部处理都在本机完成，不会把书名、元数据或书籍上传到网络服务。
- 书籍文件只增加，不会自动删除。
- 每次写入前都会在 `state/backups/` 保存数据库或 ContentProvider 快照。
- 进度写入前退出两端阅读器，写入后重新打开并回读，降低缓存覆盖风险。
- BOOX Collection 和关系采用软停用，不物理删除数据库记录。
- Apple 和 ONYX 都不保证内部数据库兼容性；系统或固件升级后可能失效。
- 请额外保留独立备份。本项目按 MIT License 提供，不承担数据丢失担保。

提交安全问题或诊断日志前请阅读 [SECURITY.md](SECURITY.md)。参与开发请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## License

[MIT](LICENSE)
