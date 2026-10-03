"""共享 pytest 配置:确保项目根目录在 sys.path 上(规范文件,禁改)。"""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
