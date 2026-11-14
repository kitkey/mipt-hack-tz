import os,time,json
os.environ['PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK']='True'
os.environ['TZCHECK_REFDATA_OFFLINE']='1'
from paddleocr import PPStructureV3
start=time.perf_counter()
ocr=PPStructureV3(lang='ru',device='cpu',enable_mkldnn=False,use_doc_orientation_classify=False,use_doc_unwarping=False,use_textline_orientation=False)
print('LOADED',round(time.perf_counter()-start,2),flush=True)
for result in ocr.predict('eval/ocr_compare/generated/lift_src/150/001_clean.png'):
 print('RESULT',result.markdown,flush=True)

