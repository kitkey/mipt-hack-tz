import html
import json
import os
import re

import docx

from .confidence import label as conf_label, show_threshold
from .model import status_title
from .rag.verify import BASIS as NORM_BASIS, NOT_CHECKED as NORM_NOT_CHECKED

SEV_RU = {"high": "высокий", "medium": "средний", "low": "низкий"}
# ключи основания, которые не выводятся (служебные или уже показаны в строке), и подписи остальных
EV_SKIP = {"найдено", "нормы", "source", "norm_ids", "цитата найдена в тексте", "основание из базы норм", "второй блок", "второе место", "вторая цитата",
           NORM_BASIS}
EV_LABEL = {"блок отчёта": "раздел", "довод проверки": "довод второй проверки", "обратная связь": "решения закупщиков"}
GOST_STATUS = {"approved": "утверждён", "verified": "сверен", "draft": "черновик"}


def to_json(result: dict, path: str):
    data = {k: v for k, v in result.items() if not k.startswith("_")}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


STATUS_CLS = {"ошибка": "st-err", "неясно": "st-unc", "ручная проверка": "st-rev", "рекомендация": "st-adv"}

CSS = """
:root{--fg:#1d1d1f;--muted:#666;--line:#dcdfe5;--bg:#fff;--soft:#f4f6fa;--accent:#3b5bdb;--hi:#c92a2a;--mid:#d9480f;--lo:#5c7cfa;--rev:#7048e8;--quote:#fffbe6}
body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:24px;color:var(--fg);background:var(--bg)}
h1{font-size:22px;margin:0 0 6px}.meta{color:var(--muted);margin-bottom:14px;font-size:14px}
.summary{background:var(--soft);border-left:4px solid var(--accent);padding:12px 16px;margin:12px 0}
.warn{border-left:4px solid var(--mid);background:#fff4e6;padding:8px 14px;margin:10px 0}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{border:1px solid var(--line);padding:8px;vertical-align:top;text-align:left}
th{background:#f0f2f5;position:sticky;top:0}blockquote{margin:6px 0 0;padding:4px 8px;background:var(--quote);border-left:3px solid #f0c000}
.sev{font-weight:600}.sev-high .sev{color:var(--hi)}.sev-medium .sev{color:var(--mid)}.sev-low .sev{color:var(--lo)}
.st{display:inline-block;margin-top:4px;font-size:12px;padding:1px 6px;border-radius:4px;border:1px solid currentColor}
.st-adv{color:var(--muted)}.st-err{color:var(--hi)}.st-unc{color:var(--mid)}.st-rev{color:var(--rev)}
details{margin-top:6px;font-size:12px;color:var(--muted)}details ul{padding-left:16px;margin:4px 0}
.fb{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}.fb button{font-size:12px;border:1px solid var(--line);background:#fff;border-radius:4px;padding:3px 7px;cursor:pointer}
.fb button.on{background:var(--accent);color:#fff;border-color:var(--accent)}.fb input{font-size:12px;width:100%;margin-top:4px;border:1px solid var(--line);border-radius:4px;padding:3px 6px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0;padding:0;list-style:none}.chips li{background:var(--soft);border-radius:12px;padding:3px 10px;font-size:13px}
.conf b{font-size:16px}.conf small{display:block;color:var(--muted);font-size:12px}.conf-none{color:var(--muted);font-size:13px}
.ctl{display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin:10px 0;font-size:14px}.ctl input[type=range]{vertical-align:middle}
.nm{color:var(--muted)}.nm-off{color:var(--mid);font-weight:600}
"""

FB_JS = """
<script>
document.addEventListener('click', async (e) => {
  const btn = e.target.closest('.fb button'); if (!btn) return;
  const box = btn.closest('.fb'); const comment = box.querySelector('input.fbc').value;
  const r = await fetch('/api/feedback', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({run_id: box.dataset.rid, finding_id: box.dataset.fid, decision: btn.dataset.d, comment: comment})});
  if (r.ok) { box.querySelectorAll('button').forEach(b => b.classList.remove('on')); btn.classList.add('on'); }
  else { alert('Не сохранилось: ' + (await r.text())); }
});
</script>"""


def ev_label(key: str) -> str:
    m = re.fullmatch(r"уверенность \(модель (при .+)\)", key)
    return f"уверенность {m.group(1)}" if m else EV_LABEL.get(key, key)


def ev_value(key: str, v) -> str:
    if key == "пункт ГОСТ" and isinstance(v, dict):
        head = ", ".join(x for x in (v.get("designation") or "", f"п. {v['clause']}" if v.get("clause") else "",
                                     GOST_STATUS.get(v.get("status"), v.get("status") or "")) if x)
        return head + (f": «{v['quote']}»" if v.get("quote") else "")
    if key == "блок отчёта" and isinstance(v, str):
        return re.sub(r"^на проверку: ", "«На проверку», ", v)
    if key == "применимость ГОСТ" and isinstance(v, dict):
        return "; ".join(x for x in (v.get("why") or "", f"«{v['quote']}»" if v.get("quote") else "") if x)
    if isinstance(v, bool):
        return "да" if v else "нет"
    if isinstance(v, list):
        return "; ".join(ev_value("", x) for x in v)
    if isinstance(v, dict):
        return "; ".join(f"{a}: {ev_value('', b)}" for a, b in v.items())
    return str(v)


def norm_mark(ev: dict) -> str:
    """Признак приложенного пункта нормы: «проверено моделью» или «применимость не проверена»; без пункта пусто."""
    return str(ev.get(NORM_BASIS) or "") if ev.get("пункт нормы") else ""


def _evidence(f: dict) -> str:
    esc = html.escape
    ev = f.get("evidence") or {}
    mark = norm_mark(ev)
    tail = {"пункт нормы": f' <span class="nm{" nm-off" if mark == NORM_NOT_CHECKED else ""}">({esc(mark)})</span>'} if mark else {}
    items = [f"<li>{esc(ev_label(k))}: {esc(ev_value(k, v))}{tail.get(k, '')}</li>" for k, v in ev.items()
             if k not in EV_SKIP and v not in (None, "", [], {})]
    return f"<details><summary>Основание</summary><ul>{''.join(items)}</ul></details>" if items else ""


CONF_JS = """
<script>
(() => {
  const body = document.getElementById('frows'); if (!body) return;
  const rng = document.getElementById('cmin'), val = document.getElementById('cminv'), none = document.getElementById('cnone'),
        sort = document.getElementById('csort'), shown = document.getElementById('cshown');
  const q = new URLSearchParams(location.search);
  if (q.has('min')) rng.value = Math.max(0, Math.min(100, parseInt(q.get('min')) || 0));
  const conf = r => r.dataset.conf === '' ? null : +r.dataset.conf;
  function apply() {
    const min = +rng.value; val.textContent = min + ' %';
    const rows = [...body.rows];
    rows.sort((a, b) => {
      if (sort.value === 'idx') return a.dataset.idx - b.dataset.idx;
      const ca = conf(a), cb = conf(b);
      if (ca === null && cb === null) return a.dataset.idx - b.dataset.idx;
      if (ca === null) return 1; if (cb === null) return -1;
      return sort.value === 'desc' ? cb - ca : ca - cb;
    });
    let n = 0;
    rows.forEach(r => { const c = conf(r); const vis = c === null ? none.checked : c >= min;
      r.style.display = vis ? '' : 'none'; n += vis; body.appendChild(r); });
    shown.textContent = n; shown.parentElement.hidden = n === rows.length;
  }
  [rng, none, sort].forEach(el => el.addEventListener('input', apply));
  apply();
})();
</script>"""


def _conf_cell(f: dict) -> str:
    esc = html.escape
    pct = f.get("confidence_pct")
    if pct is None:
        return f'<td class="conf"><span class="conf-none">{esc(conf_label(f))}</span></td>'
    return f'<td class="conf"><b>{int(pct)} %</b><small>{esc(basis_title(f.get("confidence_basis", "")))}</small></td>'


def _conf_controls(total: int) -> str:
    t = show_threshold()
    return (f'<div class="ctl"><label>Порядок: <select id="csort"><option value="idx">по риску</option>'
            '<option value="desc">уверенность: сначала высокая</option><option value="asc">уверенность: сначала низкая</option></select></label>'
            f'<label>Уверенность от <input type="range" id="cmin" min="0" max="100" step="5" value="{t}"> <span id="cminv">{t} %</span></label>'
            '<label><input type="checkbox" id="cnone" checked> без оценки</label>'
            f'<span id="cshownw" hidden>показано <span id="cshown">{total}</span> из {total}</span></div>')


def basis_title(basis: str) -> str:
    """Подпись основания уверенности: «модель при второй проверке» показывается как «при второй проверке»."""
    return re.sub(r"^модель (при )", r"\1", basis or "")


def _model_note(block: dict | None) -> str:
    note = (block or {}).get("note")
    return f'<div class="warn"><b>Пояснение модели.</b> {html.escape(note)}</div>' if note else ""


def _model_table(rows: list[str], block: dict | None = None) -> str:
    if not rows:
        return ""
    block = block or {}
    esc = html.escape
    sev = block.get("by_severity", {})
    sev_chips = "".join(f"<li>{SEV_RU.get(k, k)} риск: {sev[k]}</li>" for k in ("high", "medium", "low") if sev.get(k))
    cat_chips = "".join(f"<li>{esc(k)}: {v}</li>" for k, v in sorted(block.get("by_category", {}).items(), key=lambda x: -x[1]))
    table = ('<table><thead><tr><th>№</th><th>Риск и статус</th><th>Уверенность</th><th>Категория</th><th>Где и цитата</th><th>Почему</th>'
             f'<th>Как исправить</th><th>Норма</th></tr></thead><tbody id="mrows">{"".join(rows)}</tbody></table>')
    return f'<h2>На проверку ({len(rows)})</h2><ul class="chips">{sev_chips}</ul><ul class="chips">{cat_chips}</ul>{table}'


def _unchecked_table(rows: list[str], block: dict | None = None) -> str:
    if not rows:
        return ""
    table = ('<table><thead><tr><th>№</th><th>Риск и статус</th><th>Уверенность</th><th>Категория</th><th>Где и цитата</th><th>Что не найдено</th>'
             f'<th>Что сделать</th><th>Норма</th></tr></thead><tbody id="urows">{"".join(rows)}</tbody></table>')
    return f'<h2>Не проверено ({len(rows)})</h2><div class="meta">Сведений нет в переданных файлах.</div>' + table


def _passport_html(result: dict) -> str:
    p = result.get("passport")
    if not p:
        return ""
    esc = html.escape
    refs = "".join(f"<li>{esc(k)}: {esc(v.get('версия') or v.get('получено'))}</li>" for k, v in p.get("справочники", {}).items())
    files = "".join(f"<li>{esc(x['файл'])}: sha256 {esc(x['sha256'][:16])}…</li>" for x in p.get("файлы", []))
    return (f"<details><summary>Паспорт проверки</summary><ul>{refs}{files}<li>модель: {esc(str(p.get('модель')))}</li>"
            f"<li>справочники только из локальных копий: {'да' if p.get('справочники офлайн') else 'нет'}</li></ul></details>")


def to_html(result: dict, run_id: str | None = None) -> str:
    esc = html.escape
    rows, model_rows, unchecked_rows = [], [], []
    for f in result.get("findings", []):
        target = {"llm_review": model_rows, "unchecked": unchecked_rows}.get(f.get("section"), rows)
        norms = "<br>".join(
            f'<a href="{esc(result["norms"][n]["url"])}" target="_blank">{esc(result["norms"][n]["title"])}</a>' if result["norms"][n]["url"]
            else esc(result["norms"][n]["title"]) for n in f["norm_ids"] if n in result.get("norms", {}))
        status = f.get("status") or ("ручная проверка" if f["needs_review"] else "ошибка")
        fbk = ""
        if run_id:
            fid = esc(f["id"])
            fbk = (f'<div class="fb" data-rid="{esc(run_id)}" data-fid="{fid}"><button data-d="accept">Принять</button>'
                   '<button data-d="reject">Отклонить</button><button data-d="review">На проверку</button>'
                   '<input class="fbc" placeholder="Комментарий закупщика"></div>')
        ev = f.get("evidence") or {}
        second = (f'<br><a href="#src-{esc(str(ev.get("второй блок", "")), quote=True)}">{esc(str(ev.get("второе место", "")))}</a><blockquote>{esc(str(ev["вторая цитата"]))}</blockquote>'
                  if ev.get("вторая цитата") else "")
        conf = f.get("confidence_pct")
        qhtml = f"<blockquote>{esc(f['quote'])}</blockquote>" if f["quote"] else ""
        target.append(
            f'<tr class="sev-{f["severity"]}" data-conf="{"" if conf is None else int(conf)}" data-idx="{len(target)}"><td>{f["id"]}</td><td><span class="sev">{SEV_RU.get(f["severity"], "")}</span><br>'
            f'<span class="st {STATUS_CLS.get(status, "")}">{esc(status_title(status))}</span></td>{_conf_cell(f)}'
            f'<td>{esc(f["category"])}</td><td><a href="#src-{esc(f["block_id"], quote=True)}">{esc(f["location"])}</a>{qhtml}{second}</td>'
            f'<td>{esc(f["explanation"])}{_evidence(f)}</td><td>{esc(f["fix"])}{fbk}</td><td>{norms}</td></tr>')
    doc = result.get("_doc")
    sources = "".join(f'<p id="src-{esc(b.id, quote=True)}"><b>{esc(b.location())}</b><br>{esc(b.text)}</p>' for b in doc.blocks) if doc else ""
    stats = result.get("stats", {})
    cats = "".join(f"<li>{esc(k)}: {v}</li>" for k, v in sorted(stats.get("by_category", {}).items(), key=lambda x: -x[1]))
    groups = "".join(f"<li><b>{esc(k)}: {v}</b></li>" for k, v in stats.get("by_group", {}).items())
    sts = "".join(f"<li>{esc(status_title(k))}: {v}</li>" for k, v in stats.get("by_status", {}).items())
    warn = "".join(f"<li>{esc(w)}</li>" for w in result.get("warnings", []))
    model = str(result.get("model") or "").removeprefix("openrouter/")
    mode = f"Модель {model}" if model else {"rules": "Только правила", "rules+llm": "Правила и модель"}.get(result.get("mode", ""), "Без модели")
    pii = result.get("pii_masked") or {}
    pii_text = ("ПД заменены метками: " + ", ".join(f"{k} {v}" for k, v in pii.items())) if pii else "персональных данных не найдено" if model else ""
    seconds = str(result.get("seconds", "")).replace(".", ",")
    meta = ", ".join(x for x in (mode, f"время {seconds} с" if seconds else "", pii_text) if x)
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Проверка ТЗ: {esc(result.get('file', ''))}</title>
<style>{CSS}</style></head><body>
<h1>Проверка ТЗ: {esc(result.get('file', ''))}</h1>
<div class="meta">{esc(meta)}</div>
<div class="summary">{esc(result.get('summary', ''))}</div>{_model_note(stats.get('model_block'))}
<ul class="chips">{sts}</ul>{f'<ul class="chips">{groups}</ul>' if groups else ''}<ul class="chips">{cats}</ul>{f'<div class="warn"><b>Предупреждения</b><ul>{warn}</ul></div>' if warn else ''}
{_conf_controls(len(rows))}
<table><thead><tr><th>№</th><th>Риск и статус</th><th>Уверенность</th><th>Категория</th><th>Где и цитата</th><th>Почему</th><th>Как исправить</th><th>Норма</th></tr></thead>
<tbody id="frows">{''.join(rows)}</tbody></table>{_model_table(model_rows, stats.get('model_block'))}{_unchecked_table(unchecked_rows)}{_passport_html(result)}<h2>Текст документов</h2>{sources}{CONF_JS}{FB_JS if run_id else ''}</body></html>"""


def to_compare_html(cmp: dict) -> str:
    esc = html.escape

    def block(title, items, cls):
        if not items:
            return f"<h2>{title}: 0</h2>"
        rows = "".join(f'<tr><td>{esc(f["category"])}</td><td>{esc(f["location"])}<blockquote>{esc(f["quote"])}</blockquote></td>'
                       f'<td>{esc(f["explanation"])}</td></tr>' for f in items)
        return f'<h2 class="{cls}">{title}: {len(items)}</h2><table><tr><th>Категория</th><th>Где и цитата</th><th>Замечание</th></tr>{rows}</table>'

    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Сравнение версий ТЗ</title>
<style>{CSS} h2{{font-size:17px;margin:22px 0 8px}} .fixed{{color:#2b8a3e}} .added{{color:var(--hi)}}</style></head><body>
<h1>Сравнение версий ТЗ</h1><div class="meta">Было: {esc(cmp['old'])}, стало: {esc(cmp['new'])}</div>
<div class="summary">{esc(cmp['summary'])}</div>
{block("Исправлено", cmp['fixed'], 'fixed')}{block("Осталось", cmp['remained'], '')}{block("Появилось", cmp['added'], 'added')}
</body></html>"""


def _sentence(s: str) -> str:
    s = (s or "").strip()
    return s if not s or s[-1] in ".!?…»\"" else s + "."


def to_docx_comments(result: dict, src_path: str, out_path: str) -> bool:
    doc_obj = result.get("_doc")
    if doc_obj is None or not src_path.lower().endswith(".docx"):
        return False
    d = docx.Document(src_path)
    paras, rows = {}, {}
    pi, ti = 0, 0
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = docx.text.paragraph.Paragraph(child, d)
            if " ".join(p.text.split()):
                pi += 1
                paras[pi] = p
        elif tag == "tbl":
            t = docx.table.Table(child, d)
            for ri, row in enumerate(t.rows):
                rows[(ti + 1, ri + 1)] = row
            ti += 1
    added = 0
    paths = result.get("_paths", [src_path])
    try:
        file_index = [os.path.abspath(p) for p in paths].index(os.path.abspath(src_path))
    except ValueError:
        return False
    prefix = f"a{file_index}_" if file_index else ""
    entries = []
    for finding in result.get("findings", []):
        entries.append(finding)
        ev = finding.get("evidence") or {}
        if ev.get("второй блок"):
            other = dict(finding, block_id=ev["второй блок"], quote=ev["вторая цитата"])
            other["evidence"] = dict(ev, **{"второе место": finding["location"], "вторая цитата": finding["quote"]})
            entries.append(other)
    for f in entries:
        bid = f["block_id"]
        if prefix:
            if not bid.startswith(prefix):
                continue
            bid = bid[len(prefix):]
        elif bid.startswith("a"):
            continue
        target = None
        if bid[0] in "hp" and bid[1:].isdigit():
            target = paras.get(int(bid[1:]))
        elif bid.startswith("t") and "r" in bid:
            # строка таблицы «t3r2» или строка вложенной таблицы «t3r2n1r4» (вложенная таблица 1 в строке 2 таблицы 3)
            m = re.match(r"t(\d+)r(\d+)(?:n(\d+)r(\d+))?$", bid)
            row = rows.get((int(m.group(1)), int(m.group(2)))) if m else None
            if row is not None and m.group(3):
                inner = [t for c in row.cells for t in c.tables]
                k, r = int(m.group(3)), int(m.group(4))
                row = inner[k - 1].rows[r - 1] if k <= len(inner) and r <= len(inner[k - 1].rows) else row
            if row is not None:
                cell = next((c for c in row.cells if f["quote"][:30] in " ".join(c.text.split())), row.cells[min(1, len(row.cells) - 1)])
                head = " ".join(f["quote"].split())[:30]
                target = next((p for p in cell.paragraphs if p.runs and head and head in " ".join(p.text.split())), None) or \
                    next((p for p in cell.paragraphs if p.runs), None)
        if target is None or not target.runs:
            continue
        section = f.get("section")
        text = {"llm_review": "На проверку. ", "unchecked": "Не проверено, сведений нет в переданных файлах. "}.get(section, "")
        text += f'[{f["id"]}] {f["category"]}, {SEV_RU.get(f["severity"], "")} риск'
        text += ", на проверку" if f["needs_review"] and section not in ("llm_review", "unchecked") else ""
        text += f', уверенность {f["confidence_pct"]} %' if f.get("confidence_pct") is not None else ""
        text += f'. {_sentence(f["explanation"])} Как исправить: {_sentence(f["fix"])}'
        ev = f.get("evidence") or {}
        if ev.get("вторая цитата"):
            text += f" Второе место: {ev.get('второе место', '')}, «{ev['вторая цитата']}»."
        norms = [result["norms"][n]["title"] for n in f["norm_ids"] if n in result.get("norms", {})]
        if norms:
            text += " Норма: " + "; ".join(norms) + "."
        if ev.get("пункт нормы") and ev.get("цитата нормы"):
            mark = norm_mark(ev)
            text += f" Пункт нормы {ev['пункт нормы']}{f' ({mark})' if mark else ''}: «{ev['цитата нормы']}»."
        try:
            d.add_comment(target.runs, text=text, author="Проверка ТЗ", initials="ТЗ")
            added += 1
        except Exception:
            continue
    d.save(out_path)
    return added > 0


def export_failed(result: dict, what: str, e: Exception):
    """Сбой выгрузки в Word не теряет результат проверки: причина идёт в предупреждения отчёта."""
    result.setdefault("warnings", []).append(f"Выгрузка {what} не выполнена ({type(e).__name__}: {str(e)[:120]}), "
                                             "результат проверки сохранён в отчёте JSON и HTML")


def _comments(result: dict, src_path: str, out: str) -> bool:
    try:
        return to_docx_comments(result, src_path, out)
    except Exception as e:
        export_failed(result, f"комментариев Word ({os.path.basename(out)})", e)
        return False


def write_all(result: dict, src_path: str, out_dir: str) -> dict:
    """Отчёты JSON и HTML и файлы Word с комментариями. Word выгружается первым: его сбой попадает в предупреждения отчёта."""
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.splitext(os.path.basename(src_path))[0]
    paths = {"json": os.path.join(out_dir, base + ".report.json"), "html": os.path.join(out_dir, base + ".report.html")}
    docx_out = os.path.join(out_dir, base + ".comments.docx")
    if _comments(result, src_path, docx_out):
        paths["docx"] = docx_out
    for i, attachment in enumerate(result.get("_paths", [])[1:], 1):
        if attachment.lower().endswith(".docx"):
            attachment_out = os.path.join(out_dir, f"attachment_{i}.comments.docx")
            if _comments(result, attachment, attachment_out):
                paths[f"docx_attachment_{i}"] = attachment_out
    to_json(result, paths["json"])
    with open(paths["html"], "w", encoding="utf-8") as f:
        f.write(to_html(result))
    return paths
