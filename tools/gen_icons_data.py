#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""将 assets 下的 .ico 以 base64 形式内嵌进 app_common/icons_data.py。

这样程序图标完全内嵌于代码/可执行文件，符合“单文件、不携带多余素材文件”的要求。
用法：python tools/gen_icons_data.py
"""
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _wrap(b64: str, width: int = 100) -> list[str]:
    return [b64[i:i + width] for i in range(0, len(b64), width)]


def main() -> int:
    assets = ROOT / "assets"
    out = ROOT / "app_common" / "icons_data.py"
    blocks = [
        "# -*- coding: utf-8 -*-",
        '"""图标内嵌数据（base64，由 tools/gen_icons_data.py 自动生成，勿手改）。"""',
        "",
    ]
    for role, var in (("client", "CLIENT_ICO_B64"), ("server", "SERVER_ICO_B64")):
        ico = assets / f"icon_{role}.ico"
        if not ico.exists():
            print(f"缺少图标: {ico}，请先运行 tools/make_icon.py")
            return 1
        b64 = base64.b64encode(ico.read_bytes()).decode("ascii")
        lines = _wrap(b64)
        blocks.append(f"{var} = (")
        for line in lines:
            blocks.append(f"    '{line}'")
        blocks.append(")")
        blocks.append("")
    out.write_text("\n".join(blocks), encoding="utf-8")
    print(f"已生成内嵌图标数据: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
