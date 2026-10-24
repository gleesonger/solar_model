"""Produce a symmetric, transparent solar icon as SVG.

All visible elements have the same nominal line thickness. Rays are generated
from the sun's centre at uniform radial start distance, with short/long lengths.
"""
from math import cos, sin, radians, hypot
from pathlib import Path

OUT = Path(__file__).resolve().parent
ORANGE = '#f59e0b'
CX, CY = 12.0, 10.0             # Sun raised by 1 SVG unit from the previous drawing
CIRCLE_RADIUS = 3.0             # Radius measured to ring centreline
STROKE = 1.75                 # Shared thickness for the box, ring, and rays
RAY_START = 5.0                # Radial distance from sun centre, for EVERY ray
SHORT_LENGTH = 2
LONG_LENGTH = 3.25
BOX_WIDTH = 18.0              # Outer width of the bottom box
BOX_HEIGHT = 5.0              # Outer height of the bottom box
BOX_GAP = -0.5                 # Gap below the downward ray

# SVG strokes straddle their rectangle, so inset its centreline by half a stroke.
box_x = CX - BOX_WIDTH / 2 + STROKE / 2
box_y = CY + RAY_START + SHORT_LENGTH + BOX_GAP + STROKE / 2
box_width = BOX_WIDTH - STROKE
box_height = BOX_HEIGHT - STROKE

parts = [
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">',
    '  <!-- Transparent background; bottom box uses the shared stroke thickness. -->',
    f'  <rect x="{box_x:g}" y="{box_y:g}" width="{box_width:g}" height="{box_height:g}" '
    f'fill="none" stroke="{ORANGE}" stroke-width="{STROKE:g}"/>',
    f'  <!-- Sun centre=({CX:g}, {CY:g}); ring stroke and ray strokes all {STROKE:g}. -->',
    f'  <circle cx="{CX:g}" cy="{CY:g}" r="{CIRCLE_RADIUS:g}" fill="none" stroke="{ORANGE}" stroke-width="{STROKE:g}"/>',
]

for angle in range(0, 360, 45):
    # Cardinal rays (0, 90, 180, 270 degrees) are short;
    # diagonal rays (45, 135, 225, 315 degrees) are long.
    length = SHORT_LENGTH if angle % 90 == 0 else LONG_LENGTH
    dx, dy = cos(radians(angle)), sin(radians(angle))
    x1, y1 = CX + dx * RAY_START, CY + dy * RAY_START
    x2, y2 = CX + dx * (RAY_START + length), CY + dy * (RAY_START + length)
    # Mathematical invariants (before SVG numerical formatting).
    assert abs(hypot(x1 - CX, y1 - CY) - RAY_START) < 1e-10
    assert abs(hypot(x2 - x1, y2 - y1) - length) < 1e-10
    parts.append(
        f'  <line x1="{x1:.8f}" y1="{y1:.8f}" '
        f'x2="{x2:.8f}" y2="{y2:.8f}" '
        f'stroke="{ORANGE}" stroke-width="{STROKE:g}" stroke-linecap="butt"/>'
    )
parts.append('</svg>')

svg = '\n'.join(parts) + '\n'
svg_path = OUT / 'solar_power.svg'
svg_path.write_text(svg)


print('done')
