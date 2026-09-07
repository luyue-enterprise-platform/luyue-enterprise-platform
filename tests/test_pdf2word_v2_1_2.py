# -*- coding: utf-8 -*-
"""v2.1.2 图片横竖版"主体内容判定"回归测试

修复背景：图片转 PDF 此前按"像素宽高比"判定方向。手机拍照的竖版证件常因
EXIF 方向标记以横版像素存储，被误判为横版——直观表现为"以图片右下角水印/
印章位置当了方向基准"，竖版内容被错误转成横版。

修复：改为依据图片实际主体内容判定（content_orient）——
1. EXIF 归一化（消除拍照旋转标记根源）；
2. OCR 主体文字区域外接矩形宽高比（对右下角水印鲁棒）；
3. 无文字时按像素宽高兜底（向后兼容）。
"""
import os
import shutil
import tempfile
import unittest
from unittest import mock

import fitz

from modules.pdf2word.core import content_orient, to_pdf, validator


def _font(sz):
    from PIL import ImageFont
    for name in ('arial.ttf', 'msyh.ttc', 'simhei.ttf'):
        try:
            return ImageFont.truetype(name, sz)
        except Exception:
            continue
    return None


class TestContentOrientationLogic(unittest.TestCase):
    """纯逻辑：文字框外接矩形 -> 方向（mock 掉 OCR，快速确定性）"""

    def _orient(self, boxes):
        with mock.patch.object(content_orient, '_ocr_boxes', return_value=boxes):
            return content_orient.content_orientation(None)

    def test_portrait_body_with_bottom_right_watermark(self):
        # 竖版主体文字区 + 右下角小水印 → 仍判竖版（水印不主导整体宽高比）
        boxes = [
            (40, 60, 560, 100), (40, 180, 560, 220), (40, 300, 560, 340),
            (40, 420, 560, 460), (40, 540, 560, 580), (40, 660, 560, 700),
            (430, 950, 520, 980),  # 右下角水印
        ]
        self.assertEqual(self._orient(boxes), 'portrait')

    def test_landscape_body(self):
        # 横版主体文字区（宽 > 高）→ 横版
        boxes = [
            (40, 60, 1100, 110), (40, 240, 1100, 290), (40, 420, 1100, 470),
        ]
        self.assertEqual(self._orient(boxes), 'landscape')

    def test_no_text_returns_none(self):
        # 无文字框 → None（调用方按像素兜底）
        self.assertIsNone(self._orient([]))

    def test_degenerate_box_returns_none(self):
        self.assertIsNone(self._orient([(10, 10, 10, 10)]))


class TestDecideOrientationFallback(unittest.TestCase):
    """EXIF 归一化 + 无文字时像素兜底（mock OCR 为空，不依赖引擎）"""

    def _decide(self, img):
        with mock.patch.object(content_orient, '_ocr_boxes', return_value=[]):
            return content_orient.decide_orientation(img)

    def test_pixel_fallback_landscape(self):
        from PIL import Image
        _n, orient, basis = self._decide(Image.new('RGB', (1600, 900), 'white'))
        self.assertEqual((orient, basis), ('landscape', 'pixel'))

    def test_pixel_fallback_portrait(self):
        from PIL import Image
        _n, orient, basis = self._decide(Image.new('RGB', (600, 1200), 'white'))
        self.assertEqual((orient, basis), ('portrait', 'pixel'))

    def test_exif_orientation_normalized_to_portrait(self):
        # 横版像素 + EXIF 方向=6（应竖版显示）→ 转正后按竖版
        from PIL import Image
        img = Image.new('RGB', (1600, 900), 'white')
        ex = img.getexif()
        ex[0x0112] = 6
        norm, orient, basis = self._decide(img)
        self.assertEqual(norm.size, (900, 1600))  # 像素已转正为竖版
        self.assertEqual(orient, 'portrait')       # 无文字 → 按转正后像素判竖版


class TestImageToPdfContentOrientation(unittest.TestCase):
    """端到端：真实 OCR + 转换（OCR/字体不可用时自动跳过）"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        if _font(40) is None:
            self.skipTest('无可用 truetype 字体')
        try:
            from modules.insurance.core.ocr_engine import get_engine
            get_engine()
        except Exception as e:
            self.skipTest('OCR 引擎不可用: %s' % e)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make(self, path, size, lines, step, font_size=40, watermark=None):
        from PIL import Image, ImageDraw
        img = Image.new('RGB', size, 'white')
        d = ImageDraw.Draw(img)
        f = _font(font_size)
        x0 = size[0] // 2 - 80 if size[0] > size[1] else 40
        for i, ln in enumerate(lines):
            d.text((x0, 40 + i * step), ln, fill='black', font=f)
        if watermark:
            d.text((size[0] - 140, size[1] - 60), watermark,
                   fill='gray', font=_font(20))
        img.save(path)
        return path

    def test_portrait_cert_with_watermark_converts_to_portrait(self):
        """用户反馈的 bug：竖版证件 + 右下角水印 → 必须输出竖版"""
        src = self._make(os.path.join(self.tmp, 'cert.png'), (700, 1100),
                         ['Certificate Line %d' % i for i in range(9)],
                         step=110, watermark='WM')
        dst = os.path.join(self.tmp, 'cert.pdf')
        pages, orient = to_pdf.convert_image_to_pdf(src, dst)
        self.assertEqual(pages, 1)
        self.assertEqual(orient, 'portrait')
        with fitz.open(dst) as doc:
            r = doc[0].rect
            self.assertLess(r.width, r.height)  # 竖版 A4 页
        rep = validator.validate_image_to_pdf([src], dst, 'cert.png', 'cert.pdf',
                                              expected_orients=[orient])
        self.assertEqual(rep['overall'], 'pass', rep['items'])

    def test_landscape_content_converts_to_landscape(self):
        """横版内容 → 横版（确认无回归）"""
        src = self._make(os.path.join(self.tmp, 'wide.png'), (1400, 800),
                         ['A wide landscape content line %d with more text' % i
                          for i in range(4)], step=170)
        dst = os.path.join(self.tmp, 'wide.pdf')
        _p, orient = to_pdf.convert_image_to_pdf(src, dst)
        self.assertEqual(orient, 'landscape')
        with fitz.open(dst) as doc:
            r = doc[0].rect
            self.assertGreater(r.width, r.height)

    def test_portrait_content_in_landscape_canvas_prefers_content(self):
        """横版画布 + 竖版主体内容（无 EXIF）→ 以内容为准判竖版"""
        from PIL import Image, ImageDraw
        src = os.path.join(self.tmp, 'embed.png')
        img = Image.new('RGB', (1400, 900), 'white')
        d = ImageDraw.Draw(img)
        f = _font(36)
        for i in range(10):
            d.text((620, 40 + i * 82), 'Line %d' % i, fill='black', font=f)
        img.save(src)
        dst = os.path.join(self.tmp, 'embed.pdf')
        _p, orient = to_pdf.convert_image_to_pdf(src, dst)
        self.assertEqual(orient, 'portrait')  # 内容竖版优先于画布横版


if __name__ == '__main__':
    unittest.main()
