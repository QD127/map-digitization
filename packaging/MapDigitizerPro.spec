# -*- mode: python ; coding: utf-8 -*-

import os

from PyInstaller.utils.hooks import collect_all


block_cipher = None

datas = []
binaries = []
hiddenimports = []

# 工程根目录（program/）
# PyInstaller 执行 spec 时不一定定义 __file__，但会注入 SPECPATH（spec 所在目录）。
ROOT = os.path.abspath(os.path.join(globals().get("SPECPATH", os.getcwd()), ".."))

# GUI 程序入口（用绝对路径，避免从 packaging/ 解析出错）
entry_script = os.path.join(ROOT, "app.py")

# 运行时需要的项目文件
datas += [(os.path.join(ROOT, "centers_rgb_manual_hex.json"), ".")]

# 打包 GIS/栅格依赖。
# 注意：不要对 numpy/scipy/pandas/geopandas 做 collect_all，会显著拖慢构建并把大量不相关可选依赖打进包里。
# 这里仅显式收集“容易漏掉的二进制/数据资源”。
for pkg in [
    "rasterio",
    "pyproj",
    "pyogrio",
    "shapely",
]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

a = Analysis(
    [entry_script],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # GUI/绘图/科学计算大包（本项目不需要；在“干净环境”里也不应出现）
        "PyQt5",
        "qtpy",
        "matplotlib",
        "skimage",
        "sklearn",
        "sympy",
        "astropy",
        "cv2",
        "torch",
        "torchvision",
        "transformers",
        "timm",
        "notebook",
        "jupyterlab",
        "distributed",
        "dask",
        "xarray",
        "bokeh",
        "h5py",
        "netCDF4",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MapDigitizerPro",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
