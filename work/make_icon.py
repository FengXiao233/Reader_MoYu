from PIL import Image, ImageDraw

im = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle((10, 10, 246, 246), radius=56, fill="#E7E6DC")
d.rounded_rectangle((29, 30, 227, 226), radius=42, fill="#344C50")
d.polygon([(55, 72), (119, 83), (119, 190), (55, 179)], fill="#F7F1DD")
d.polygon([(137, 83), (201, 72), (201, 179), (137, 190)], fill="#F7F1DD")
d.line([(128, 84), (128, 192)], fill="#D5B579", width=8)
d.arc((66, 89, 113, 151), 260, 80, fill="#C6A56A", width=5)
d.arc((144, 91, 193, 151), 100, 280, fill="#C6A56A", width=5)
d.polygon([(189, 38), (193, 49), (204, 53), (193, 57), (189, 68), (185, 57), (174, 53), (185, 49)], fill="#F5D692")
im.save("outputs/reader_icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
