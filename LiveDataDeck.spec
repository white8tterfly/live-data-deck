# -*- mode: python ; coding: utf-8 -*-
"""直播数据舱绿色版打包配置（PyInstaller onedir）。

打包：pyinstaller LiveDataDeck.spec --noconfirm
产物：dist/LiveDataDeck/LiveDataDeck.exe（web 资源在 _internal/web，只读）
      config/ 与 data/ 首次运行时自动创建在 exe 同级目录。
只打包 web/ 静态资源，不含本机 config/settings.json（含凭证）。
"""
from PyInstaller.utils.hooks import collect_submodules

# uvicorn 有些子模块是按字符串动态导入的，收集起来免得运行时缺模块报错
hidden = [
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.protocols.websockets.auto",
    "anyio._backends._asyncio",
]
hidden += collect_submodules("uvicorn.lifespan")

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=[("web", "web")],
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 用不到的绘图/科学计算等大包直接排除，缩体积
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LiveDataDeck",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LiveDataDeck",
)
