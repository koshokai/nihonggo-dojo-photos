"""给「日本生活」文章找实拍照片：按 tools/reading/life_*.txt 里的 photo: 搜索词，在 Wikimedia Commons 上找
可以自由使用的照片（CC0、公有领域、CC BY、CC BY-SA），下载 960px 宽的版本到 photos/，作者和授权记在 photos/index.json。
网页上每张照片都会注明作者、授权和原图链接（CC BY / BY-SA 的要求）。

在 GitHub Actions 里运行（.github/workflows/photos.yml）：python3 tools/fetch_photos.py
已经有照片的文章会跳过；想换某张照片，就从 photos/index.json 删掉那一条（并删除图片）再运行。
"""
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get('PHOTO_OUT') or ROOT / 'photos')   # Actions 里可以换输出目录
API = 'https://commons.wikimedia.org/w/api.php'
INDEX = 'https://commons.wikimedia.org/w/index.php'
QUIET = bool(os.environ.get('PUBLIC_LOG'))   # 在公开仓库的 Actions 里跑时：日志只打 id，不打搜索词和照片地址（会员文章的话题不公开）
UA = 'NihongoDojo/1.0 (https://koshokai.github.io/nihonggo-dojo/; photo credits shown on site)'
STOP = {'japan', 'japanese', 'tokyo', 'with', 'from', 'interior', 'exterior', 'street', 'food'}
OK_LICENSE = re.compile(r'^(CC0|Public domain|PD|CC BY(-SA)? [0-9.]+)', re.I)


_last = {}   # 上一次请求各个主机的时间
_gap = {}    # 各个主机的请求间隔（被 429 后加长，之后慢慢缩短）


def get(url, params=None):
    # Wikimedia 请求太快会返回 429：同一个主机的请求之间至少隔 3 秒，
    # 429 时按 Retry-After 等待（没有的话指数退避），并把这个主机的间隔加长
    if params:
        url += '?' + urllib.parse.urlencode(params)
    host = urllib.parse.urlparse(url).netloc
    base = 3.0
    req = urllib.request.Request(url, headers={'User-Agent': UA, 'Accept': '*/*'})   # 不带 Accept 的请求更容易被当成机器人限流
    for i in range(12):
        wait = _last.get(host, 0) + _gap.get(host, base) - time.time()
        if wait > 0:
            time.sleep(wait)
        _last[host] = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
            _gap[host] = max(base, _gap.get(host, base) * 0.95)
            return data
        except urllib.error.HTTPError as e:
            if i == 11 or e.code not in (429, 500, 502, 503, 504):
                raise
            if e.code == 429:
                _gap[host] = min(_gap.get(host, base) * 1.5, 20)
            ra = e.headers.get('Retry-After', '')
            w = (int(ra) if ra.isdigit() else min(5 * 2 ** i, 120)) + 1 + i
            print(f'  {e.code} {host}, wait {w}s', flush=True)
            time.sleep(w)
        except Exception:
            if i >= 3:
                raise
            time.sleep(5 * (i + 1))


def plain(s):
    return html.unescape(re.sub(r'<[^>]+>', '', s or '')).strip()


def search(q, must=None):
    """搜索文件名，返回候选 [{title, page}]（还没查授权）。
    API 的搜索经常被限流（429），所以搜索用普通的搜索页（Special:Search），授权、作者再用 info() 一次查几十个。"""
    h = get(INDEX, {'search': q + ' filetype:bitmap', 'title': 'Special:Search', 'ns6': 1, 'fulltext': 1, 'limit': 40}).decode('utf-8', 'replace')
    pages = []
    for i, li in enumerate(h.split('<li class="mw-search-result')[1:]):
        t = re.search(r'mw-search-result-heading"><a href="[^"]*" title="([^"]+)"', li)
        wh = re.search(r"mw-search-result-data'>\(([\d,]+) × ([\d,]+)", li)
        if t and wh:
            pages.append({'title': html.unescape(t.group(1)), 'index': i,
                          'width': int(wh.group(1).replace(',', '')), 'height': int(wh.group(2).replace(',', ''))})
    # 全文搜索会匹配到说明文字里顺带提到的照片（搜「宇都宫饺子」出来汉堡），所以按文件名里含有几个搜索词重新排序，
    # 一个都不含的不要
    words = [w for w in re.findall(r'(?<![-a-z])[a-z]+', q.lower()) if len(w) >= 3 and w not in STOP | SMALL]   # 「-burger」是排除词，不算；dog、tea 这样的三个字母的词也算
    title = lambda p: re.sub(r'[^a-z]+', ' ', p.get('title', '').lower())
    stem = lambda w: re.sub(r'(es|s)$', '', w) if len(w) > 4 else w   # carnations ↔ carnation
    score = lambda p: sum(stem(w) in title(p) for w in words)
    # 文件名要包含关键词（只含一个词的常常是不相干的照片）：两个词以内要全部包含，三个词以上可以缺一个
    need = len(words) if len(words) <= 2 else len(words) - 1
    if must:   # 放宽的最后一轮：文件名里只要有中心词（一般是最后一个名词）就行
        need, must = 1, stem(must)
    pages = sorted([p for p in pages if words and score(p) >= need and (not must or must in title(p))], key=lambda p: (-score(p), p['index']))
    # 只要横向的 jpg 照片
    return [{'title': p['title'], 'page': 'https://commons.wikimedia.org/wiki/' + p['title'].replace(' ', '_')} for p in pages
            if re.search(r'\.jpe?g$', p['title'], re.I) and p['width'] >= 800 and p['width'] >= p['height']]


_info = {}


def info(titles):
    """查授权、作者和 960px 缩略图（API 一次最多 50 个）。授权不合适的是 None"""
    todo = [t for t in dict.fromkeys(titles) if t not in _info]
    for i in range(0, len(todo), 50):
        d = json.loads(get(API, {'action': 'query', 'format': 'json', 'titles': '|'.join(todo[i:i + 50]), 'prop': 'imageinfo',
                                 'iiprop': 'url|extmetadata|mime|size', 'iiurlwidth': 960}))
        q = d.get('query') or {}
        back = {n['to']: n['from'] for n in q.get('normalized', [])}
        for p in q.get('pages', {}).values():
            t = back.get(p.get('title'), p.get('title'))
            ii = (p.get('imageinfo') or [{}])[0]
            meta = ii.get('extmetadata', {})
            lic = plain(meta.get('LicenseShortName', {}).get('value'))
            ok = ii.get('mime') == 'image/jpeg' and ii.get('width', 0) >= 800 and ii.get('width', 0) >= ii.get('height', 1) \
                and OK_LICENSE.match(lic) and 'NC' not in lic and 'ND' not in lic
            by = plain(meta.get('Artist', {}).get('value')) or 'Unknown'
            # 缩略图地址现在是 thumb.wikimedia.org，换成 upload.wikimedia.org（同样的路径），去掉 ?utm_… 参数
            thumb = re.sub(r'^https://thumb\.wikimedia\.org/', 'https://upload.wikimedia.org/', (ii.get('thumburl') or ii.get('url') or '').split('?')[0])
            _info[t] = {'thumb': thumb, 'by': by[:120], 'license': lic, 'page': ii.get('descriptionurl', '')} if ok else None
        for t in todo[i:i + 50]:
            _info.setdefault(t, None)
    return [_info[t] for t in titles]


def colorful(data):
    """黑白老照片、扫描的文字不要：平均饱和度太低就跳过。没装 Pillow 时不检查。"""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(data)).convert('HSV').resize((64, 40))
        s = im.getchannel('S').tobytes()
        return sum(s) / len(s) > 40
    except ImportError:
        return True


SMALL = {'the', 'and', 'for', 'with', 'from', 'into', 'onto', 'that', 'this', 'what', 'when', 'your', 'their', 'about', 'after',
         'before', 'behind', 'under', 'over', 'near', 'inside', 'outside', 'its', 'our', 'his', 'her', 'are', 'was', 'new', 'old', 'one'}


def content_words(q):
    return [w for w in re.findall(r"[A-Za-z][A-Za-z'-]*", q) if len(w) >= 3 and w.lower() not in STOP | SMALL]


def tries(queries):
    """搜索顺序：① 文章的搜索词；② 词太多找不到时，拆成两个词的短搜索词（中心词 + 一个修饰词）；
    ③ 还是没有，就放宽：文件名里有中心词就行。返回 [(搜索词, 中心词或 None)]"""
    out = [(q, None) for q in queries]
    for q in queries:
        w = content_words(q)
        if len(w) >= 3:
            out += [(f'{w[0]} {w[1]}', None), (f'{w[-2]} {w[-1]}', None), (f'{w[0]} {w[-1]}', None)]
    for q in queries[:2]:
        w = content_words(q)
        if len(w) >= 2:
            out.append((q, w[-1]))
    seen, res = set(), []
    for q, m in out:
        if q.strip() and (q.lower(), m) not in seen:
            seen.add((q.lower(), m)); res.append((q, m))
    return res


def articles():
    # 旧阅读文章和商务日语：搜索词集中写在 tools/photo_queries.json
    q = ROOT / 'tools' / 'photo_queries.json'
    if q.exists():
        for aid, s in json.loads(q.read_text(encoding='utf-8')).items():
            yield aid, [x.strip() for x in s.split(" / ") if x.strip()]   # 空字符串 = 不要照片（没有贴切的）
    # （旧版韩语、英语阅读的照片已经不用了，不再下载）
    import sys
    sys.path.insert(0, str(ROOT / 'tools'))
    over = json.loads(q.read_text(encoding='utf-8')) if q.exists() else {}
    # 动漫·日剧名台词（tools/anime/*.json 的 photo 字段）
    from build_anime import photo_queries as anime_queries
    yield from ((k, v) for k, v in anime_queries().items() if k not in over)
    # 英语阅读 A1–C1（原稿在私有仓库 nihonggo-dojo-content/english/reading/out_*.json 的 photo 字段，id 是 r-<级别>-<序号>）
    for f in sorted((ROOT.parent / 'nihonggo-dojo-content' / 'english' / 'reading').glob('out_*.json')):
        for a in json.loads(f.read_text(encoding='utf-8')):
            if a['id'] not in over:   # 最后加一个从英文标题来的搜索词（前面的都找不到时用）
                yield a['id'], a.get('photo', []) + [' '.join(content_words(a['title']))]
    # 中文阅读（lang/zh-read.json 的 photo 字段，id 是 zh-r<序号>）
    zr = ROOT / 'lang' / 'zh-read.json'
    for a in (json.loads(zr.read_text(encoding='utf-8')) if zr.exists() else []):
        if a.get('photo') and a['id'] not in over:
            yield a['id'], [q.strip() for q in a['photo'].split(' / ') if q.strip()]
    # 中文会员阅读（私有仓库 nihonggo-dojo-content/zh-reading/member*.json 的 photo 字段，id 是 zr-m<序号>）
    for f in sorted((ROOT.parent / 'nihonggo-dojo-content' / 'zh-reading').glob('member*.json')):
        for a in json.loads(f.read_text(encoding='utf-8')):
            if a.get('photo') and a['id'] not in over:
                yield a['id'], [q.strip() for q in a['photo'].split(' / ') if q.strip()]
    for f in sorted((ROOT / 'tools' / 'reading').glob('*_*.txt')):
        for block in re.split(r'\n(?=# id:)', f.read_text(encoding='utf-8').strip()):
            head = block.split('\n---\n', 1)[0]
            meta = {k.strip('# ').strip(): v.strip() for k, v in (l.split(':', 1) for l in head.splitlines())}
            if 'photo' in meta and meta['id'] not in over:   # photo_queries.json 里写了的（包括空字符串＝不要照片）以它为准
                yield meta['id'], [q.strip() for q in meta['photo'].split(' / ') if q.strip()]


def main():
    # 可以只处理某些 id：python3 tools/fetch_photos.py zr-m r-B1   （按开头匹配）
    import sys
    if sys.argv[1:2] == ['--dump']:   # python3 tools/fetch_photos.py --dump 文件 [id 开头…]：导出缺照片的文章和搜索词
        idx = json.loads((OUT / 'index.json').read_text(encoding='utf-8'))
        only, out, seen = tuple(sys.argv[3:]), [], set()
        for aid, queries in articles():
            if aid not in idx and aid not in seen and queries and (not only or aid.startswith(only)):
                seen.add(aid); out.append([aid, queries])
        Path(sys.argv[2]).write_text(json.dumps({'used': sorted({v['page'] for v in idx.values()}), 'jobs': out}, ensure_ascii=False, indent=0), encoding='utf-8')
        print(len(out), 'jobs')
        return
    only = tuple(sys.argv[1:])
    OUT.mkdir(exist_ok=True)
    idx_path = OUT / 'index.json'
    idx = json.loads(idx_path.read_text(encoding='utf-8')) if idx_path.exists() else {}
    used = {v['page'] for v in idx.values()}
    todo, seen = [], set()
    # PHOTO_JOBS：事先导出的任务清单（--dump 生成），在别的仓库的 Actions 里跑时用，不需要原稿
    jobs = json.loads(Path(os.environ['PHOTO_JOBS']).read_text(encoding='utf-8')) if os.environ.get('PHOTO_JOBS') else None
    if jobs:
        used |= set(jobs['used'])
    for aid, queries in (jobs['jobs'] if jobs else articles()):
        if aid not in idx and aid not in seen and (not only or aid.startswith(only)):
            seen.add(aid); todo.append((aid, queries))
    print(len(todo), 'articles to do', flush=True)
    # 每 12 篇一组：先逐篇搜索（每篇凑到 3 个以上候选就停），再一次查这一组候选的授权，最后下载
    stop = time.time() + float(os.environ.get('PHOTO_MINUTES', '0')) * 60   # Actions 里限定时间，到点停下来好把结果存回去
    for k in range(0, len(todo), 12):
        if os.environ.get('PHOTO_MINUTES') and time.time() > stop:
            print('time budget reached', flush=True); break
        group = []
        for aid, queries in todo[k:k + 12]:
            cands = []
            for q, must in tries(queries):
                try:
                    cands += [c for c in search(q, must) if c['page'] not in used and c not in cands][:6]
                except Exception as e:
                    print('  search failed', aid, '' if QUIET else q, type(e).__name__ if QUIET else e, flush=True)
                if len(cands) >= 3:
                    break
            group.append((aid, queries, cands[:8]))
        try:
            info([c['title'] for _, _, cands in group for c in cands])
        except Exception as e:
            print('  info failed', type(e).__name__ if QUIET else e, flush=True); continue
        for aid, queries, cands in group:
            hit = data = None
            for c in cands:
                c = _info.get(c['title'])
                if not c or c['page'] in used:
                    continue
                try:
                    d = get(c['thumb'])
                except Exception as e:
                    print('  download failed', aid, '' if QUIET else c['thumb'], type(e).__name__ if QUIET else e, flush=True); continue
                if colorful(d):
                    hit, data = c, d
                    break
            if not hit:
                print('no photo', aid, '' if QUIET else queries, flush=True)
                continue
            file = f'{aid}.jpg'
            (OUT / file).write_bytes(data)
            idx[aid] = {'file': f'photos/{file}', 'by': hit['by'], 'license': hit['license'], 'page': hit['page']}
            used.add(hit['page'])
            print(aid, hit['license'], flush=True) if QUIET else print(aid, hit['license'], hit['by'][:40], hit['page'], flush=True)
            idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=1), encoding='utf-8')
    print(len(idx), 'photos')


if __name__ == '__main__':
    main()
