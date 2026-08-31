import json, os, glob
cache = '.cache'
idx_dirs = sorted(glob.glob(os.path.join(cache, '*', 'index.json')))
print('indexes found:', len(idx_dirs))
for p in idx_dirs:
    try:
        j = json.load(open(p, encoding='utf-8'))
        print('%-16s pdf=%-40s pages=%s/%s model=%s created=%s' % (
            os.path.basename(os.path.dirname(p)), j.get('pdf_name','?')[:40],
            j.get('pages_indexed'), j.get('total_pages'), j.get('model'), j.get('created','')))
    except Exception as e:
        print('ERR', p, e)
