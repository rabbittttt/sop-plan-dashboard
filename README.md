# SOP计划看板

本地运行的月度 SOP 计划看板。网页和 Excel 共用同一份计划数据，支持滚动 M+6、锁单对照、版本维护、导入导出与 Excel 快照备份。

## 启动

1. 安装 Python 3.10+，运行 `python -m pip install -r requirements.txt`。
2. 在 `input_file/` 放入原始销量文件。点击“网页维护”即可在主表改车型总量、展开改版本，或增删行，再点击“保存修改”；无需选择维护月份或维护粒度。Excel保留导入、导出能力，`SOP计划维护.xlsx` 是可选的同步文件；没有它时，启动、网页维护和导出都不会自动创建。需要长期表格同步时，到“数据与规则”创建维护Excel。已有业务数据迁移时，同时复制 `data/` 和 `input_file/`。
3. 双击 `启动看板.cmd`，或运行 `./start-sop.ps1`。默认地址为 <http://127.0.0.1:8876/>。
4. 运行 `./停止看板.ps1` 停止本项目服务。

启动脚本优先使用 `SOP_PYTHON_PATH` 指定的 Python，其次使用本机 Codex 运行环境，最后使用 PATH 中的 Python。Excel 生成脚本优先使用 `SOP_NODE_PATH` 指定的 Node，其次使用本机 Codex 运行环境，最后使用 PATH 中的 Node。

**Excel 运行依赖：** 安装 `requirements.txt` 后，维护表、网页导出和快照备份都可由 Python 独立生成，适用于没有 Codex 运行环境的内网机器。若本机已有 `@oai/artifact-tool`，程序仍可使用 Node 生成；该包缺失或 Node 无法启动时自动转用 Python。设置 `SOP_EXCEL_ENGINE=python` 可直接使用 Python 生成。`scripts/node_modules` 是可自动重建的本机链接，不提交到 Git。

**共享代际规则：** 将“计划看板”和“订单分析”放在同级目录，保留 `../订单分析/scripts/config/shared_config.py` 的 `MERGE_GROUPS` 字典。看板每次读取此相对路径，不硬编码车型映射；规则修改后刷新即生效。页面可切换单代际与汇总代际，汇总计划自动相加、只读，未参与汇总的车型仍可维护。规则缺失或无效时会明确报错，不能同时展示两层数量。该外部配置由“订单分析”项目维护，不复制进本仓库。

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

本机已安装 Microsoft Excel 时，可用原生只读打开检查生成文件。此检查使用正常打开方式，遇到需要修复的文件会失败，并核对标题合并、列宽及隐藏列：

```powershell
./tests/verify_excel_native.ps1 -Paths ./tests/artifacts/inline-layout/export.xlsx
```

详细的业务口径、表格维护、导入导出和快照规则见[业务规则与使用说明](docs/业务规则与使用说明.md)。
