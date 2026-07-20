# ADR-0016：终端 Coin-cat 品牌标识

- 状态：Accepted
- 日期：2026-07-19

## 背景

README 使用透明背景的蓝金白像素猫头像。CLI 启动 header 需要具有同一视觉语言的“小猫硬币”，同时适配 SSH、窄终端、无色输出和机器 JSON。

## 问题

应直接显示图片、使用 emoji、制作大型 ASCII banner，还是设计固定字符的终端重绘？

## 候选方案

1. 依赖终端图片协议显示 README PNG。
2. 使用猫 emoji 或大型随机 ASCII 画。
3. 使用 19 列、9 行 Unicode 块字符“像素圆章”，由 Rich 映射灰蓝、柔金、暖白和深色；提供紧凑 ASCII 降级。

## 最终选择

选择方案 3，即 `docs/cli.md` 候选 A。它是 inspired by README cat style 的终端友好重设计，不是原图的逐像素转换。

## 选择原因

固定字符在常见终端可预测，能同时表达硬币、猫耳、眼睛和白色中线，尺寸足以形成品牌感又不会压过对话。去色后仍可读，ASCII fallback 避免 emoji 和 East Asian Width 差异。

## 代价

Unicode block 在少数字体下仍可能有宽度或字形差异；需要 snapshot、窄宽和无色测试。颜色不能作为唯一的信息载体。

## 后续影响

Goal D 在 `cli/logo.py` 落地 Rich Text，header 展示 Agent、Runtime、Project 和 Tool 摘要。JSON 模式不渲染 Logo，非 UTF-8 或 dumb terminal 使用紧凑 ASCII。

## 可逆性

高。字符和色板可以在保持 19 列外框、无色语义和 fallback contract 的前提下迭代。
