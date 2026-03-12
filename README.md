# Map Digitizer Pro（按图例颜色数字化）

把一张“已去除地名/河流/行政边界/图例等干扰信息”的地图图片（PNG/JPG 等）按图例颜色分成 *K* 类，并导出 **GIS 矢量面要素**（Shapefile / GeoPackage / GeoJSON），支持小斑点合并、预览、取色笔与参数调节。

- GUI：`app.py`（Tkinter，内置缩放与拖动）
- 核心算法：`classify_by_color.py`（可独立 CLI 运行）
- 许可证：MIT（见 `LICENSE`）

---

## 1. 功能概览

- 输入：常见图片格式（`.png` / `.jpg` / `.jpeg` / `.tif` 等，自动转 RGB）
- 图例颜色（K 可变）：
  - 手动输入 HEX：`b31e22` 或 `#b31e22`
  - 手动输入 RGB：`179,30,34` 或 `179 30 34`
  - **取色笔**：导入图片后直接在原图点击取色（支持 `auto / replace / add` 行为）
  - 左侧色板列表支持滚动（颜色数量多时不挤占其它控件）
- 预览：生成“数字化着色预览图”
- 小斑点处理：
  - `drop`：小面直接变为背景（0）
  - `merge`：小面并入临近类别（推荐）
- 导出：
  - `*.shp`（推荐：对 ArcGIS 兼容最好）
  - `*.gpkg`（ArcGIS Pro/QGIS 通常可用；ArcMap 不支持）
  - `*.geojson`
  - 同时输出：`*_labels.png`、`*_preview.png`、`*_meta.json`、`*_centers_rgb.json`

---

## 2. 安装与运行（源码）

### 2.1 推荐：Conda（Windows 最省心）

```powershell
cd D:\code\codex\digitization_1\program
conda create -n mapdigitizer python=3.11 -y
conda activate mapdigitizer

# GIS 相关依赖建议走 conda-forge
conda install -c conda-forge numpy pillow scipy shapely rasterio geopandas pyogrio -y
```

运行 GUI：
```powershell
python .\app.py
```

### 2.2 备选：pip（可能会遇到 GDAL/PROJ 依赖问题）

```powershell
cd D:\code\codex\digitization_1\program
python -m pip install -r .\requirements.txt
python .\app.py
```

---

## 3. GUI 使用流程（推荐）

1. 点击“选择…”导入地图图片（建议：已清理成“纯色块 + 白色背景”）。
2. 在“图例颜色”区域：
   - 手动输入颜色；或勾选“取色笔”后在原图点击取色。
   - 取色模式：
     - `auto`：有空行就填空行；否则替换当前行
     - `replace`：替换当前选中行
     - `add`：直接新增一行
3. 调参（建议先调这几个）：
   - `Color Tolerance (tol)`：颜色容差（越大越“宽松”）
   - `White Threshold`：白背景阈值（背景不够白可调低）
   - `Min Pixels` + `Small Action`：小斑点阈值与处理方式（想更强力合并就把阈值调高）
4. 点“Run Preview”查看预览；满意后点“Export”导出矢量。

---

## 4. 预览窗口操作（缩放 + 拖动）

- 缩放：鼠标滚轮
- 拖动：中键或右键按住拖动平移

---

## 5. 关键参数解释（对应 GUI）

- `tol`（默认 12）：按 Lab 颜色距离做阈值分类；边缘有抗锯齿/颜色混合时可适当调大（例如 14~24）。
- `white_thresh`（默认 245）：`RGB>=white_thresh` 认为是背景；背景泛灰时调低。
- `fill_unknown`（默认开启）：把未知像素/文本/线条像素回填到最近的类别（更“连续”，但可能吃掉细线）。
- `min_pixels`（默认 200）：小斑点阈值（像素数）；**想合并更彻底就调高**（例如 500、1000、3000…需看图分辨率）。
- `small_action`：
  - `merge`：小斑点并入临近面（推荐）
  - `drop`：小斑点置为背景（0）
- `simplify`：矢量简化容差（坐标单位；0 表示不简化）。如果输出是像素坐标，单位≈像素。
- `dissolve`：按 `class_id` 融合（每一类可能变成一个或多个 MultiPolygon）
- `ref_raster`（重要）：参考栅格（GeoTIFF 等），用于拷贝 transform/CRS 生成“带真实坐标”的矢量。
- `crs`：输出 CRS（如 `EPSG:4326`）。如果提供了 `ref_raster` 且其自带 CRS，可留空让程序沿用。

---

## 6. 坐标系与地理配准（非常重要）

### 6.1 你有同宽高的 GeoTIFF（推荐）

如果你有一张与输入图片**宽高完全一致**的参考栅格（例如同尺寸 GeoTIFF），在 GUI 里设置 `ref_raster`：

- 输出矢量会使用参考栅格的 transform/CRS（ArcGIS/QGIS 里就是“真正地理坐标”）

> 注意：`ref_raster` **必须与输入图片同宽同高**，否则无法一一对应像素。

### 6.2 你没有地理参考

仍然可以导出矢量，但坐标本质是“像素坐标”（左上角为原点附近）。GUI 默认 `crs=EPSG:4326` 只是为了让部分软件不报“未知坐标系”，**并不代表真实地理位置**。

---

## 7. 输出文件说明

假设输出基名为 `zones`，导出目录为 `outputs/`：

- `zones_labels.png`：标签栅格（灰度 PNG，值域 0..K，0=背景/未知）
- `zones_preview.png`：按中心色渲染的预览图
- `zones_meta.json`：像素统计与中心色信息
- `zones_centers_rgb.json`：本次导出使用的中心色（便于复现）
- `zones.shp`：矢量面（字段：`class_id`）
- `zones.gpkg`：矢量面（同上；图层名为你设置的 basename）
- `zones.geojson`：矢量面（同上）

---

## 8. ArcGIS 打开说明（重点）

- **最推荐：Shapefile（`.shp`）**，兼容性最好。
- `.gpkg`：
  - ArcGIS Pro 通常可直接打开；
  - ArcMap 不支持 GeoPackage（建议用 `.shp` 或 `.geojson`）。

如果你遇到“ArcGIS 打不开 GPKG”的情况，请优先：
1) 改用 `.shp` 输出；或  
2) 确认你使用的是 ArcGIS Pro（不是 ArcMap）；或  
3) 用 QGIS 试打开以排除文件本身是否损坏。

---

## 9. 打包为 EXE（Windows）

本项目已提供 PyInstaller spec 与脚本：

```powershell
cd D:\code\codex\digitization_1\program
python -m pip install pyinstaller
pyinstaller --noconfirm --clean .\packaging\MapDigitizerPro.spec
```

产物默认在：
- `dist\MapDigitizerPro\MapDigitizerPro.exe`（onedir 方式）

如系统禁止执行 `.ps1`，可用：
```powershell
powershell -ExecutionPolicy Bypass -NoProfile -File .\packaging\build_exe.ps1
```

---

## 10. 命令行用法（可选）

核心脚本支持 CLI（适合批处理/复现）：

```powershell
cd D:\code\codex\digitization_1\program
python .\classify_by_color.py `
  --in .\your_map.png `
  --k 8 `
  --centers-json .\centers_rgb_manual_hex.json `
  --tol 12 `
  --white-thresh 245 `
  --fill-unknown `
  --out-label .\outputs\zones_labels.png `
  --out-preview .\outputs\zones_preview.png `
  --out-meta .\outputs\zones_meta.json `
  --out-shp .\outputs\zones.shp `
  --min-pixels 1000 `
  --small-action merge
```

---

## 11. 开源发布建议（仓库结构）

建议把 `program/` 作为一个独立仓库发布（这样 `README.md` / `LICENSE` / `requirements.txt` 都在根目录）。

本目录已包含 `.gitignore`（默认忽略 `outputs/`、`build/`、`dist/` 等产物）。

> 不建议把 `dist/` 打进 Git 历史；更推荐用 GitHub Releases 发布 EXE。

---

## 12. 贡献与反馈

- 欢迎提交 Issue / PR（建议同时附：输入图片、图例颜色、参数与预期结果）。
- 你也可以把“图例颜色中心值 JSON”（如 `*_centers_rgb.json`）一并提供，方便复现与定位问题。

