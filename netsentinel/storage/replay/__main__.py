"""``python -m netsentinel.storage.replay`` 入口(包形态,替代原单文件的
``if __name__ == "__main__"`` 分支;CLI 逻辑见 :mod:`.cli`)。
"""
from netsentinel.storage.replay import main

if __name__ == "__main__":  # pragma: no cover - 手工运行入口
    raise SystemExit(main())
