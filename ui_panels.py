# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from PIL import Image, ImageTk
import tkinter as tk
from tkinter import ttk


Point = Tuple[int, int]


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


@dataclass
class ViewerEvent:
    canvas_x: float
    canvas_y: float
    image_x: int
    image_y: int


class PanZoomViewer(ttk.Frame):
    def __init__(
        self,
        master,
        *,
        title: str,
        badge: str = "",
        bg: str = "#ffffff",
        show_header: bool = True,
        placeholder: str = "",
    ) -> None:
        super().__init__(master)
        self._title = title
        self._badge = badge
        self._placeholder = placeholder or ""

        self._img: Optional[Image.Image] = None
        self._scale: float = 1.0
        self._imgtk: Optional[ImageTk.PhotoImage] = None

        self._on_move: Optional[Callable[[ViewerEvent], None]] = None
        self._on_click: Optional[Callable[[ViewerEvent], None]] = None

        if show_header:
            header = ttk.Frame(self)
            header.pack(fill=tk.X, padx=8, pady=(8, 4))
            ttk.Label(header, text=title).pack(side=tk.LEFT)
            if badge:
                ttk.Label(header, text=badge, foreground="#2f5aff").pack(side=tk.RIGHT)

        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=1, highlightbackground="#d0d7de")
        self.canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))

        # 交互：中键/右键拖拽平移、滚轮缩放、鼠标位置回调
        self.canvas.bind("<ButtonPress-2>", self._on_pan_start)
        self.canvas.bind("<B2-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonPress-3>", self._on_pan_start)
        self.canvas.bind("<B3-Motion>", self._on_pan_move)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._on_click_event)

    def set_on_move(self, fn: Optional[Callable[[ViewerEvent], None]]) -> None:
        self._on_move = fn

    def set_on_click(self, fn: Optional[Callable[[ViewerEvent], None]]) -> None:
        self._on_click = fn

    def set_cursor(self, cursor: str) -> None:
        self.canvas.configure(cursor=cursor or "")

    def set_scale(self, scale: float) -> None:
        self._scale = _clamp(float(scale), 0.1, 6.0)
        self.redraw()

    def set_image(self, img: Optional[Image.Image]) -> None:
        self._img = img
        self.redraw()

    def canvas_to_image(self, x: float, y: float) -> Optional[Point]:
        if self._img is None:
            return None
        cx = self.canvas.canvasx(x)
        cy = self.canvas.canvasy(y)
        ix = int(cx / self._scale)
        iy = int(cy / self._scale)
        if ix < 0 or iy < 0 or ix >= self._img.width or iy >= self._img.height:
            return None
        return (ix, iy)

    def _emit_event(self, x: float, y: float) -> Optional[ViewerEvent]:
        pt = self.canvas_to_image(x, y)
        if pt is None:
            return None
        cx = self.canvas.canvasx(x)
        cy = self.canvas.canvasy(y)
        return ViewerEvent(canvas_x=cx, canvas_y=cy, image_x=pt[0], image_y=pt[1])

    def _on_motion(self, event) -> None:
        if self._on_move is None:
            return
        ev = self._emit_event(event.x, event.y)
        if ev is not None:
            self._on_move(ev)

    def _on_click_event(self, event) -> None:
        if self._on_click is None:
            return
        ev = self._emit_event(event.x, event.y)
        if ev is not None:
            self._on_click(ev)

    def _on_pan_start(self, event) -> None:
        self.canvas.scan_mark(event.x, event.y)

    def _on_pan_move(self, event) -> None:
        self.canvas.scan_dragto(event.x, event.y, gain=1)

    def _on_wheel(self, event) -> None:
        # 以鼠标位置为中心缩放（近似）
        old = self._scale
        if event.delta > 0:
            new = old * 1.1
        else:
            new = old / 1.1
        self.set_scale(new)

    def redraw(self) -> None:
        self.canvas.delete("all")
        if self._img is None:
            self.canvas.configure(scrollregion=(0, 0, 1, 1))
            if self._placeholder:
                w = max(1, self.canvas.winfo_width())
                h = max(1, self.canvas.winfo_height())
                self.canvas.create_text(
                    w // 2,
                    h // 2,
                    text=self._placeholder,
                    fill="#667085",
                    font=("Segoe UI", 11),
                )
            return
        w = max(1, int(self._img.width * self._scale))
        h = max(1, int(self._img.height * self._scale))
        disp = self._img.resize((w, h), Image.Resampling.NEAREST)
        self._imgtk = ImageTk.PhotoImage(disp)
        self.canvas.create_image(0, 0, image=self._imgtk, anchor="nw")
        self.canvas.configure(scrollregion=(0, 0, w, h))


class CompositeViewer(PanZoomViewer):
    def __init__(self, master, *, title: str, badge: str = "", bg: str = "#ffffff") -> None:
        super().__init__(master, title=title, badge=badge, bg=bg)
        self._orig: Optional[Image.Image] = None
        self._prev: Optional[Image.Image] = None
        self._mode: str = "overlay"  # overlay | slider
        self._alpha: float = 0.55
        self._slider: float = 0.5

    def set_sources(self, orig: Optional[Image.Image], prev: Optional[Image.Image]) -> None:
        self._orig = orig
        self._prev = prev
        self.redraw()

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.redraw()

    def set_alpha(self, alpha: float) -> None:
        self._alpha = _clamp(float(alpha), 0.0, 1.0)
        self.redraw()

    def set_slider(self, slider: float) -> None:
        self._slider = _clamp(float(slider), 0.0, 1.0)
        self.redraw()

    def redraw(self) -> None:
        self.canvas.delete("all")
        if self._orig is None:
            self.canvas.configure(scrollregion=(0, 0, 1, 1))
            return

        # 没有预览就只显示原图
        if self._prev is None or self._prev.size != self._orig.size:
            # 直接走父类渲染，避免递归调用自身 redraw
            self._img = self._orig
            super().redraw()
            return

        w = max(1, int(self._orig.width * self._scale))
        h = max(1, int(self._orig.height * self._scale))
        o = self._orig.resize((w, h), Image.Resampling.NEAREST)
        p = self._prev.resize((w, h), Image.Resampling.NEAREST)

        if self._mode == "slider":
            cut = int(w * self._slider)
            comp = o.copy()
            comp.paste(p.crop((0, 0, cut, h)), (0, 0))
            # 分割线
            line_x = cut
        else:
            comp = Image.blend(o, p, self._alpha)
            line_x = -1

        self._imgtk = ImageTk.PhotoImage(comp)
        self.canvas.create_image(0, 0, image=self._imgtk, anchor="nw")
        if line_x >= 0:
            self.canvas.create_line(line_x, 0, line_x, h, fill="#2f5aff", width=2)
        self.canvas.configure(scrollregion=(0, 0, w, h))
