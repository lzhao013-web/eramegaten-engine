# eraMegaten Engine

eraMegaten Engine 是一个用于解析、运行和测试 EraBasic / Emuera 风格脚本的 Python 引擎。

本仓库只包含引擎源码、命令行工具和测试，不包含游戏脚本、素材、存档或其他游戏数据。

## 使用

```powershell
python -m pip install -e .
python -m pytest
```

启动检阅前端：

```powershell
python -m eramegaten_engine.gui
```

前端支持可筛选操作面板、右键批量跳过消息、数字直输、中键拖动、缩放/适宽和输入历史。

## 状态

项目仍在实验阶段，适合兼容性验证、脚本分析和自动化回放。
