# UI 验收

- 桌面：1440 × 1000，三列组件卡，无横向溢出。
- 移动：390 × 844 视口，全页高度 1372，单列组件卡，无横向溢出。
- 健康态：标题为“平台基础设施已就绪”，PostgreSQL、Redis、MinIO 均来自 Live API。
- 降级态：停止 Redis 后标题为“基础设施需要关注”，Redis 卡为异常并展示 API detail。
- 浏览器：两个视口均 0 console error、0 page error。

截图：`web-desktop-1440x1000.png`、`web-mobile-390x1372.png`。机器已有 Playwright/Chromium 缓存用于验收，未加入产品依赖。
