# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
from PIL import Image
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, colorchooser

sys.path.insert(0, os.path.dirname(__file__))
import classify_by_color as core  # noqa: E402
from ui_panels import PanZoomViewer, ViewerEvent  # noqa: E402


RGB = Tuple[int, int, int]


def _parse_color(text: str) -> RGB:
    s = (text or "").strip()
    if not s:
        raise ValueError("颜色为空")

    if s.startswith("#"):
        s = s[1:]

    # HEX: RRGGBB
    if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
        r = int(s[0:2], 16)
        g = int(s[2:4], 16)
        b = int(s[4:6], 16)
        return (r, g, b)

    # RGB: "r,g,b" or "r g b"
    parts = [p for p in s.replace(",", " ").split() if p]
    if len(parts) == 3:
        r, g, b = (int(float(x)) for x in parts)
        if not (0 <= r <= 255 and 0 <= g <= 255 and 0 <= b <= 255):
            raise ValueError("RGB 需在 0..255")
        return (r, g, b)

    raise ValueError("颜色格式不支持：请输入 HEX(RRGGBB) 或 RGB(如 179,30,34)")


def _rgb_to_hex(rgb: RGB) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


@dataclass
class PaletteRow:
    frame: tk.Frame
    entry: tk.Entry
    swatch: tk.Canvas
    rgb_label: tk.Label


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("按图例颜色数字化（预览 + 导出矢量）")
        self.geometry("1200x780")

        self.input_path = tk.StringVar(value="")
        self.output_dir = tk.StringVar(value=os.path.abspath(os.path.join(os.path.dirname(__file__), "outputs")))
        self.basename = tk.StringVar(value="zones")

        self.tol = tk.DoubleVar(value=12.0)
        self.white_thresh = tk.IntVar(value=245)
        self.fill_unknown = tk.BooleanVar(value=True)
        self.min_pixels = tk.IntVar(value=200)
        self.small_action = tk.StringVar(value="merge")
        self.simplify = tk.DoubleVar(value=0.0)
        self.dissolve = tk.BooleanVar(value=False)
        self.ref_raster = tk.StringVar(value="")
        self.crs = tk.StringVar(value="EPSG:4326")

        self.export_shp = tk.BooleanVar(value=True)
        self.export_gpkg = tk.BooleanVar(value=False)
        self.export_geojson = tk.BooleanVar(value=False)

        self.pick_mode = tk.BooleanVar(value=False)
        self.pick_behavior = tk.StringVar(value="auto")  # auto | replace | add
        self.last_picked = tk.StringVar(value="")

        self.zoom = tk.DoubleVar(value=1.0)

        self.palette_rows: List[PaletteRow] = []
        self._orig_img: Optional[Image.Image] = None
        self._prev_img: Optional[Image.Image] = None

        self._build_ui()
        self._add_palette_row("#b31e22")
        self._add_palette_row("#a0701c")
        self._add_palette_row("#a86e24")
        self._add_palette_row("#e62119")
        self._add_palette_row("#399f3c")
        self._add_palette_row("#f5a81c")
        self._add_palette_row("#f1ee7d")
        self._add_palette_row("#a5d170")

    def _build_ui(self) -> None:
        root = ttk.Frame(self)
        root.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left = ttk.Frame(root)
        left.pack(side=tk.LEFT, fill=tk.Y)

        right = ttk.Frame(root)
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        # 输入
        lf_in = ttk.LabelFrame(left, text="输入图片")
        lf_in.pack(fill=tk.X, pady=(0, 10))
        ttk.Entry(lf_in, textvariable=self.input_path, width=44).pack(side=tk.LEFT, padx=6, pady=6)
        ttk.Button(lf_in, text="选择...", command=self._choose_input).pack(side=tk.LEFT, padx=4, pady=6)
        ttk.Checkbutton(lf_in, text="取色笔", variable=self.pick_mode, command=self._refresh_cursor).pack(
            side=tk.LEFT, padx=4, pady=6
        )

        # 图例颜色
        lf_pal = ttk.LabelFrame(left, text="图例颜色（HEX 或 RGB）")
        lf_pal.pack(fill=tk.X, pady=(0, 10))
        pal_top = ttk.Frame(lf_pal)
        pal_top.pack(fill=tk.X, padx=6, pady=(6, 0))
        ttk.Label(pal_top, text="点击取色：").pack(side=tk.LEFT)
        ttk.Combobox(
            pal_top,
            values=["auto", "replace", "add"],
            textvariable=self.pick_behavior,
            state="readonly",
            width=8,
        ).pack(side=tk.LEFT, padx=6)
        ttk.Label(pal_top, textvariable=self.last_picked, foreground="#555").pack(side=tk.LEFT, padx=6)

        # 色板行数可能很多：这里单独加一个滚动条（不影响其它控件布局/取色笔功能）
        pal_mid = ttk.Frame(lf_pal)
        pal_mid.pack(fill=tk.X, padx=6, pady=6)

        pal_canvas = tk.Canvas(pal_mid, height=230, highlightthickness=0)
        pal_scroll = ttk.Scrollbar(pal_mid, orient="vertical", command=pal_canvas.yview)
        pal_canvas.configure(yscrollcommand=pal_scroll.set)
        pal_canvas.pack(side=tk.LEFT, fill=tk.X, expand=True)
        pal_scroll.pack(side=tk.RIGHT, fill=tk.Y)

        pal_inner = ttk.Frame(pal_canvas)
        pal_win = pal_canvas.create_window((0, 0), window=pal_inner, anchor="nw")

        def _pal_sync_scrollregion(_event=None) -> None:
            pal_canvas.configure(scrollregion=pal_canvas.bbox("all"))

        def _pal_sync_width(event) -> None:
            pal_canvas.itemconfigure(pal_win, width=event.width)

        pal_inner.bind("<Configure>", _pal_sync_scrollregion)
        pal_canvas.bind("<Configure>", _pal_sync_width)

        def _pal_wheel(event) -> None:
            delta = int(-1 * (event.delta / 120))
            pal_canvas.yview_scroll(delta, "units")

        def _pal_enter(_event=None) -> None:
            pal_canvas.bind_all("<MouseWheel>", _pal_wheel)

        def _pal_leave(_event=None) -> None:
            pal_canvas.unbind_all("<MouseWheel>")

        pal_canvas.bind("<Enter>", _pal_enter)
        pal_canvas.bind("<Leave>", _pal_leave)
        pal_inner.bind("<Enter>", _pal_enter)
        pal_inner.bind("<Leave>", _pal_leave)

        self.pal_container = pal_inner
        ttk.Button(lf_pal, text="添加一行", command=lambda: self._add_palette_row("")).pack(fill=tk.X, padx=6, pady=(0, 6))

        # 参数
        lf_params = ttk.LabelFrame(left, text="参数")
        lf_params.pack(fill=tk.X, pady=(0, 10))
        grid = ttk.Frame(lf_params)
        grid.pack(fill=tk.X, padx=6, pady=6)

        def add_row(r: int, label: str, widget: tk.Widget) -> None:
            ttk.Label(grid, text=label, width=14).grid(row=r, column=0, sticky="w", pady=2)
            widget.grid(row=r, column=1, sticky="ew", pady=2)

        grid.columnconfigure(1, weight=1)
        add_row(0, "tol", ttk.Spinbox(grid, from_=0, to=100, increment=0.5, textvariable=self.tol, width=10))
        add_row(1, "white_thresh", ttk.Spinbox(grid, from_=0, to=255, increment=1, textvariable=self.white_thresh, width=10))
        add_row(2, "fill_unknown", ttk.Checkbutton(grid, variable=self.fill_unknown))
        add_row(3, "min_pixels", ttk.Spinbox(grid, from_=0, to=100000, increment=10, textvariable=self.min_pixels, width=10))
        add_row(4, "small_action", ttk.Combobox(grid, values=["drop", "merge"], textvariable=self.small_action, state="readonly"))
        add_row(5, "simplify", ttk.Spinbox(grid, from_=0, to=50, increment=0.2, textvariable=self.simplify, width=10))
        add_row(6, "dissolve", ttk.Checkbutton(grid, variable=self.dissolve))

        # 输出
        lf_out = ttk.LabelFrame(left, text="输出")
        lf_out.pack(fill=tk.X, pady=(0, 10))
        out1 = ttk.Frame(lf_out)
        out1.pack(fill=tk.X, padx=6, pady=6)
        ttk.Entry(out1, textvariable=self.output_dir, width=44).pack(side=tk.LEFT)
        ttk.Button(out1, text="目录...", command=self._choose_output_dir).pack(side=tk.LEFT, padx=6)

        out2 = ttk.Frame(lf_out)
        out2.pack(fill=tk.X, padx=6, pady=(0, 6))
        ttk.Label(out2, text="basename").pack(side=tk.LEFT)
        ttk.Entry(out2, textvariable=self.basename, width=18).pack(side=tk.LEFT, padx=6)

        out3 = ttk.Frame(lf_out)
        out3.pack(fill=tk.X, padx=6, pady=(0, 6))
        ttk.Checkbutton(out3, text="SHP", variable=self.export_shp).pack(side=tk.LEFT)
        ttk.Checkbutton(out3, text="GPKG", variable=self.export_gpkg).pack(side=tk.LEFT, padx=8)
        ttk.Checkbutton(out3, text="GeoJSON", variable=self.export_geojson).pack(side=tk.LEFT)

        # 参考栅格 & CRS
        lf_ref = ttk.LabelFrame(left, text="参考栅格 / CRS（可选）")
        lf_ref.pack(fill=tk.X)
        r1 = ttk.Frame(lf_ref)
        r1.pack(fill=tk.X, padx=6, pady=6)
        ttk.Entry(r1, textvariable=self.ref_raster, width=44).pack(side=tk.LEFT)
        ttk.Button(r1, text="选择...", command=self._choose_ref_raster).pack(side=tk.LEFT, padx=6)
        r2 = ttk.Frame(lf_ref)
        r2.pack(fill=tk.X, padx=6, pady=(0, 6))
        ttk.Label(r2, text="CRS").pack(side=tk.LEFT)
        ttk.Entry(r2, textvariable=self.crs, width=18).pack(side=tk.LEFT, padx=6)

        # 操作按钮
        btns = ttk.Frame(left)
        btns.pack(fill=tk.X, pady=(10, 0))
        ttk.Button(btns, text="预览", command=self._run_preview).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(btns, text="导出", command=self._run_export).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

        self.status = tk.StringVar(value="就绪")
        ttk.Label(left, textvariable=self.status, wraplength=360, foreground="#333").pack(fill=tk.X, pady=(8, 0))

        self.preview_wrap = ttk.Frame(right)
        self.preview_wrap.pack(fill=tk.BOTH, expand=True)

        self.paned = ttk.Panedwindow(self.preview_wrap, orient=tk.HORIZONTAL)
        self.orig_view = PanZoomViewer(self.paned, title="ORIGINAL", show_header=False, placeholder="原图预览")
        self.prev_view = PanZoomViewer(self.paned, title="PREVIEW", show_header=False, placeholder="结果预览")
        self.paned.add(self.orig_view, weight=1)
        self.paned.add(self.prev_view, weight=1)
        self.paned.pack(fill=tk.BOTH, expand=True)

        # 事件：取色笔点击（仅原图）
        self.orig_view.set_on_click(self._on_pick_click)

        # 让滚轮缩放保持同步（覆盖 viewer 内置 wheel 逻辑）
        for v in (self.orig_view, self.prev_view):
            v.canvas.bind("<MouseWheel>", self._on_zoom_wheel)

    def _choose_input(self) -> None:
        path = filedialog.askopenfilename(title="选择输入图片", filetypes=[("Images", "*.png;*.jpg;*.jpeg;*.bmp;*.tif;*.tiff"), ("All", "*.*")])
        if path:
            self.input_path.set(path)
            self._load_original_preview(path)

    def _choose_ref_raster(self) -> None:
        path = filedialog.askopenfilename(title="选择参考栅格（可选）", filetypes=[("Raster", "*.tif;*.tiff;*.img;*.png;*.jpg;*.jpeg"), ("All", "*.*")])
        if path:
            self.ref_raster.set(path)

    def _choose_output_dir(self) -> None:
        path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_dir.set(path)

    def _load_original_preview(self, path: str) -> None:
        try:
            img = Image.open(path).convert("RGB")
            self._orig_img = img
            self.orig_view.set_image(img)
            self._apply_zoom()
        except Exception as e:
            messagebox.showerror("读取失败", str(e))

    def _refresh_cursor(self) -> None:
        if self.pick_mode.get():
            self.configure(cursor="crosshair")
            self.orig_view.set_cursor("crosshair")
        else:
            self.configure(cursor="")
            self.orig_view.set_cursor("")
            self.prev_view.set_cursor("")

    def _on_pick_click(self, ev: ViewerEvent) -> None:
        if not self.pick_mode.get():
            return
        if self._orig_img is None:
            return
        x, y = ev.image_x, ev.image_y
        rgb = self._orig_img.getpixel((x, y))
        if isinstance(rgb, int):
            rgb = (rgb, rgb, rgb)
        rgb = (int(rgb[0]), int(rgb[1]), int(rgb[2]))
        hx = _rgb_to_hex(rgb)
        self.last_picked.set(f"已取色：{hx}  ({rgb[0]},{rgb[1]},{rgb[2]})")

        behavior = self.pick_behavior.get()
        focus = self.focus_get()

        def is_palette_entry(w) -> Optional[PaletteRow]:
            for pr in self.palette_rows:
                if pr.entry is w:
                    return pr
            return None

        target_row = is_palette_entry(focus)
        if behavior == "add" or (behavior == "auto" and target_row is None):
            self._add_palette_row(hx)
            return
        if target_row is None and self.palette_rows:
            target_row = self.palette_rows[-1]
        if target_row is not None:
            target_row.entry.delete(0, tk.END)
            target_row.entry.insert(0, hx)
            self._refresh_palette_row(target_row.swatch, target_row.rgb_label, target_row.entry.get())

    def _apply_zoom(self) -> None:
        z = float(self.zoom.get())
        self.orig_view.set_scale(z)
        self.prev_view.set_scale(z)

    def _on_zoom_wheel(self, event) -> None:
        z = float(self.zoom.get())
        z = z * 1.1 if event.delta > 0 else z / 1.1
        self.zoom.set(max(0.25, min(3.0, z)))
        self._apply_zoom()

    def _add_palette_row(self, initial: str) -> None:
        row = ttk.Frame(self.pal_container)
        row.pack(fill=tk.X, pady=2)

        entry = ttk.Entry(row, width=18)
        entry.insert(0, initial)
        entry.pack(side=tk.LEFT)

        swatch = tk.Canvas(row, width=28, height=18, highlightthickness=1, highlightbackground="#888")
        swatch.pack(side=tk.LEFT, padx=6)

        rgb_label = ttk.Label(row, text="(?, ?, ?)", width=16)
        rgb_label.pack(side=tk.LEFT)

        def pick() -> None:
            c = colorchooser.askcolor(title="选择颜色")
            if c and c[0]:
                r, g, b = (int(c[0][0]), int(c[0][1]), int(c[0][2]))
                entry.delete(0, tk.END)
                entry.insert(0, _rgb_to_hex((r, g, b)))
                self._refresh_palette_row(swatch, rgb_label, entry.get())

        ttk.Button(row, text="取色", command=pick).pack(side=tk.LEFT, padx=6)

        def remove() -> None:
            row.destroy()
            self.palette_rows[:] = [pr for pr in self.palette_rows if pr.frame is not row]

        ttk.Button(row, text="删除", command=remove).pack(side=tk.RIGHT)

        pr = PaletteRow(frame=row, entry=entry, swatch=swatch, rgb_label=rgb_label)
        self.palette_rows.append(pr)
        entry.bind("<KeyRelease>", lambda _e: self._refresh_palette_row(swatch, rgb_label, entry.get()))
        self._refresh_palette_row(swatch, rgb_label, entry.get())

    def _refresh_palette_row(self, swatch: tk.Canvas, rgb_label: tk.Label, text: str) -> None:
        try:
            rgb = _parse_color(text)
            swatch.delete("all")
            swatch.create_rectangle(0, 0, 28, 18, fill=_rgb_to_hex(rgb), outline="")
            rgb_label.configure(text=f"({rgb[0]}, {rgb[1]}, {rgb[2]})")
        except Exception:
            swatch.delete("all")
            swatch.create_rectangle(0, 0, 28, 18, fill="#ffffff", outline="")
            rgb_label.configure(text="(?, ?, ?)")

    def _collect_centers(self) -> np.ndarray:
        colors: List[RGB] = []
        for pr in self.palette_rows:
            t = pr.entry.get().strip()
            if not t:
                continue
            colors.append(_parse_color(t))
        if len(colors) < 2:
            raise ValueError("至少需要 2 个图例颜色（建议 8 个）")
        return np.array(colors, dtype=np.float32)

    def _run_preview(self) -> None:
        path = self.input_path.get().strip()
        if not path:
            messagebox.showwarning("缺少输入", "请先选择输入图片。")
            return

        def work() -> None:
            try:
                self.status.set("预览计算中...")
                centers = self._collect_centers()
                labels0 = core.classify_image(
                    path,
                    centers,
                    tol=float(self.tol.get()),
                    white_thresh=int(self.white_thresh.get()),
                    fill_unknown=bool(self.fill_unknown.get()),
                )

                # 预览时也应用一次碎斑处理（更接近最终导出）
                gdf = None
                labels_for_preview = labels0
                if int(self.min_pixels.get()) > 0:
                    # 复用矢量化前同样的处理逻辑，但只为了渲染预览，不做矢量化
                    labels_for_preview = labels0.copy()
                    if self.small_action.get() == "merge":
                        labels_for_preview = core._merge_small_regions(labels_for_preview, k=int(labels_for_preview.max()), min_pixels=int(self.min_pixels.get()))
                    else:
                        labels_for_preview = core._remove_small_regions(labels_for_preview, k=int(labels_for_preview.max()), min_pixels=int(self.min_pixels.get()))

                prev = core.render_preview(labels_for_preview, centers)
                prev_img = Image.fromarray(prev, mode="RGB")

                def update_ui() -> None:
                    self._prev_img = prev_img
                    self.prev_view.set_image(prev_img)
                    self._apply_zoom()
                    self.status.set("预览完成")

                self.after(0, update_ui)
            except Exception as e:
                msg = f"{e}\n\n{traceback.format_exc()}"
                self.status.set("预览失败")
                self.after(0, lambda: messagebox.showerror("预览失败", msg))

        threading.Thread(target=work, daemon=True).start()

    def _run_export(self) -> None:
        path = self.input_path.get().strip()
        if not path:
            messagebox.showwarning("缺少输入", "请先选择输入图片。")
            return
        out_dir = self.output_dir.get().strip()
        if not out_dir:
            messagebox.showwarning("缺少输出", "请选择输出目录。")
            return
        base = (self.basename.get().strip() or "zones").strip()

        def work() -> None:
            try:
                os.makedirs(out_dir, exist_ok=True)
                self.status.set("导出中...")

                centers = self._collect_centers()
                labels0 = core.classify_image(
                    path,
                    centers,
                    tol=float(self.tol.get()),
                    white_thresh=int(self.white_thresh.get()),
                    fill_unknown=bool(self.fill_unknown.get()),
                )

                label_png = os.path.join(out_dir, f"{base}_labels.png")
                prev_png = os.path.join(out_dir, f"{base}_preview.png")
                meta_json = os.path.join(out_dir, f"{base}_meta.json")

                core._write_label_png(label_png, labels0, k=int(labels0.max()))
                Image.fromarray(core.render_preview(labels0, centers), mode="RGB").save(prev_png)
                core._write_meta_json(meta_json, labels0, centers, k=int(labels0.max()))

                shp_path = os.path.join(out_dir, f"{base}.shp") if self.export_shp.get() else ""
                gpkg_path = os.path.join(out_dir, f"{base}.gpkg") if self.export_gpkg.get() else ""
                geojson_path = os.path.join(out_dir, f"{base}.geojson") if self.export_geojson.get() else ""

                if shp_path or gpkg_path or geojson_path:
                    gdf = core.vectorize_labels(
                        labels0,
                        ref_raster=self.ref_raster.get().strip(),
                        crs=(self.crs.get().strip() or None),
                        min_pixels=int(self.min_pixels.get()),
                        small_action=self.small_action.get(),
                        simplify=float(self.simplify.get()),
                        dissolve=bool(self.dissolve.get()),
                    )
                    core.export_vectors(gdf, out_shp=shp_path, out_gpkg=gpkg_path, out_geojson=geojson_path, layer=base)

                # 导出一份图例中心色，便于复现
                centers_json = os.path.join(out_dir, f"{base}_centers_rgb.json")
                payload = {str(i + 1): [int(x) for x in centers[i].tolist()] for i in range(centers.shape[0])}
                with open(centers_json, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)

                self.status.set(f"导出完成：{out_dir}")
                self.after(0, lambda: messagebox.showinfo("完成", f"已导出到：\n{out_dir}"))
            except Exception as e:
                msg = f"{e}\n\n{traceback.format_exc()}"
                self.status.set("导出失败")
                self.after(0, lambda: messagebox.showerror("导出失败", msg))

        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    App().mainloop()
