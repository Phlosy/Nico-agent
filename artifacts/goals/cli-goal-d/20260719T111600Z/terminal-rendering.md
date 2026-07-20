# 终端渲染证据说明

`cli-e2e/tty.txt` 是 `TERM=xterm-256color` 的真实 PTY transcript，包含 ANSI 颜色和 19×9 Unicode coin-cat。验收会先去除 ANSI 再比对完整 `▄██████▄` 轮廓，避免颜色 span 分隔字符导致误判。

`cli-e2e/non-tty.txt` 是 human 输出重定向，使用无 ANSI 的 5 行 ASCII 紧凑猫币。`cli-e2e/slash.txt` 是真实 prompt_toolkit PTY 中依次执行 `/help`、`/inspect` 和 `/exit` 的 transcript。

`exec-attached.json`、`exec-detached.json`、`watch.json` 与 `watch-after.json` 均为单一 JSON 文档且不含 ANSI 或 Logo。
