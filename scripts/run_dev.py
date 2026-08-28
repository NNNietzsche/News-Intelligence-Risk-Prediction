"""开发启动：只监视 app/config，并等待文件写完再重载，减少半成品代码把服务打挂。"""

import sys
from pathlib import Path

import uvicorn

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]


if __name__ == "__main__":
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        reload_delay=0.75,
        reload_dirs=[str(ROOT / "app"), str(ROOT / "config")],
        app_dir=str(ROOT),
    )
