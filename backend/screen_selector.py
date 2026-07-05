"""
Screen Region Selector
Launches a fullscreen transparent tkinter overlay that lets the user
drag a rectangle to select a region of the screen.

Before showing the overlay, minimizes only the currently active window
(the 随心一阅 browser) so the user can see the exam page behind it.
Restores that window after selection.

Returns: (x, y, width, height) or None if cancelled.
"""

from __future__ import annotations

import tkinter as tk
import pyautogui
import pygetwindow as gw
import time

pyautogui.PAUSE = 0.3


class ScreenSelector:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.25)
        self.root.configure(bg="black")
        self.root.config(cursor="cross")

        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)

        self.start_x = 0
        self.start_y = 0
        self.rect_id: int | None = None
        self.result: tuple[int, int, int, int] | None = None

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Escape>", self._on_cancel)

    def _on_press(self, event: tk.Event) -> None:
        self.start_x = event.x
        self.start_y = event.y
        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#4F7EFF", width=2,
            fill="#4F7EFF", stipple="gray25",
        )

    def _on_drag(self, event: tk.Event) -> None:
        if self.rect_id:
            self.canvas.coords(
                self.rect_id,
                self.start_x, self.start_y,
                event.x, event.y,
            )

    def _on_release(self, event: tk.Event) -> None:
        x1, y1 = self.start_x, self.start_y
        x2, y2 = event.x, event.y

        x = min(x1, x2)
        y = min(y1, y2)
        w = abs(x2 - x1)
        h = abs(y2 - y1)

        if w > 5 and h > 5:
            self.result = (x, y, w, h)

        self.root.destroy()

    def _on_cancel(self, _event: tk.Event) -> None:
        self.result = None
        self.root.destroy()

    def select(self, region_type: str = "answer") -> tuple[int, int, int, int] | None:
        """Select a screen region. region_type is 'answer', 'score', or 'submit'."""
        return self.run()

    def run(self) -> tuple[int, int, int, int] | None:
        target_window = self._find_and_minimize()
        time.sleep(0.3)

        try:
            self.root.mainloop()
        finally:
            self._restore_window(target_window)

        return self.result

    @staticmethod
    def _find_and_minimize():
        """Find and minimize the 随心一阅 window the user just clicked in."""
        # First try: find window with "随心一阅" in title
        for w in gw.getAllWindows():
            title = w.title or ''
            if '随心一阅' in title and not w.isMinimized:
                try:
                    w.minimize()
                    return w
                except Exception as e:
                    print(f"[screen_selector] Failed to minimize window '{title}': {e}")

        # Second try: get the currently active window
        try:
            active = gw.getActiveWindow()
            if active and not active.isMinimized:
                if active.width > 300 and active.height > 200:
                    active.minimize()
                    return active
        except Exception as e:
            print(f"[screen_selector] Failed to minimize active window: {e}")

        return None

    @staticmethod
    def _restore_window(window):
        """Restore a previously minimized window."""
        if window is None:
            return
        time.sleep(0.2)
        try:
            window.restore()
            window.activate()
        except Exception as e:
            print(f"[screen_selector] Failed to restore window: {e}")


if __name__ == "__main__":
    selector = ScreenSelector()
    coords = selector.run()
    print(f"Selected region: {coords}")
