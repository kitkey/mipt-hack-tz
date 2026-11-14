"""Render paired PDFs and deterministic synthetic degradation, CPU only."""
import json, sys
from pathlib import Path
import numpy as np
import pymupdf
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tzcheck.parse import parse
from benchmark_text import PAIRS
OUT=Path('eval/ocr_compare/generated')
def main():
    OUT.mkdir(parents=True,exist_ok=True)
    manifest=[]
    for name,path in PAIRS:
        ref=parse('samples/'+name+'.docx')
        (OUT/(name+'.reference.json')).write_text(json.dumps(ref.to_dict(),ensure_ascii=False,indent=2),encoding='utf-8')
        doc=pymupdf.open(path)
        for index,page in enumerate(doc):
            for dpi in (150,300):
                base=OUT/name/str(dpi); base.mkdir(parents=True,exist_ok=True)
                pix=page.get_pixmap(dpi=dpi, colorspace=pymupdf.csRGB,alpha=False)
                clean=base/f'{index+1:03d}_clean.png'; pix.save(str(clean))
                # Rotated raster, grey paper, independent Gaussian noise and synthetic marks.
                angle=[1,-2,3][index%3]
                p=page.get_pixmap(matrix=pymupdf.Matrix(dpi/72,dpi/72).prerotate(angle),colorspace=pymupdf.csRGB,alpha=False)
                a=np.frombuffer(p.samples,dtype=np.uint8).reshape(p.height,p.width,3).copy()
                rng=np.random.default_rng(12000+index+dpi)
                noise=rng.normal(0,4,(p.height,p.width,1))
                a=np.clip(a.astype(np.float32)*0.90+noise,0,255).astype(np.uint8)
                # Printed circular stamp + signature-like strokes; deliberately obscures text.
                h,w=a.shape[:2]; yy,xx=np.ogrid[:h,:w]; cx=int(w*.72); cy=int(h*.76); radius=max(12,int(w*.075))
                ring=np.abs((xx-cx)**2+(yy-cy)**2-radius**2)<radius*2
                a[ring]=[100,90,125]
                for k in range(-2,3):
                    x=np.arange(int(w*.48),int(w*.88)); y=(h*.82+np.sin(x/25)*h*.008+k).astype(int); a[y,x]=[60,55,75]
                out=pymupdf.Pixmap(pymupdf.csRGB,w,h,a.tobytes(),False)
                degraded=base/f'{index+1:03d}_degraded.jpg'; degraded.write_bytes(out.tobytes('jpg',jpg_quality=55))
                manifest.append({'name':name,'pdf':path,'page':index+1,'dpi':dpi,'clean':str(clean),'degraded':str(degraded),'rotation_deg':angle,'noise_sigma':4,'jpeg_quality':55})
        print(name,flush=True)
    (OUT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__': main()
