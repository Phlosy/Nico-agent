# Goal B 执行命令

```bash
scripts/bootstrap.sh
scripts/test.sh
scripts/test-integration.sh
scripts/e2e.sh
scripts/verify-goal-b.sh
```

迁移可逆性：

```bash
.venv/bin/alembic -c backend/alembic.ini downgrade base
.venv/bin/alembic -c backend/alembic.ini upgrade head
```

UI：

```bash
npx --yes playwright@1.55.0 screenshot --browser chromium --viewport-size '1440,1000' --full-page http://127.0.0.1:18080 web-desktop.png
npx --yes playwright@1.55.0 screenshot --browser chromium --viewport-size '390,844' --full-page http://127.0.0.1:18080 web-mobile.png
```

故障注入：

```bash
docker compose stop redis
curl http://127.0.0.1:18000/api/v1/health/ready
docker compose start redis
```

清理：

```bash
scripts/cleanup.sh --volumes
```
