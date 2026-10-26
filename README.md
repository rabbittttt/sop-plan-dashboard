# SOP计划看板

本地运行的月度 SOP 计划看板。网页和 Excel 共用同一份计划数据，支持滚动 M+6、锁单对照、版本维护、导入导出与 Excel 快照备份。

## 启动

1. 安装 Python 3.10+，运行 `python -m pip install -r requirements.txt`。
2. 在 `input_file/` 放入原始销量文件；同目录的 `SOP计划维护.xlsx` 用于手工维护计划。没有这份维护表时，网页仍可从统一数据导出计划。已有业务数据迁移时，同时复制 `data/` 和 `input_file/`。
3. 双击 `启动看板.cmd`，或运行 `./start-sop.ps1`。默认地址为 <http://127.0.0.1:8876/>。
4. 运行 `./停止看板.ps1` 停止本项目服务。

启动脚本优先使用 `SOP_PYTHON_PATH` 指定的 Python，其次使用本机 Codex 运行环境，最后使用 PATH 中的 Python。Excel 生成脚本优先使用 `SOP_NODE_PATH` 指定的 Node，其次使用本机 Codex 运行环境，最后使用 PATH 中的 Node。

**Excel 运行依赖：** 当前的维护表、导出和备份生成使用 `@oai/artifact-tool`。本机 `scripts/node_modules` 是指向 Codex 运行环境的链接，已排除在 Git 之外。将仓库克隆到另一台机器时，必须先提供兼容的该依赖；仅安装 `requirements.txt` 不足以使用 Excel 生成功能。计划部署内网时需要处理这项依赖。

## 目录

| 路径 | 用途 | 提交到 Git |
| --- | --- | --- |
| `server.py`、`sop_*.py` | 服务、数据、计划及 Excel 同步逻辑 | 是 |
| `web/` | 页面和图标 | 是 |
| `scripts/` | Excel 生成与数据检查脚本 | 是，`node_modules` 除外 |
| `tests/` | 自动化测试 | 是，生成文件除外 |
| `docs/` | [业务规则与使用说明](docs/业务规则与使用说明.md) | 是 |
| `input_file/` | 原始销量文件及 `SOP计划维护.xlsx` | 否 |
| `data/` | 数据库、来源解析、设置、快照及日志 | 否 |
| `output_file/` | 临时导出和本地核验文件 | 否 |

源代码仍位于项目根目录，保持现有 Python 导入和启动方式；运行数据与临时文件通过 `.gitignore` 排除。换电脑时，复制 `data/` 和 `input_file/` 才能保留当前业务状态。不要仅依靠 Git 仓库保存计划或历史快照。

## 验证

```powershell
python -m unittest discover -s tests -v
```

详细的业务口径、表格维护、导入导出和快照规则见[业务规则与使用说明](docs/业务规则与使用说明.md)。
