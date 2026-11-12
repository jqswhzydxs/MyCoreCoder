# 本轮验证记录

日期：2026-10-26（Asia/Singapore）。

## 验证范围

- 原版源码：CoreCoder 0.8.0，提交 `bd64cfe7ad2789fa0ab1b3a689ad160567b5cdb0`。
- 本轮只新增学习文档和 `examples/learning_loop_trace.py`，没有修改 `corecoder/`、原有测试或依赖声明。
- 运行环境：Windows，Python 3.14.8，仓库内 `.venv` 可编辑安装。
- 直接依赖的实际安装版本：openai 3.37.0、rich 15.0.0、prompt_toolkit 3.0.53、python-dotenv 1.2.4；检查工具为 pytest 9.1.1、ruff 0.17.2。
- 依赖按项目声明解析安装；以上是本次环境记录，不是项目提交时的依赖锁定结果，也不代表已验证所有允许的版本组合。

## 已完成检查

| 检查 | 结果 |
|---|---|
| 原版离线测试：`python -m pytest tests --ignore=tests/test_live_smoke.py -q` | **227 passed in 22.05s** |
| `learning_loop_trace.py` 默认 `read` 场景 | 两次模型接口调用；四条历史消息；读到真实临时文件内容 |
| `--scenario missing` | 文件不存在的错误作为 tool 结果回填，继续第二轮 |
| `--scenario denied` | 写入被 Permission 拒绝；拒绝原因回填；文件内容保持原样 |
| `python -m ruff check corecoder tests examples/learning_loop_trace.py` | All checks passed |
| `python -m pip check` | No broken requirements found |

测试运行时为当前进程设置了 `PYTHONUTF8=1`，并将 `.venv/Scripts` 加入 PATH，以便测试内部的 shell 命令使用相同 Python。新示例仅使用临时文件，未调用真实模型。

原有测试的 `conftest.py` 会将 checkpoint 与价格文件替换为临时路径；MCP 测试使用本地伪服务器。227 项通过说明当前环境下这些测试的断言通过，不等于真实模型任务成功率或所有安全边界都已获得证明。

## 尚未验证的部分

- 真实模型端到端调用：本轮显式排除 `test_live_smoke.py`，没有使用 API key。
- LiteLLM 的真实外部 provider：相关单元测试通过，但使用模拟后端。
- 仓库 CI 声明的其他系统及 Python 版本组合：本轮只在当前 Windows / Python 3.14.8 环境执行，未复跑完整 CI 矩阵。
- 学习者是否掌握原版：需要通过第一课复述与后续实验确认；目前未将学习进度标记为已掌握，也未实施简历改造。
