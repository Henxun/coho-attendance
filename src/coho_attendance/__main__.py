"""兼容入口：允许 `python -m coho_attendance` 直接启动 server。"""

from .server import main

if __name__ == "__main__":
    main()
