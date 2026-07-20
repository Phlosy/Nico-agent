# 错误与修正记录

1. 初次 TTY 验收直接搜索彩色字符串时失败，因为 Rich 为每个调色区域插入 ANSI span。验收改为先剥离 ANSI，再验证完整 coin-cat 轮廓；彩色输出本身未降级。
2. 环境存在 `NO_COLOR=1` 时，PTY 会按合同退回 ASCII。彩色验收显式移除该环境变量并设置 `TERM=xterm-256color`，无色路径仍单独保留。
3. `--output` 的底层 I/O 异常已统一映射为安全 `EXEC_OUTPUT_UNWRITABLE`，不会向用户暴露 traceback。

最终验收无未解决错误。
