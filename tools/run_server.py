"""命令行启动合并申报服务端。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from group_filing.server import main


if __name__ == "__main__":
    main(sys.argv[1:])
