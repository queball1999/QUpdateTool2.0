"""
Small custom widgets shared by the setup wizard and the update window.

Skeleton is the pulsing placeholder from the syncro editor's form and list
skeletons; ToggleSwitch is a compact take on its animated toggle, painted
from the active theme so it follows light/dark and the accent colour.
"""

from __future__ import annotations

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QCheckBox, QSizePolicy, QWidget

from . import theme

PULSE_MS = 40
PULSE_PERIOD_MS = 1400


class Skeleton(QWidget):
    """
    Placeholder bars that pulse while something loads.

    bars is a list of (width fraction, height in px), drawn top to bottom.
    The pulse runs only while the widget is visible, so a hidden skeleton
    costs nothing.
    """

    def __init__(self, bars=((0.92, 12), (0.74, 12), (0.83, 12), (0.58, 12)), gap: int = 10, parent=None):
        super().__init__(parent)
        self.bars = list(bars)
        self.gap = gap
        self.phase = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(PULSE_MS)
        self.timer.timeout.connect(self.tick)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(self.sizeHint().height())

    def sizeHint(self) -> QSize:
        height = sum(h for _, h in self.bars) + self.gap * max(0, len(self.bars) - 1)
        return QSize(200, height + 2)

    def showEvent(self, event) -> None:
        self.timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self.timer.stop()
        super().hideEvent(event)

    def tick(self) -> None:
        self.phase = (self.phase + PULSE_MS / PULSE_PERIOD_MS) % 1.0
        self.update()

    def colour(self, step: int) -> QColor:
        base = QColor(self.palette().text().color())
        # Triangle wave 0.06 -> 0.16 alpha, offset per bar for a travelling shimmer.
        t = (self.phase + step * 0.08) % 1.0
        base.setAlphaF(0.06 + 0.10 * (1 - abs(2 * t - 1)))
        return base

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        y = 1.0
        for step, (fraction, height) in enumerate(self.bars):
            painter.setBrush(self.colour(step))
            painter.drawRoundedRect(QRectF(0, y, self.width() * fraction, height), 5, 5)
            y += height + self.gap
        painter.end()


class ToggleSwitch(QCheckBox):
    """
    An on/off switch with a sliding knob.

    Still a QCheckBox (isChecked, setChecked, toggled all work as before), but
    it paints no text: the card it sits in shows the label. The text passed
    in is kept as the accessible name for screen readers.
    """

    WIDTH = 42
    HEIGHT = 22

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setAccessibleName(text)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        self._offset = 0.0
        self.animation = QPropertyAnimation(self, b"offset", self)
        self.animation.setDuration(160)
        self.animation.setEasingCurve(QEasingCurve.InOutCubic)
        self.toggled.connect(self.on_toggled)

    def sizeHint(self) -> QSize:
        return QSize(self.WIDTH, self.HEIGHT)

    def hitButton(self, pos) -> bool:
        return self.rect().contains(pos)

    def setChecked(self, checked: bool) -> None:
        # Set programmatically (a page loading its values): jump, don't slide.
        super().setChecked(checked)
        self.animation.stop()
        self._offset = 1.0 if checked else 0.0
        self.update()

    def on_toggled(self, checked: bool) -> None:
        self.animation.stop()
        self.animation.setStartValue(self._offset)
        self.animation.setEndValue(1.0 if checked else 0.0)
        self.animation.start()

    def get_offset(self) -> float:
        return self._offset

    def set_offset(self, value: float) -> None:
        self._offset = value
        self.update()

    offset = Property(float, get_offset, set_offset)

    def paintEvent(self, _event) -> None:
        c = theme.active
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        track = QRectF(1, 1, self.width() - 2, self.height() - 2)
        radius = track.height() / 2
        knob = track.height() - 8
        x = track.left() + 4 + (track.width() - 8 - knob) * self._offset
        on = self.isChecked()

        if not self.isEnabled():
            painter.setOpacity(0.45)

        if on:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(c["accent"]))
        else:
            painter.setPen(QColor(c["switch_off"]))
            painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(track, radius, radius)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(c["on_accent"] if on else c["switch_off"]))
        painter.drawEllipse(QRectF(x, track.top() + 4, knob, knob))
        painter.end()
