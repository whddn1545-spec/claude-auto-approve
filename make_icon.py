#!/usr/bin/env python3
"""Claude Auto-Approve 앱 아이콘 생성 (Pillow만 사용)"""

import os
import sys
from PIL import Image, ImageDraw, ImageFilter

def draw_icon(size: int) -> Image.Image:
    s = size
    img = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # ── 배경: 그라디언트 (위=진한 네이비, 아래=약간 밝은 남색) ──
    for y in range(s):
        t = y / s
        r = int(8  + t * 6)
        g = int(8  + t * 8)
        b = int(24 + t * 20)
        draw.line([(0, y), (s - 1, y)], fill=(r, g, b, 255))

    # ── 둥근 사각형 마스크 ──
    radius = s // 5
    mask = Image.new('L', (s, s), 0)
    m_draw = ImageDraw.Draw(mask)
    m_draw.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=255)
    img.putalpha(mask)

    # ── 외곽선 ──
    border = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    b_draw = ImageDraw.Draw(border)
    lw = max(1, s // 60)
    b_draw.rounded_rectangle([lw//2, lw//2, s - lw//2 - 1, s - lw//2 - 1],
                              radius=radius, outline=(0, 200, 255, 120), width=lw)
    border.putalpha(mask)
    img = Image.alpha_composite(img, border)
    draw = ImageDraw.Draw(img)

    # ── 번개 볼트 ──
    # 중심 기준 좌표 (0~1 정규화)
    cx, cy = s * 0.5, s * 0.48
    bolt = [
        (0.60, 0.08),  # 위 오른쪽
        (0.38, 0.50),  # 가운데 왼쪽
        (0.53, 0.50),  # 가운데 오른쪽
        (0.28, 0.92),  # 아래 왼쪽
        (0.62, 0.46),  # 가운데 오른쪽 아래
        (0.47, 0.46),  # 가운데 왼쪽 아래
    ]
    pts = [(int(x * s), int(y * s)) for x, y in bolt]

    # 그림자
    shadow = Image.new('RGBA', (s, s), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    off = max(1, s // 40)
    shadow_pts = [(x + off, y + off) for x, y in pts]
    sd.polygon(shadow_pts, fill=(0, 180, 230, 60))
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=max(1, s // 30)))
    img = Image.alpha_composite(img, shadow)
    draw = ImageDraw.Draw(img)

    # 메인 볼트 (그라디언트 효과: 두 색 레이어)
    draw.polygon(pts, fill=(0, 200, 255, 255))
    # 하이라이트 (상단 밝게)
    hi_pts = [(int(x * s), int(y * s)) for x, y in [
        (0.60, 0.08), (0.38, 0.50), (0.44, 0.50), (0.53, 0.08)
    ]]
    draw.polygon(hi_pts, fill=(180, 240, 255, 120))

    return img


def create_iconset(out_dir: str):
    """out_dir에 icon.iconset 폴더 생성"""
    iconset = os.path.join(out_dir, 'AppIcon.iconset')
    os.makedirs(iconset, exist_ok=True)

    specs = [
        ('icon_16x16.png',      16),
        ('icon_16x16@2x.png',   32),
        ('icon_32x32.png',      32),
        ('icon_32x32@2x.png',   64),
        ('icon_128x128.png',   128),
        ('icon_128x128@2x.png',256),
        ('icon_256x256.png',   256),
        ('icon_256x256@2x.png',512),
        ('icon_512x512.png',   512),
        ('icon_512x512@2x.png',1024),
    ]

    for fname, size in specs:
        img = draw_icon(size)
        img.save(os.path.join(iconset, fname), 'PNG')
        print(f'  {fname}')

    return iconset


def make_icns(iconset_path: str, out_path: str) -> bool:
    """iconutil로 .icns 생성"""
    import subprocess
    r = subprocess.run(['iconutil', '-c', 'icns', iconset_path, '-o', out_path],
                       capture_output=True)
    return r.returncode == 0


if __name__ == '__main__':
    out_dir = sys.argv[1] if len(sys.argv) > 1 else '.'
    print('아이콘 생성 중...')
    iconset = create_iconset(out_dir)
    icns = os.path.join(out_dir, 'AppIcon.icns')
    if make_icns(iconset, icns):
        import shutil
        shutil.rmtree(iconset)
        print(f'✅ {icns}')
    else:
        # iconutil 실패 시 PNG 폴백
        img = draw_icon(512)
        img.save(os.path.join(out_dir, 'AppIcon.png'))
        print(f'⚠ iconutil 실패 → PNG 저장')
