# -*- coding: utf-8 -*-
"""服务端打包入口（供 PyInstaller 使用，避免相对导入问题）。"""
import sys

from server_app.main import main

if __name__ == "__main__":
    sys.exit(main())
