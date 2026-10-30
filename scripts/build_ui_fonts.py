"""Build tiny self-hosted Chinese UI subsets; build-only fontTools[woff].

Usage: PYTHONPATH=src /path/to/font-build/bin/python scripts/build_ui_fonts.py REGULAR_OTF MEDIUM_OTF
Regenerate after adding UI text. Full user/source text uses the system fallback.
Original font OFL license is distributed in static/noto-OFL.txt.
"""
from pathlib import Path
import sys
from fontTools import subset
from fontTools.ttLib import TTFont
from hadan.i18n import STRINGS

root=Path(__file__).resolve().parents[1]
text=''.join(STRINGS['zh-CN'].values())
# All fixed template text as well as translations; exclude Latin to preserve Geist.
text+=''.join(p.read_text() for p in (root/'src/hadan/templates').glob('*.html'))
text+=''.join(p.read_text() for p in (root/'scripts/preview-templates').glob('*.html'))
text+=(root/'scripts/preview.py').read_text()+(root/'scripts/preview-chat.js').read_text()
chars=''.join(sorted({c for c in text if ord(c)>255}))
for source,name,weight in zip(sys.argv[1:],('quant-cjk-regular.woff2','quant-cjk-medium.woff2'),(400,500),strict=True):
 font=TTFont(source)
 options=subset.Options();options.flavor='woff2';options.notdef_glyph=True;options.desubroutinize=True;options.hinting=False
 reducer=subset.Subsetter(options=options);reducer.populate(text=chars);reducer.subset(font)
 for record in font['name'].names:
  if record.nameID in (1,2,3,4,6):
   labels={1:'Quant UI CJK',2:'Regular' if weight==400 else 'Medium',3:f'QuantUICJK-{weight}',4:f'Quant UI CJK {weight}',6:f'QuantUICJK-{weight}'}
   record.string=labels[record.nameID].encode(record.getEncoding(),errors='replace')
 font.flavor='woff2';path=root/'src/hadan/static'/name;font.save(path)
 print(name,path.stat().st_size,len(chars),'glyphs')
