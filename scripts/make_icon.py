"""Draws the app icon (1024x1024 PNG). Run with the app's Python, which has AppKit.

    python scripts/make_icon.py <output.png>
"""

from __future__ import annotations

import sys

from AppKit import (
    NSBezierPath,
    NSBitmapImageRep,
    NSColor,
    NSGraphicsContext,
    NSMakeRect,
    NSPNGFileType,
)

SIZE = 1024
MARGIN = 100  # macOS icons leave room around the rounded square


def main(output: str) -> None:
    bitmap = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
        None, SIZE, SIZE, 8, 4, True, False, "NSCalibratedRGBColorSpace", 0, 0
    )
    NSGraphicsContext.setCurrentContext_(NSGraphicsContext.graphicsContextWithBitmapImageRep_(bitmap))

    body = NSMakeRect(MARGIN, MARGIN, SIZE - 2 * MARGIN, SIZE - 2 * MARGIN)
    NSColor.blackColor().setFill()
    NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(body, 185, 185).fill()

    # A screen with a prompt: the agent's own computer.
    NSColor.whiteColor().setStroke()
    screen = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
        NSMakeRect(250, 330, 524, 370), 44, 44
    )
    screen.setLineWidth_(40)
    screen.stroke()

    prompt = NSBezierPath.bezierPath()
    prompt.setLineWidth_(40)
    prompt.setLineCapStyle_(1)
    prompt.setLineJoinStyle_(1)
    prompt.moveToPoint_((350, 590))
    prompt.lineToPoint_((440, 515))
    prompt.lineToPoint_((350, 440))
    prompt.moveToPoint_((500, 440))
    prompt.lineToPoint_((640, 440))
    prompt.stroke()

    NSGraphicsContext.currentContext().flushGraphics()
    bitmap.representationUsingType_properties_(NSPNGFileType, {}).writeToFile_atomically_(output, True)


if __name__ == "__main__":
    main(sys.argv[1])
