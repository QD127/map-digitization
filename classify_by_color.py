# -*- coding: utf-8 -*-
"""
把一张“只有少量离散颜色”的地图按颜色中心分成 K 类（默认 8 类），并可导出 ArcGIS 可直接打开的面要素。

特点：
- 不依赖 ArcGIS / arcpy：直接读取 png/jpg 等普通图片。
- 复用本目录 `digitize_by_color_arcpy.py` 的核心思路：RGB->Lab，用 ΔE(欧氏近似) 距离做阈值归类。
- 可输出：标签栅格、预览图、统计 JSON、矢量面（Shapefile/GPKG/GeoJSON）。
- 小碎斑处理：可丢弃（drop）或并入临近面体（merge）。

提示：
- 若要“带空间参考”的矢量，请提供与输入图片同宽高的 `--ref-raster`（用于 transform/CRS）。
- 如果输入图片本身没有真实坐标系，`--crs EPSG:4326` 只是为了让 ArcGIS 识别为“有坐标系”，并不代表真实地理坐标。
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Dict, Tuple

import numpy as np
from PIL import Image

try:  # 仅在输出矢量时需要
    import rasterio  # type: ignore
    from rasterio.features import shapes as raster_shapes  # type: ignore
    from rasterio.transform import from_origin  # type: ignore
except Exception:  # pragma: no cover
    rasterio = None
    raster_shapes = None
    from_origin = None

try:  # 仅在输出矢量时需要
    import geopandas as gpd  # type: ignore
except Exception:  # pragma: no cover
    gpd = None

try:  # 仅在输出矢量/碎斑清理时需要
    from shapely.geometry import shape as shp_shape  # type: ignore
except Exception:  # pragma: no cover
    shp_shape = None

try:  # 仅在修复几何时需要
    import shapely  # type: ignore
except Exception:  # pragma: no cover
    shapely = None

try:  # 仅在碎斑清理时需要
    from scipy import ndimage as ndi  # type: ignore
except Exception:  # pragma: no cover
    ndi = None


def _srgb_to_linear(c: np.ndarray) -> np.ndarray:
    c = c / 255.0
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def _rgb_to_lab(rgb_u8: np.ndarray) -> np.ndarray:
    """RGB uint8 -> Lab float32 (D65). 输出范围大致 L[0,100]."""
    rgb = rgb_u8.astype(np.float32)
    r = _srgb_to_linear(rgb[..., 0])
    g = _srgb_to_linear(rgb[..., 1])
    b = _srgb_to_linear(rgb[..., 2])

    # sRGB -> XYZ (D65)
    x = r * 0.4124564 + g * 0.3575761 + b * 0.1804375
    y = r * 0.2126729 + g * 0.7151522 + b * 0.0721750
    z = r * 0.0193339 + g * 0.1191920 + b * 0.9503041

    # Normalize by reference white
    x = x / 0.95047
    y = y / 1.0
    z = z / 1.08883

    eps = 216.0 / 24389.0
    kappa = 24389.0 / 27.0

    def f(t: np.ndarray) -> np.ndarray:
        return np.where(t > eps, np.cbrt(t), (kappa * t + 16.0) / 116.0)

    fx = f(x)
    fy = f(y)
    fz = f(z)

    l = 116.0 * fy - 16.0
    a = 500.0 * (fx - fy)
    bb = 200.0 * (fy - fz)
    return np.stack([l, a, bb], axis=-1).astype(np.float32)


def _make_masks(rgb: np.ndarray, white_thresh: int) -> Tuple[np.ndarray, np.ndarray]:
    r = rgb[..., 0]
    g = rgb[..., 1]
    b = rgb[..., 2]

    background = (r >= white_thresh) & (g >= white_thresh) & (b >= white_thresh)

    # 黑字/灰字、红边界、蓝河流（按经验阈值）
    dark = (r.astype(np.int16) + g.astype(np.int16) + b.astype(np.int16)) < 220
    redline = (r > 180) & (g < 110) & (b < 110)
    blueline = (b > 160) & (r < 140) & (g < 170)

    artifacts = dark | redline | blueline
    return background, artifacts


def _load_centers(path: str) -> np.ndarray:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    # 兼容 {"1":[...],...} 或 [[...],...]
    if isinstance(data, dict):
        keys = sorted(data.keys(), key=lambda x: int(x))
        arr = np.array([data[k] for k in keys], dtype=np.float32)
    else:
        arr = np.array(data, dtype=np.float32)
    if arr.ndim != 2 or arr.shape[1] != 3:
        raise SystemExit(f"centers-json 格式不正确：期望 Nx3，实际={arr.shape}")
    return arr


def _save_centers(path: str, centers_rgb: np.ndarray) -> None:
    payload: Dict[str, list[float]] = {str(i + 1): [float(x) for x in centers_rgb[i]] for i in range(centers_rgb.shape[0])}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _kmeans_fit(x: np.ndarray, k: int, iters: int, seed: int) -> np.ndarray:
    """非常轻量的 KMeans：仅用于从图像像元估计离散调色板中心色。"""
    rng = np.random.default_rng(seed)
    if x.shape[0] < k:
        raise SystemExit(f"KMeans 样本数({x.shape[0]})小于 k={k}")

    # kmeans++ 初始化
    centers = np.empty((k, 3), dtype=np.float32)
    centers[0] = x[rng.integers(0, x.shape[0])]
    d2 = ((x - centers[0]) ** 2).sum(axis=1)
    for i in range(1, k):
        probs = d2 / max(d2.sum(), 1e-12)
        centers[i] = x[rng.choice(x.shape[0], p=probs)]
        d2 = np.minimum(d2, ((x - centers[i]) ** 2).sum(axis=1))

    for _ in range(iters):
        # 分配
        dist2 = ((x[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        labels = dist2.argmin(axis=1)
        # 更新
        new_centers = centers.copy()
        for i in range(k):
            sel = x[labels == i]
            if sel.size:
                new_centers[i] = sel.mean(axis=0)
            else:
                new_centers[i] = x[rng.integers(0, x.shape[0])]
        if np.allclose(new_centers, centers, atol=0.5):
            centers = new_centers
            break
        centers = new_centers
    return centers


def _classify_by_lab_distance(
    rgb: np.ndarray,
    background: np.ndarray,
    artifacts: np.ndarray,
    centers_rgb: np.ndarray,
    tol: float,
    fill_unknown: bool,
    chunk: int,
) -> np.ndarray:
    lab = _rgb_to_lab(rgb)
    centers_lab = _rgb_to_lab(centers_rgb.reshape(1, -1, 3).astype(np.uint8)).reshape(-1, 3)

    h, w, _ = lab.shape
    flat = lab.reshape(-1, 3)

    pred_mask = (~background).reshape(-1)
    labels = np.full(flat.shape[0], -1, dtype=np.int16)

    idx = np.where(pred_mask)[0]
    for start in range(0, idx.size, chunk):
        sel = idx[start : start + chunk]
        x = flat[sel].astype(np.float32)
        d2 = ((x[:, None, :] - centers_lab[None, :, :]) ** 2).sum(axis=2)
        arg = d2.argmin(axis=1)
        dmin = np.sqrt(d2[np.arange(d2.shape[0]), arg])

        out = (arg + 1).astype(np.int16)
        out[dmin > tol] = -1
        labels[sel] = out

    labels = labels.reshape(h, w)
    labels[background] = -1

    if fill_unknown:
        need = ((labels == -1) & (~background)) | artifacts
        if need.any():
            need_idx = np.where(need.reshape(-1))[0]
            for start in range(0, need_idx.size, chunk):
                sel = need_idx[start : start + chunk]
                x = flat[sel].astype(np.float32)
                d2 = ((x[:, None, :] - centers_lab[None, :, :]) ** 2).sum(axis=2)
                labels.reshape(-1)[sel] = (d2.argmin(axis=1) + 1).astype(np.int16)

    return labels


def _read_rgb_image(path: str) -> np.ndarray:
    img = Image.open(path)
    if img.mode in ("RGBA", "LA"):
        base = Image.new("RGBA", img.size, (255, 255, 255, 255))
        img = Image.alpha_composite(base, img.convert("RGBA")).convert("RGB")
    else:
        img = img.convert("RGB")
    return np.asarray(img, dtype=np.uint8)


def _write_label_png(path: str, labels: np.ndarray, k: int) -> None:
    out = labels.copy()
    out[(out < 0) | (out > k)] = 0
    img = Image.fromarray(out.astype(np.uint8), mode="L")
    img.save(path)


def _write_preview_png(path: str, labels: np.ndarray, centers_rgb: np.ndarray) -> None:
    h, w = labels.shape
    out = np.full((h, w, 3), 255, dtype=np.uint8)
    for i in range(centers_rgb.shape[0]):
        rgb = np.clip(np.rint(centers_rgb[i]), 0, 255).astype(np.uint8)
        out[labels == (i + 1)] = rgb
    Image.fromarray(out, mode="RGB").save(path)


def _write_meta_json(path: str, labels: np.ndarray, centers_rgb: np.ndarray, k: int) -> None:
    counts = {str(i): int((labels == i).sum()) for i in range(0, k + 1)}
    centers = {str(i + 1): [float(x) for x in centers_rgb[i]] for i in range(k)}
    payload = {"k": int(k), "counts": counts, "centers_rgb": centers, "nodata": 0}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _remove_small_regions(labels: np.ndarray, k: int, min_pixels: int) -> np.ndarray:
    """移除每个类别里小于 min_pixels 的连通区域（置为 0）。labels 期望为 uint8，值域 0..k。"""
    if min_pixels <= 0:
        return labels
    if ndi is None:  # pragma: no cover
        raise SystemExit("缺少依赖 scipy：无法执行 --min-pixels 碎斑清理。")
    out = labels.copy()
    structure = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8)  # 4 邻域
    for cls in range(1, k + 1):
        mask = out == cls
        if not mask.any():
            continue
        cc, n = ndi.label(mask, structure=structure)
        if n == 0:
            continue
        sizes = np.bincount(cc.reshape(-1))
        # sizes[0] 是背景
        small_ids = np.where(sizes < min_pixels)[0]
        small_ids = small_ids[small_ids != 0]
        if small_ids.size:
            out[np.isin(cc, small_ids)] = 0
    return out


def _merge_small_regions(labels: np.ndarray, k: int, min_pixels: int) -> np.ndarray:
    """把小于 min_pixels 的连通小斑点并入临近面体（用最近的非小斑点类别回填）。

    实现要点：
    - 先找出“每个类别内部”的小连通域，做成 small_mask；
    - 将 small_mask 位置临时置 0；
    - 用距离变换把 small_mask 像元映射到最近的“有效类别像元”，从而实现合并。
    """
    if min_pixels <= 0:
        return labels
    if ndi is None:  # pragma: no cover
        raise SystemExit("缺少依赖 scipy：无法执行 --min-pixels 小斑点合并。")

    labels_u8 = labels.astype(np.uint8, copy=False)
    structure = np.ones((3, 3), dtype=np.uint8)  # 8 邻域

    small_mask = np.zeros(labels_u8.shape, dtype=bool)
    for cls in range(1, k + 1):
        mask = labels_u8 == cls
        if not mask.any():
            continue
        cc, n = ndi.label(mask, structure=structure)
        if n == 0:
            continue
        sizes = np.bincount(cc.reshape(-1))
        small_ids = np.where(sizes < min_pixels)[0]
        small_ids = small_ids[small_ids != 0]
        if small_ids.size:
            small_mask |= np.isin(cc, small_ids)

    if not small_mask.any():
        return labels_u8

    keep = labels_u8.copy()
    keep[small_mask] = 0
    valid = keep > 0
    if not valid.any():
        return labels_u8

    # 仅合并“确实贴着某个非小斑点区域”的碎斑；如果碎斑四周都是背景(0)，就直接丢弃为 0，
    # 避免把海洋/空白处的小噪声拉到地图内部某个远处类别，从而产生大量离散小面。
    kernel = np.ones((3, 3), dtype=np.uint8)
    kernel[1, 1] = 0
    neighbor_valid = ndi.convolve(valid.astype(np.uint8), kernel, mode="constant", cval=0) > 0
    merge_mask = small_mask & neighbor_valid

    # distance_transform_edt 返回“到最近 0 的距离”；因此把 ~valid 作为 input，
    # 这样 0 就是 valid 像元，indices 就会指向最近的 valid 像元。
    _, (iy, ix) = ndi.distance_transform_edt(~valid, return_indices=True)
    nearest = keep[iy, ix]

    out = labels_u8.copy()
    out[merge_mask] = nearest[merge_mask]
    out[small_mask & (~merge_mask)] = 0
    return out


def _polygonize_labels(
    labels: np.ndarray,
    transform,
    crs,
) -> "gpd.GeoDataFrame":
    """把标签栅格 polygonize 成面要素。labels 期望为 uint8，值域 0..K（0为背景）。"""
    if raster_shapes is None or shp_shape is None or gpd is None:  # pragma: no cover
        raise SystemExit("缺少依赖（rasterio/shapely/geopandas）：无法输出矢量。")
    feats = []
    mask = labels > 0
    for geom, val in raster_shapes(labels, mask=mask, transform=transform):
        if not val:
            continue
        feats.append({"geometry": shp_shape(geom), "class_id": int(val)})
    gdf = gpd.GeoDataFrame(feats, geometry="geometry", crs=crs)
    # 修复少量无效几何（自交等）
    if not gdf.empty:
        if hasattr(gdf.geometry, "make_valid"):
            gdf["geometry"] = gdf.geometry.make_valid()
        else:
            gdf["geometry"] = gdf.geometry.buffer(0)
        # 兜底：对仍无效的再 make_valid 一次（shapely>=2）
        if shapely is not None:
            try:
                bad = ~gdf.geometry.is_valid
                if bool(bad.any()):
                    gdf.loc[bad, "geometry"] = shapely.make_valid(gdf.loc[bad, "geometry"])
            except Exception:
                pass
        gdf = gdf[~gdf.geometry.is_empty]
    return gdf


def _write_gpkg(gdf: gpd.GeoDataFrame, path: str, layer: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    # 本环境 fiona 缺失，强制走 pyogrio
    gdf.to_file(path, layer=layer, driver="GPKG", engine="pyogrio")


def _write_shapefile(gdf: gpd.GeoDataFrame, shp_path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(shp_path)) or ".", exist_ok=True)
    gdf.to_file(shp_path, driver="ESRI Shapefile", engine="pyogrio")


def _write_geojson(gdf: gpd.GeoDataFrame, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    gdf.to_file(path, driver="GeoJSON", engine="pyogrio")


def render_preview(labels: np.ndarray, centers_rgb: np.ndarray) -> np.ndarray:
    """把标签图渲染成 RGB 预览图（0=白底，1..K=对应中心色）。返回 uint8(H,W,3)。"""
    h, w = labels.shape
    out = np.full((h, w, 3), 255, dtype=np.uint8)
    k = int(centers_rgb.shape[0])
    for i in range(k):
        rgb = np.clip(np.rint(centers_rgb[i]), 0, 255).astype(np.uint8)
        out[labels == (i + 1)] = rgb
    return out


def classify_image(
    in_image: str,
    centers_rgb: np.ndarray,
    *,
    tol: float = 22.0,
    white_thresh: int = 245,
    fill_unknown: bool = True,
    chunk: int = 800000,
) -> np.ndarray:
    """按给定 centers_rgb 对图片做 K 类分类，返回 labels(uint8) 值域 0..K（0=背景/未知）。"""
    rgb = _read_rgb_image(in_image)
    background, artifacts = _make_masks(rgb, white_thresh=int(white_thresh))

    labels = _classify_by_lab_distance(
        rgb=rgb,
        background=background,
        artifacts=artifacts,
        centers_rgb=centers_rgb.astype(np.float32),
        tol=float(tol),
        fill_unknown=bool(fill_unknown),
        chunk=int(chunk),
    )

    k = int(centers_rgb.shape[0])
    labels0 = labels.copy()
    labels0[(labels0 < 0) | (labels0 > k)] = 0
    return labels0.astype(np.uint8, copy=False)


def vectorize_labels(
    labels0: np.ndarray,
    *,
    ref_raster: str = "",
    crs: str | None = None,
    min_pixels: int = 0,
    small_action: str = "drop",
    simplify: float = 0.0,
    dissolve: bool = False,
) -> "gpd.GeoDataFrame":
    """把 labels0(0..K) 矢量化为面要素 GeoDataFrame（字段 class_id）。"""
    if gpd is None or rasterio is None or from_origin is None:  # pragma: no cover
        raise SystemExit("缺少依赖（geopandas/rasterio）：无法矢量化。")

    labels_u8 = labels0.astype(np.uint8, copy=False)
    k = int(labels_u8.max())
    min_pixels = int(min_pixels)
    if min_pixels > 0 and str(small_action).lower() == "merge":
        labels_vec = _merge_small_regions(labels_u8, k=k, min_pixels=min_pixels)
    else:
        labels_vec = _remove_small_regions(labels_u8, k=k, min_pixels=min_pixels)

    out_crs = crs or None
    if ref_raster:
        with rasterio.open(ref_raster) as ds:
            if (ds.width, ds.height) != (labels_u8.shape[1], labels_u8.shape[0]):
                raise SystemExit(f"ref-raster 尺寸不匹配：ref={ds.width}x{ds.height}，labels={labels_u8.shape[1]}x{labels_u8.shape[0]}。")
            transform = ds.transform
            out_crs = out_crs or ds.crs
    else:
        transform = from_origin(0.0, float(labels_u8.shape[0]), 1.0, 1.0)

    gdf = _polygonize_labels(labels_vec, transform=transform, crs=out_crs)
    if simplify and not gdf.empty:
        gdf["geometry"] = gdf.geometry.simplify(float(simplify), preserve_topology=True)
    if dissolve and not gdf.empty:
        gdf = gdf.dissolve(by="class_id", as_index=False)
    return gdf


def export_vectors(
    gdf: "gpd.GeoDataFrame",
    *,
    out_shp: str = "",
    out_gpkg: str = "",
    layer: str = "zones",
    out_geojson: str = "",
) -> None:
    """按用户选择导出 shp/gpkg/geojson（路径为空则跳过）。"""
    if out_gpkg:
        _write_gpkg(gdf, out_gpkg, layer=str(layer))
    if out_shp:
        _write_shapefile(gdf, out_shp)
    if out_geojson:
        _write_geojson(gdf, out_geojson)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="按颜色中心把图片分成 K 类（默认 8 类）")
    p.add_argument("--in", dest="in_image", required=True, help="输入图片（png/jpg 等）")
    p.add_argument("--out-label", dest="out_label", required=True, help="输出标签 PNG（像元值 0..K）")
    p.add_argument("--out-preview", dest="out_preview", default="", help="输出预览 PNG（每类用中心色渲染）")
    p.add_argument("--out-meta", dest="out_meta", default="", help="输出元数据 JSON（像元统计与中心色）")
    p.add_argument("--out-gpkg", dest="out_gpkg", default="", help="输出矢量面要素（GeoPackage .gpkg）")
    p.add_argument("--out-shp", dest="out_shp", default="", help="输出矢量面要素（ESRI Shapefile .shp，ArcGIS 最兼容）")
    p.add_argument("--out-geojson", dest="out_geojson", default="", help="输出矢量面要素（GeoJSON .geojson）")
    p.add_argument("--layer", dest="layer", default="zones", help="输出 GPKG 图层名（默认 zones）")
    p.add_argument("--ref-raster", dest="ref_raster", default="", help="参考栅格（优先用其 transform/CRS；需与输入图片同宽高）")
    p.add_argument("--crs", dest="crs", default="", help="输出矢量的 CRS（如 EPSG:4326；不填则不设置）")
    p.add_argument("--min-pixels", dest="min_pixels", type=int, default=0, help="矢量化前移除小碎斑（像元数阈值）")
    p.add_argument(
        "--small-action",
        dest="small_action",
        choices=["drop", "merge"],
        default="drop",
        help="小碎斑处理方式：drop=置为0(默认)；merge=并入相邻面",
    )
    p.add_argument("--simplify", dest="simplify", type=float, default=0.0, help="矢量化后简化容差（坐标单位；0 表示不简化）")
    p.add_argument("--dissolve", dest="dissolve", action="store_true", help="按 class_id 融合为每类一个（可能较慢）")

    p.add_argument("--k", dest="k", type=int, default=8, help="类别数（默认 8）")
    p.add_argument("--tol", dest="tol", type=float, default=22.0, help="颜色阈值（Lab 距离，越大越宽容）")
    p.add_argument("--white-thresh", dest="white_thresh", type=int, default=245, help="背景阈值（>=该值认为近白背景）")
    p.add_argument("--fill-unknown", dest="fill_unknown", action="store_true", help="把未知/线条/文字像元用最近类别回填")
    p.add_argument("--chunk", dest="chunk", type=int, default=800000, help="分块像元数（防止内存过高）")

    p.add_argument("--centers-json", dest="centers_json", default="centers_rgb_clean.json", help="中心色 JSON（默认使用本目录已有文件）")
    p.add_argument("--export-centers-json", dest="export_centers_json", default="", help="导出自动估计的中心色 JSON")
    p.add_argument("--sample", dest="sample", type=int, default=200000, help="自动估计中心色时的抽样像元数")
    p.add_argument("--iters", dest="iters", type=int, default=20, help="自动估计中心色的 KMeans 迭代次数")
    p.add_argument("--seed", dest="seed", type=int, default=7, help="随机种子")

    args = p.parse_args(argv)

    rgb = _read_rgb_image(args.in_image)
    background, artifacts = _make_masks(rgb, white_thresh=int(args.white_thresh))

    if args.centers_json and os.path.exists(args.centers_json):
        centers_rgb = _load_centers(args.centers_json)
        if centers_rgb.shape[0] != int(args.k):
            raise SystemExit(f"centers-json 里是 {centers_rgb.shape[0]} 类，但你指定 --k {args.k}")
    else:
        train = rgb[(~background) & (~artifacts)].reshape(-1, 3).astype(np.float32)
        if train.shape[0] == 0:
            raise SystemExit("可训练像元为 0：请检查 white-thresh 是否过低导致全被当背景。")
        rng = np.random.default_rng(int(args.seed))
        if train.shape[0] > int(args.sample):
            sel = rng.choice(train.shape[0], size=int(args.sample), replace=False)
            sample = train[sel]
        else:
            sample = train
        centers_rgb = _kmeans_fit(sample, k=int(args.k), iters=int(args.iters), seed=int(args.seed))
        if args.export_centers_json:
            _save_centers(args.export_centers_json, centers_rgb)

    labels = _classify_by_lab_distance(
        rgb=rgb,
        background=background,
        artifacts=artifacts,
        centers_rgb=centers_rgb,
        tol=float(args.tol),
        fill_unknown=bool(args.fill_unknown),
        chunk=int(args.chunk),
    )

    os.makedirs(os.path.dirname(os.path.abspath(args.out_label)) or ".", exist_ok=True)
    labels0 = labels.copy()
    labels0[(labels0 < 0) | (labels0 > int(args.k))] = 0
    _write_label_png(args.out_label, labels0, k=int(args.k))

    if args.out_preview:
        _write_preview_png(args.out_preview, labels, centers_rgb)

    if args.out_meta:
        _write_meta_json(args.out_meta, labels0, centers_rgb, k=int(args.k))

    if args.out_gpkg or args.out_shp or args.out_geojson:
        if gpd is None or rasterio is None or from_origin is None:  # pragma: no cover
            raise SystemExit("缺少依赖（geopandas/rasterio）：无法输出 --out-gpkg。")
        k = int(args.k)
        min_pixels = int(args.min_pixels)
        labels_u8 = labels0.astype(np.uint8)
        if min_pixels > 0 and str(args.small_action).lower() == "merge":
            labels_vec = _merge_small_regions(labels_u8, k=k, min_pixels=min_pixels)
        else:
            labels_vec = _remove_small_regions(labels_u8, k=k, min_pixels=min_pixels)

        crs = args.crs or None
        if args.ref_raster:
            with rasterio.open(args.ref_raster) as ds:
                if (ds.width, ds.height) != (rgb.shape[1], rgb.shape[0]):
                    raise SystemExit(
                        f"ref-raster 尺寸不匹配：ref={ds.width}x{ds.height}，image={rgb.shape[1]}x{rgb.shape[0]}。"
                    )
                transform = ds.transform
                # 若用户显式给 crs，则优先；否则沿用 ref 的 crs（可能为 None）
                crs = crs or ds.crs
        else:
            # 用像素坐标：左上 (0, H)，像元大小=1
            transform = from_origin(0.0, float(rgb.shape[0]), 1.0, 1.0)

        gdf = _polygonize_labels(labels_vec, transform=transform, crs=crs)
        if args.simplify and not gdf.empty:
            gdf["geometry"] = gdf.geometry.simplify(float(args.simplify), preserve_topology=True)
        if args.dissolve and not gdf.empty:
            gdf = gdf.dissolve(by="class_id", as_index=False)

        if args.out_gpkg:
            _write_gpkg(gdf, args.out_gpkg, layer=str(args.layer))
        if args.out_shp:
            _write_shapefile(gdf, args.out_shp)
        if args.out_geojson:
            _write_geojson(gdf, args.out_geojson)

    # 控制台输出中心色，方便你手动对齐图例
    centers_out: Dict[str, list[float]] = {str(i + 1): [float(x) for x in centers_rgb[i]] for i in range(int(args.k))}
    print(json.dumps(centers_out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
