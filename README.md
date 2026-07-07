# eraMegaten Engine

一个用于解析、运行和测试 EraBasic / Emuera 风格脚本的 Python 兼容引擎。

本仓库只包含引擎实现和测试，不包含游戏脚本、素材、存档或其他游戏数据。

## 功能概览

- 加载 ERB / ERH 脚本和 CSV 数据。
- 执行常见控制流、表达式、变量、数组、输入输出和存档辅助逻辑。
- 提供命令行工具用于审计、查看和重放脚本入口。
- 包含运行时、图形和兼容性回归测试。

## 基本使用

```powershell
python -m pip install -e .
python -m pytest tests\test_engine.py -q
```

## 项目状态

项目仍在实验阶段，适合脚本分析、自动化回放和兼容性验证。
