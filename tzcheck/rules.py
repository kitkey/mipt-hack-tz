import json
import os
import re
from collections import defaultdict
from datetime import datetime

from .model import Finding
from . import okpd as okpd_dict
from . import customer
from . import refdata
from .parse import OKPD_RE, ParsedDoc, Block

LATIN_STOP = {
    "USB", "USB3", "GIGE", "VISION", "BLUETOOTH", "WI-FI", "WIFI", "AC", "DC", "AC/DC", "WINDOWS", "LINUX", "SDK", "OPENCV",
    "ROS", "IP", "LED", "LCD", "PDF", "RS", "HDMI", "PC", "ISO", "IEC", "SI", "CE", "API", "CPU", "GPU", "RAM", "SSD", "HDD",
    "PCI", "IEEE", "TYPE", "REV", "VER", "CLASS", "LAN", "ETHERNET", "TCP", "PCI", "PCIE", "DDR", "DDR4", "DDR5", "SATA", "NVME", "FULL", "HD", "TFT", "IPS", "RGB", "UPS",
    "PDF/A", "XML", "JSON", "HTML", "CSV", "TXT", "DOCX", "XLSX", "OS", "MS", "ID", "QR", "NFC", "GPS", "GSM", "LTE", "SIM",
    "IP54", "IP65", "IP67", "TYPE-C", "MICRO", "MINI", "PRO", "MAX", "PLUS", "LITE", "TRMS", "CAT", "EN", "DIN", "ANSI", "ASTM",
    "HRC", "HRB", "HRA", "HB", "HBW", "HV", "HL", "RA", "RZ", "RQ", "VGA", "DVI", "DP", "MPEG", "JPEG", "PNG", "RAW", "CMOS", "CCD", "PWM", "PID", "PLC", "CAN", "MODBUS", "PROFINET", "OPC", "UA",
    "AVX", "AMX", "SSE", "EXPRESS", "GEN", "ECC", "GDDR", "SAS", "SFP", "QSFP", "RJ", "NVLINK", "OCP", "BMC", "IPMI", "RAID",
    "TPM", "UEFI", "BIOS", "POE", "DIMM", "RDIMM", "LRDIMM", "NVM", "PCI-E",
    "CSS", "INCITS", "MSK", "RFC", "ITU", "ECMA",
    "LGA", "AM4", "AM5", "MOLEX", "OEM", "BOX", "ATX", "SODIMM",
}
# Перед латинским обозначением стоит слово, показывающее, что это стандарт, шкала или название помещения на объекте, а не товар.
NOT_MODEL_CTX_RE = re.compile(r"(?:стандарт\w*|шкал\w*|протокол\w*|участ(?:ок|ка|ке)|цех\w*|помещени\w*|корпус\w*|здани\w*)[\s\w.«\"№-]{0,12}$", re.I)
STD_DESIGNATION_RE = re.compile(r"\d+(?:\.\d+)*-(?:19|20)\d\d\b")
SOFTWARE_SUBJECT_RE = re.compile(r"неисключительн\w+\s+прав|прав\w*\s+(?:на\s+)?использовани\w*\s+(?:программ|ПО\b)|программн\w+\s+обеспечени", re.I)
SI_ATTR_RE = re.compile(r"(?:тип|вид|наличие|количество|материал|марка|модель|способ\w*)\s+$", re.I)
# Срок годности как требование к товару (не упоминание в маркировке или приёмке) заменяет гарантийный срок у расходуемых товаров.
SHELF_LIFE_RE = re.compile(r"остаточн\w+\s+срок\w*\s+годности|срок\w*\s+годности\s*(?:[:—–-]|не менее|должен|составляет)", re.I)
WARRANTY_TERM_RE = re.compile(r"в течение (?:всего )?срока (?:действия|исполнения) (?:договора|контракта)|остаточн\w+\s+срок\w*\s+годности|срок\w*\s+годности\s*(?:[:—–-]|не менее|должен|составляет)", re.I)
NOT_WARRANTY_RE = re.compile(r"банковск\w*\s+гаранти|независим\w*\s+гаранти|гаранти\w*\s+(?:исполнения|возврата\s+аванса)|обеспечени\w*\s+исполнени", re.I)
KNOWN_BRANDS = {
    "rigol", "fluke", "hioki", "keysight", "agilent", "tektronix", "binder", "mitutoyo", "optitrack", "hikrobot", "basler",
    "national", "siemens", "schneider", "otis", "kone", "schindler", "thyssenkrupp", "mitsubishi", "bosch", "makita", "dell",
    "lenovo", "intel", "amd", "microsoft", "apple", "samsung", "canon", "xerox", "brother", "epson", "sartorius", "ohaus",
    "mettler", "toledo", "testo", "megeon", "owon", "uni-t", "aktakom", "zeiss", "olympus", "leica", "nikon", "hitachi",
    "panasonic", "philips", "honeywell", "omron", "festo", "kuka", "fanuc", "abb", "dji", "nvidia", "cisco", "huawei", "hp",
}
MODEL_RE = re.compile(r"\b([A-Z][A-Za-z]{2,}|[A-Z]{2,})(?:[\s-]+([A-Z]{0,4}[\s-]?\d[\w-]*))")
LATIN_WORD_RE = re.compile(r"\b[A-Za-z][A-Za-z-]{1,}\b")
SI_WORDS = re.compile(
    r"(?<![а-яё])(?:мультиметр|осциллограф|термогигрометр|пирометр|анемометр|калибратор|весы|штангенциркул|микрометр|манометр|термометр|гигрометр|дальномер|ваттметр|амперметр|вольтметр|"
    r"анализатор|нутромер|штангенрейсмас|тесламетр|угломер|радиусомер|щуп|индикатор часов|плита поверочн|уровень\s+(?:строительн|брусков|рамн|лазерн)|рулетк|линейк\w*\s+(?:измерительн|металлическ|поверочн|лекальн|\d)|тахометр|люксметр|шумомер|"
    r"динамометр|твердомер|толщиномер|профилометр|частотомер|измерител(?!ьн)|датчик)", re.I)
VAGUE_RE = re.compile(
    r"по возможности|при необходимости|высокого качества|современн\w+|(?<![а-яё])надлежащ\w+ качеств|и т\.\s?п\.|и т\.\s?д\.|"
    r"иное по согласованию|в согласованные с Заказчиком сроки|по согласованию с Заказчиком", re.I)
NOTE_RE = re.compile(
    r"\(\s*вместо\s+[\wА-ЯЁа-яё]|в пределах плановой суммы|не требуются;|поставляется лабораторией|"
    r"конфигурацией не предусмотрен|на усмотрение исполнителя|устранены разночтения|исправлено по замечани|по замечаниям закупщик|"
    r"было:|стало:|\bTODO\b|уточнить у|проверить у", re.I)
DAYS_RE = re.compile(r"(?<![\d(])(\d{1,4})\s+(календарн\w+|рабоч\w+)\s+(дн\w+|недел\w+)", re.I)
GOST_RE = re.compile(r"\b(ГОСТ|СНиП|СП|ГОСТ Р|ГОСТ IEC|ГОСТ ISO|ГОСТ Р ИСО|ГОСТ Р МЭК)\s*№?\s*(\d[\d.]*)\s*[-–]\s*(\d{2,4})", re.I)
GUARANTEE_RE = re.compile(r"гаранти", re.I)
PROC_RE = re.compile(r"в составе заявки|в заявке (?:участник|поставщик)|(?:участник|поставщик)\w* (?:закупки )?указывает в (?:составе )?заявк|"
                     r"национальн\w+ режим|в реестр\w* российской (?:промышленной|радиоэлектронной) продукции", re.I)
INSTRUCTION_RE = re.compile(r"(?:участник\w*|поставщик\w*)\s+(?:закупки\s+)?указыва|в\s+столбц\w*\s*\(?\d|следует\s+указывать|"
                            r"значени\w*\s+показател\w*|инструкци\w*\s+по\s+заполнени", re.I)
REF_RE = re.compile(r"референс\w*\s*[:—–-]\s*(.{2,160}?)\s+или\s+эквивалент\w*", re.I)
REF_NOTE_RE = re.compile(r"(?:приведен\w*|указан\w*|даны)\s+(?:как|в качестве)\s+референс\w*|референс\w*\s+допускают", re.I)
MONTHS_RE = re.compile(r"\d+\s*(?:\([а-яё ]+\)\s*)?(?:месяц|мес\.|год|лет)", re.I)
OPPOSITES = [("грузов", "пассажирск"), ("поставк", r"выполнени\w* работ"), ("новый", r"бывш\w* в употреблени")]
INACTIVE_WORDS = {"Заменён": "заменён", "Отменён": "отменён", "Утратил силу в РФ": "утратил силу в РФ", "Истёк срок действия": "утратил силу: истёк срок действия"}


def _sentence_around(text: str, start: int, end: int, limit: int = 220) -> str:
    left = max(text.rfind(";", 0, start), text.rfind(". ", 0, start), text.rfind("|", 0, start))
    right_candidates = [i for i in (text.find(";", end), text.find(". ", end), text.find("|", end)) if i != -1]
    right = min(right_candidates) if right_candidates else len(text)
    frag = text[left + 1:right].strip(" .;|")
    return frag[:limit] if frag else text[start:end]


UNITS_W = ["", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять", "одиннадцать", "двенадцать",
           "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
TENS_W = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят", "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
HUNDREDS_W = ["", "сто", "двести", "триста", "четыреста", "пятьсот", "шестьсот", "семьсот", "восемьсот", "девятьсот"]


def num_words(n: int, feminine: bool = False) -> str:
    if n == 0:
        return "ноль"
    if not 0 < n < 10000:
        return str(n)
    parts = []
    th, n = divmod(n, 1000)
    if th:
        parts.append({1: "одна тысяча", 2: "две тысячи", 3: "три тысячи", 4: "четыре тысячи"}.get(th, UNITS_W[th] + " тысяч"))
    h, n = divmod(n, 100)
    if h:
        parts.append(HUNDREDS_W[h])
    if n >= 20:
        t, n = divmod(n, 10)
        parts.append(TENS_W[t])
    if n:
        w = UNITS_W[n]
        if feminine and n in (1, 2):
            w = "одна" if n == 1 else "две"
        parts.append(w)
    return " ".join(parts)


def _locate(text: str, quote: str) -> str | None:
    """Фрагмент текста, совпадающий с цитатой без учёта регистра и пробелов, или None."""
    if quote in text:
        return quote
    words = quote.split()
    if not words:
        return None
    m = re.search(r"\s*".join(re.escape(w) for w in words), text, re.I)
    return m.group(0) if m else None


def _f(block: Block, quote: str, category: str, explanation: str, fix: str, severity="medium", norms=None,
       review=False, source="rule", confidence=0.75) -> Finding:
    quote = _locate(block.text, quote) or block.text[:200]
    return Finding(block.id, quote, category, explanation, fix, severity, norms or [], review, source, confidence,
                   block.location())


def _practice(f: Finding, key: str) -> Finding:
    f._practice = key
    return f


def check_brands(doc: ParsedDoc) -> list[Finding]:
    out = []
    for b in doc.blocks:
        covered = []
        for m in REF_RE.finditer(b.text):
            covered.append((m.start(), m.end()))
            out.append(_f(b, m.group(0).strip(), "Ограничение конкуренции",
                          f"В описании позиции назван товар-образец ({m.group(1).strip()[:80]}). Даже с «или эквивалент» это ориентирует "
                          "участников на одного производителя, а эквивалентность приходится доказывать относительно чужой модели, а не требований ТЗ.",
                          "Удалить «Референс: …». Параметры, ради которых выбран образец, перечислить характеристиками с числовыми границами.",
                          "medium", ["223-FZ-3-6.1-2", "223-FZ-3-6.1-3", "223-FZ-3-1-2"], False, "rule:brand", 0.75))
        for m in REF_NOTE_RE.finditer(b.text):
            out.append(_f(b, _sentence_around(b.text, m.start(), m.end()), "Ограничение конкуренции",
                          "Оговорка, что модели приведены как референсы, не снимает ограничения: конкретные товарные знаки остаются в описании предмета закупки.",
                          "Убрать модели-референсы из ТЗ вместе с этой оговоркой, требования оставить характеристиками.",
                          "medium", ["223-FZ-3-6.1-2", "223-FZ-3-6.1-3"], False, "rule:brand", 0.7))
        hits = []
        for m in MODEL_RE.finditer(b.text):
            word = m.group(1)
            if word.upper() in LATIN_STOP or len(word) < 2 or any(s <= m.start() < e for s, e in covered):
                continue
            if NOT_MODEL_CTX_RE.search(b.text[max(0, m.start() - 40):m.start()]) or STD_DESIGNATION_RE.search(m.group(0)) \
                    or re.match(r"/(?:19|20)\d\d\b", b.text[m.end():]):
                continue
            hits.append((m.start(), m.end(), m.group(0)))
        for m in LATIN_WORD_RE.finditer(b.text):
            if m.group(0).lower() in KNOWN_BRANDS and not any(s <= m.start() < e for s, e, _ in hits) and not any(s <= m.start() < e for s, e in covered):
                hits.append((m.start(), m.end(), m.group(0)))
        if not hits:
            continue
        names = ", ".join(sorted({h[2] for h in hits}))[:160]
        equiv = re.search(r"эквивалент|аналог", b.text, re.I)
        start, end = min(h[0] for h in hits), max(h[1] for h in hits)
        quote = _sentence_around(b.text, start, end)
        compat = re.search(r"совместим|взаимодейств\w* с|подключени\w* к|поддерживать\s+(?:следующ\w+\s+)?(?:браузер|операционн)", b.text, re.I)
        if compat and re.search(r"операционн\w+\s+систем|под управлением|браузер", b.text, re.I):
            pass  # список поддерживаемых ОС — требование к среде, а не выбор марки
        elif compat and not equiv:
            out.append(_f(b, quote, "Ограничение конкуренции",
                          f"Марка ({names}) названа в требовании совместимости. Без «или эквивалент» это допустимо, только если нужна совместимость "
                          "с уже имеющимся у заказчика оборудованием (223-ФЗ, ст. 3, ч. 6.1, п. 3), и это должно быть написано в ТЗ.",
                          "Указать, с каким имеющимся оборудованием нужна совместимость, или описать интерфейс и параметры вместо марки.",
                          "medium", ["223-FZ-3-6.1-3"], True, "rule:brand", 0.6))
        elif equiv:
            out.append(_f(b, quote, "Ограничение конкуренции",
                          f"Требование привязано к конкретной модели ({names}). Слова «или эквивалент» по 223-ФЗ допустимы, "
                          "но без измеримых параметров эквивалентность не проверить, и участник с другим товаром рискует отклонением.",
                          "Убрать марку и модель, перечислить характеристики с числовыми границами («не менее», «не более»), по которым сравнивается эквивалент.",
                          "medium", ["223-FZ-3-6.1-2", "223-FZ-3-6.1-3", "223-FZ-3-1-2"], True, "rule:brand", 0.7))
        elif not any(w.lower() in KNOWN_BRANDS for h in hits for w in LATIN_WORD_RE.findall(h[2])) and not re.search(r"марк|модел|производств|фирм|товарн\w+ знак", b.text, re.I):
            out.append(_f(b, quote, "Ограничение конкуренции",
                          f"Латинское обозначение с номером ({names}) похоже на модель конкретного производителя. Если это стандарт интерфейса или типоразмер, замечание снять.",
                          "Если это модель, заменить её характеристиками или добавить «(или эквивалент)».",
                          "medium", ["223-FZ-3-6.1-2", "223-FZ-3-6.1-3"], True, "rule:brand", 0.5))
        else:
            out.append(_f(b, quote, "Ограничение конкуренции",
                          f"Указана конкретная марка или модель ({names}) без слов «или эквивалент»: это прямо запрещено для описания предмета закупки и сужает круг участников.",
                          "Заменить марку и модель на функциональные и технические характеристики; если без модели нельзя (совместимость), добавить «(или эквивалент)» и обосновать.",
                          "high", ["223-FZ-3-6.1-2", "223-FZ-3-6.1-3"], False, "rule:brand", 0.8))
    return out


def check_notes(doc: ParsedDoc) -> list[Finding]:
    out = []
    for b in doc.blocks:
        quotes = []
        instruction = INSTRUCTION_RE.search(b.text)
        for m in NOTE_RE.finditer(b.text):
            if instruction:
                break
            q = _sentence_around(b.text, m.start(), m.end())
            if q in quotes:
                continue
            quotes.append(q)
            out.append(_f(b, q, "Лишнее в ТЗ",
                          "Фрагмент похож на рабочую пометку автора ТЗ (референс, замена «вместо», ссылка на бюджет), а не на требование к товару. "
                          "Такие пометки раскрывают ориентир на конкретный товар и не проверяются при приёмке.",
                          "Удалить пометку; если за ней стоит требование, переписать его как измеримую характеристику.",
                          "medium", ["223-FZ-3-6.1-1"], False, "rule:note", 0.75))
        standalone = doc.fields.get("заголовок", "").lower().lstrip().startswith("техническое задание")
        p = PROC_RE.search(b.text) if standalone else None
        if p:
            out.append(_f(b, _sentence_around(b.text, p.start(), p.end()), "Лишнее в ТЗ",
                          "Порядок подачи заявки и меры национального режима устанавливаются документацией о закупке, а не техническим заданием. "
                          "Дубль в ТЗ расходится с документацией при первой же правке одного из документов.",
                          "Удалить фрагмент из ТЗ, требование оставить только в документации о закупке.",
                          "low", ["223-FZ-4-10-1"], True, "rule:note", 0.6))
        procedural = re.search(r"заявк|аукцион|конкурс|обеспечени|порядок оплат|форма, сроки|информационн\w+ карт|участник\w* закупки", b.section + " " + b.text[:120], re.I)
        v = None if procedural else next((x for x in VAGUE_RE.finditer(b.text) if not _template_context(b.text, x)
                                          and not (x.group(0).lower().startswith("и т") and b.kind != "row")
                                          and not (b.kind != "row" and re.match(r"при необходимости|по согласованию с заказчиком|надлежащ", x.group(0), re.I))), None)
        if v:
            guarantee = bool(GUARANTEE_RE.search(b.text + " " + b.section))
            out.append(_f(b, _sentence_around(b.text, v.start(), v.end()),
                          "Гарантийные условия" if guarantee else "Некорректная характеристика",
                          f"Формулировка «{v.group(0)}» не задаёт проверяемого значения: участник не поймёт, что именно поставить, а заказчик не сможет принять или отклонить результат.",
                          "Заменить на конкретное значение или срок (например, «не более 30 календарных дней с даты уведомления»).",
                          "medium", ["223-FZ-4-10-1"], True, "rule:vague", 0.6))
    return out


def _template_context(text: str, m) -> bool:
    before = text[max(0, m.start() - 200):m.start()]
    if "(" in before[-60:] and ")" in text[m.end():m.end() + 60] and "(" not in text[m.end():m.end() + 60].split(")")[0]:
        return True
    return bool(re.search(r"включая|в том числе", before)) and m.group(0).lower().startswith("и т")


def check_days(doc: ParsedDoc) -> list[Finding]:
    out = []
    starts = defaultdict(list)
    for b in doc.blocks:
        for m in DAYS_RE.finditer(b.text):
            out.append(_practice(_f(b, m.group(0), "Оформление по внутреннему стандарту",
                          "Срок указан только цифрами; по практике закупщиков заказчика сроки дублируются прописью, чтобы исключить опечатку в цифре.",
                          f"{m.group(1)} ({num_words(int(m.group(1)), m.group(3).lower().startswith('недел'))}) {m.group(2)} {m.group(3)}", "low", ["INTERNAL"], False, "rule:days", 0.8), "days.in_words"))
        for m in re.finditer(r"с (момента|даты|дня) (заключения|подписания)", b.text, re.I):
            starts[m.group(1).lower()].append((b, m))
    if len(starts) > 1:
        main = "даты" if "даты" in starts else max(starts, key=lambda k: len(starts[k]))
        for key, items in starts.items():
            if key == main:
                continue
            for b, m in items:
                out.append(_practice(_f(b, m.group(0), "Оформление по внутреннему стандарту",
                              f"Начало сроков в документе сформулировано по-разному («с {main}» и «с {key}»). Разные формулировки дают разные даты отсчёта и повод для спора.",
                              f"Заменить «{m.group(0)}» на «с {main} {m.group(2).lower()}», как в остальных сроках документа.", "low", ["INTERNAL"], False, "rule:start", 0.7), "days.start_wording"))
    return out


def _gost_evidence(st: dict) -> dict:
    ev = {"статус по каталогу": f"{st['designation']}: {st.get('status') or '?'}", "источник статуса": st.get("source") or "",
          "ссылка для проверки": st.get("check_url") or st.get("source_url") or "", "статус проверен": refdata.msk(st.get("checked_at"), True)}
    if st.get("replaced_by"):
        ev["заменяющий стандарт"] = st["replaced_by"]
    if st.get("date_end"):
        ev["дата прекращения действия"] = st["date_end"]
    if st.get("stale"):
        ev["сведения о статусе устарели"] = True
    lists = refdata.tr_lists_for(st["designation"])
    if lists:
        ev["в перечнях к техрегламентам"] = [f"{x['tr']}, перечень {x['list_id']}: {x['status_text'] or 'статус не указан'}" for x in lists[:3]]
    return ev


def _gost_inactive(b: Block, ref: str, st: dict, missing_r: bool = False, extra_r: bool = False) -> Finding:
    word = INACTIVE_WORDS.get(st["status"], st["status"].lower())
    when = f" с {st['date_end']}" if st.get("date_end") else ""
    repl = st.get("replaced_by")
    head = (f"В тексте указан «ГОСТ» без «Р»; по номеру это {st['designation']}, и он {word}{when}" if missing_r
            else f"Национального стандарта {ref} нет; под этим номером есть межгосударственный {st['designation']}, и он {word}{when}" if extra_r
            else f"Стандарт {ref} {word}{when}")
    expl = (head + " по сведениям Росстандарта" + (f", вместо него действует {repl}" if repl else "") +
            ". Ссылка на недействующий стандарт не задаёт проверяемых требований: участник и приёмка будут опираться на разные документы.")
    fix = (f"Заменить ссылку на {repl}, сверив требования ТЗ с действующим стандартом." if repl
           else "Удалить ссылку или указать действующий стандарт на тот же предмет.")
    f = _f(b, ref, "Стандарты (ГОСТ, поверка)", expl, fix, "high", ["ROSSTANDART"], False, "rule:gost", 0.9)
    f.evidence.update(_gost_evidence(st))
    return f


def _grouped(items: list, single, many) -> list[Finding]:
    """Одно замечание на список однотипных ссылок вместо замечания на каждую; остальные места в also_in."""
    if not items:
        return []
    if len(items) == 1:
        return [single(*items[0])]
    f = many(items)
    f.also_in = list(dict.fromkeys(b.id for b, *_ in items[1:] if b.id != items[0][0].id))
    return [f]


def check_gost(doc: ParsedDoc) -> list[Finding]:
    out = []
    seen = set()
    old_refs, snip_refs = [], []
    for b in doc.blocks:
        for m in GOST_RE.finditer(b.text):
            kind, num, year = m.group(1).upper(), m.group(2), m.group(3)
            ref = m.group(0)
            if ref in seen:
                continue
            seen.add(ref)
            try:
                n = int(num.split(".")[0])
            except ValueError:
                n = 0
            if kind == "ГОСТ" and n >= 50000 and len(num) == 5:
                alt = refdata.gost_status(f"ГОСТ Р {num}-{year}")
                if alt and alt["inactive"]:
                    out.append(_gost_inactive(b, ref, alt, missing_r=True))
                    continue
                expl = f"Номер {num} относится к диапазону национальных стандартов ГОСТ Р, а в тексте указан «ГОСТ» без «Р»: ссылка может указывать на несуществующий документ."
                if alt:
                    expl += f" В каталоге Росстандарта под этим номером есть {alt['designation']}" + (f" «{alt['title'][:90]}»" if alt.get("title") else "") + f", статус «{alt['status']}»."
                f = _f(b, ref, "Стандарты (ГОСТ, поверка)", expl,
                       f"ГОСТ Р {num}-{year}" + ("." if alt else " (проверить по сведениям Росстандарта)."), "medium", ["ROSSTANDART"],
                       not alt, "rule:gost", 0.85 if alt else 0.75)
                if alt:
                    f.evidence.update(_gost_evidence(alt))
                out.append(f)
            elif kind == "СНИП":
                snip_refs.append((b, ref))
            else:
                if len(year) == 3:
                    tail = re.match(r"\s*[-–]\s*(\d{4})(?!\d)", b.text[m.end():])
                    if not tail:
                        continue
                    num, year = f"{num}-{year}", tail.group(1)
                    ref = b.text[m.start():m.end() + tail.end()]
                st = refdata.gost_status(f"{kind} {num}-{year}")
                if kind == "ГОСТ Р" and not st:
                    alt = refdata.gost_status(f"ГОСТ {num}-{year}")
                    if alt and alt["inactive"]:
                        out.append(_gost_inactive(b, ref, alt, extra_r=True))
                        continue
                    if alt:
                        f = _f(b, ref, "Стандарты (ГОСТ, поверка)",
                               f"Национального стандарта {ref} нет; под этим номером есть межгосударственный {alt['designation']}"
                               + (f" «{alt['title'][:90]}»" if alt.get("title") else "") + f", статус «{alt['status']}». Лишняя «Р» в обозначении.",
                               f"{alt['designation']}.", "medium", ["ROSSTANDART"], False, "rule:gost", 0.85)
                        f.evidence.update(_gost_evidence(alt))
                        out.append(f)
                        continue
                if st and st["inactive"]:
                    out.append(_gost_inactive(b, ref, st))
                    continue
                if st and st.get("status") == "Действует":
                    continue
                y = int(year) if len(year) == 4 else 1900 + int(year) if int(year) > 30 else 2000 + int(year)
                if y < 2012:
                    old_refs.append((b, ref, y))
        gm = re.search(r"действующ\w+ (?:нормативн\w+ )?(?:ГОСТ|СНиП|норм)[^.;]{0,120}", b.text)
        if gm and (re.search(r"указанн\w+\s+в\s+(?:пункт|раздел|приложени|таблиц|настоящ)|перечислен", gm.group(0), re.I)
                   or re.search(r"в\s+силу\s+$", b.text[max(0, gm.start() - 10):gm.start()], re.I)):
            gm = None
        if gm and "rule:gost-generic" not in seen:
            seen.add("rule:gost-generic")
            m = re.search(r"действующ\w+ (?:нормативн\w+ )?(?:ГОСТ|СНиП|норм)[^.;]{0,120}", b.text)
            out.append(_f(b, m.group(0), "Стандарты (ГОСТ, поверка)",
                          "Общая ссылка на «действующие ГОСТ, СНиП…» без перечня документов: исполнитель не знает, какие именно требования обязательны, при приёмке это не проверить.",
                          "Перечислить конкретные стандарты и своды правил, обязательные для этого предмета закупки.", "low", ["223-FZ-4-10-1"], True, "rule:gost", 0.5))
    out += _grouped(snip_refs,
                    lambda b, ref: _f(b, ref, "Стандарты (ГОСТ, поверка)",
                                      "СНиП в большинстве актуализированы сводами правил (СП); ссылка на СНиП может указывать на недействующий документ.",
                                      "Указать актуальный свод правил вместо СНиП после проверки по сведениям Росстандарта.", "low", ["ROSSTANDART"], True, "rule:gost", 0.6),
                    lambda items: _f(items[0][0], items[0][1], "Стандарты (ГОСТ, поверка)",
                                     f"Ссылки на СНиП в документе ({len(items)}): " + ", ".join(dict.fromkeys(r for _, r in items)) +
                                     ". СНиП в большинстве актуализированы сводами правил (СП); ссылки могут указывать на недействующие документы.",
                                     "Для каждого СНиП указать актуальный свод правил после проверки по сведениям Росстандарта.", "low", ["ROSSTANDART"], True, "rule:gost", 0.6))
    out += _grouped(old_refs,
                    lambda b, ref, y: _f(b, ref, "Стандарты (ГОСТ, поверка)",
                                         f"Стандарт {ref} принят в {y} г.; за это время он мог быть отменён или заменён. Автоматически действие стандарта не подтверждено.",
                                         "Проверить действие стандарта по сведениям Росстандарта и при замене указать действующую редакцию.", "low", ["ROSSTANDART"], True, "rule:gost", 0.5),
                    lambda items: _f(items[0][0], items[0][1], "Стандарты (ГОСТ, поверка)",
                                     f"Действие стандартов, принятых до 2012 г. ({len(items)}), автоматически не подтверждено (их нет в локальной копии каталога): "
                                     + ", ".join(dict.fromkeys(r for _, r, _ in items)) + ". Часть из них могла быть отменена или заменена.",
                                     "Проверить действие каждого стандарта по сведениям Росстандарта и при замене указать действующую редакцию.", "low", ["ROSSTANDART"], True, "rule:gost", 0.5))
    return out


def check_si(doc: ParsedDoc) -> list[Finding]:
    out = []
    doc_has_si_rule = re.search(r"поверк|утвержд\w+ типа|аршин", doc.text(), re.I)
    explicit = len(re.findall(r"(?:является|не является) средством измерени", doc.text(), re.I))
    seen = set()
    if SOFTWARE_SUBJECT_RE.search(doc.fields.get("заголовок", "")):
        return out
    for b in doc.blocks:
        if b.kind not in ("row", "para"):
            continue
        m = SI_WORDS.search(b.text)
        if not m:
            continue
        if not re.search(r"при отнесении к СИ", b.text, re.I) and not _si_names_item(b, m):
            continue
        if re.search(r"при отнесении к СИ", b.text, re.I):
            types = _si_lookup(b, m)
            f = _f(b, "при отнесении к СИ", "Стандарты (ГОСТ, поверка)",
                   "Условие «при отнесении к СИ» перекладывает на поставщика решение, нужна ли поверка: одни участники учтут её в цене, другие нет, а приёмка станет спорной."
                   + _si_note(types, True),
                   "Определить заранее по ФГИС «Аршин»: «Товар является средством измерений утверждённого типа, с действующей первичной поверкой» или «Товар не является СИ, поверка не требуется».",
                   "medium", ["102-FZ-9-1", "102-FZ-13-1", "ARSHIN"], False, "rule:si", 0.8)
            f.evidence.update(_si_evidence(types))
            out.append(f)
            continue
        if re.search(r"поверк|утвержд\w+ типа|аршин|не является средством измерени", b.text, re.I):
            continue
        if b.kind == "row" and len(b.text) < 25:
            continue
        name = (b.cells[1] if b.kind == "row" and len(b.cells) > 1 else m.group(0)).lower()[:40]
        if explicit >= 3 or name in seen:
            continue
        seen.add(name)
        types = _si_lookup(b, m)
        f = _f(b, _sentence_around(b.text, m.start(), m.end()), "Стандарты (ГОСТ, поверка)",
               f"Позиция похожа на средство измерений («{m.group(0)}»), но не сказано, является ли она СИ утверждённого типа и нужна ли поверка."
               + ("" if doc_has_si_rule else " Во всём документе требований к поверке нет.") + _si_note(types),
               "Проверить тип в ФГИС «Аршин» и прямо указать: СИ утверждённого типа с действующей поверкой либо не СИ, поверка не требуется.",
               "medium", ["102-FZ-9-1", "102-FZ-13-1", "ARSHIN"], True, "rule:si", 0.6 if types else 0.55)
        f.evidence.update(_si_evidence(types))
        out.append(f)
    return out


SI_PREP_RE = re.compile(r"(?:^|\s)(?:с|со|от|для|к|ко|из|без|в|во|на|по|при|под|через|и)\s+(?:\w+\s+)?$", re.I)


def _si_names_item(b: Block, m) -> bool:
    """Слово средства измерений называет позицию: стоит в начале ячейки-наименования или абзаца, а не внутри фразы после предлога."""
    if INSTRUCTION_RE.search(b.text):
        return False
    cells = b.cells if b.kind == "row" else [b.text]
    for c in cells:
        for part in [p for item in _items(c) for p in re.split(r"\s*;\s*", item)]:
            mm = SI_WORDS.search(part)
            if mm and mm.start() <= 40 and not SI_PREP_RE.search(part[max(0, mm.start() - 25):mm.start()]) \
                    and not (SI_ATTR_RE.search(part[:mm.start()]) and re.match(r"\w*\s*:", part[mm.end():])):
                return True
    return False


def _si_lookup(b: Block, m) -> list[dict]:
    if b.kind != "row" or len(b.cells) < 2:
        return []
    anchor = m.group(0)
    name = b.cells[1]
    query = name if anchor.lower()[:5] in name.lower() else b.text[m.start():m.start() + 120]
    return refdata.si_type(query, anchor)


def _si_date(iso: str | None) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if iso and len(iso) >= 10 else ""


def _si_title(t: str, limit: int = 80) -> str:
    return t if len(t) <= limit else t[:limit].rsplit(" ", 1)[0] + "…"


def _si_note(types: list[dict], conditional: bool = False) -> str:
    if not types:
        return ""
    t = types[0]
    today = datetime.now().strftime("%Y-%m-%d")
    valid = [x for x in types if not x.get("valid_to") or x["valid_to"] >= today] or types
    examples = "; ".join(f"№ {x['number']} «{_si_title(x['title'])}»" + (f" ({_si_title(x['notation'], 40)})" if x.get("notation") else "")
                         + (f", свидетельство до {_si_date(x['valid_to'])}" if x.get("valid_to") else "") for x in valid[:2])
    head = (" По ФГИС «Аршин» такие приборы выпускаются как СИ утверждённого типа" if conditional
            else " В ФГИС «Аршин» есть утверждённые типы СИ с таким наименованием")
    return f"{head}: по запросу «{t.get('query') or t['search'].strip('*')}» найдено {t['total']}, например {examples}."


def _si_evidence(types: list[dict]) -> dict:
    if not types:
        return {}
    t = types[0]
    ev = {"ФГИС «Аршин»": f"по запросу «{t.get('query') or t['search'].strip('*')}» найдено утверждённых типов СИ: {t['total']}",
          "типы СИ из реестра": [f"№ {x['number']} «{x['title']}»" + (f", {x['notation']}" if x.get("notation") else "")
                                 + (f", свидетельство до {_si_date(x['valid_to'])}" if x.get("valid_to") else "")
                                 + (f", МПИ {x['mpi_months']} мес." if x.get("mpi_months") else "") for x in types],
          "реестр проверен": refdata.msk(t.get("checked_at"), True)}
    if t.get("url"):
        ev["ссылка на тип в реестре"] = t["url"]
    if t.get("local"):
        ev["сведения из локальной копии реестра"] = True
    return ev


def _item_name(b: Block) -> str:
    cell = next((c for c in b.cells if c.strip() and not re.fullmatch(r"[\d.,\s]+", c) and len(c) < 200), "")
    return re.split(r"\s+(?:Референс|Страна происхождения|Изготовитель)", cell, maxsplit=1, flags=re.I)[0].strip()


def check_okpd(doc: ParsedDoc) -> list[Finding]:
    out = []
    title = doc.fields.get("заголовок", "").lower()
    is_goods = "поставк" in title
    is_works = "работ" in title and not is_goods
    generic_seen = {}
    for b in doc.blocks:
        codes_here = OKPD_RE.findall(b.text)
        if len(codes_here) >= 3:
            continue
        for m in OKPD_RE.finditer(b.text):
            code = m.group(0)
            ctx = (b.text + " " + b.section).lower()
            whole_cell = any(c.strip() == code for c in b.cells)
            if not whole_cell and "окпд" not in ctx:
                continue
            item = _item_name(b)
            kit = bool(re.match(r"\s*(?:комплект(?!аци)|набор|стенд|комплекс|измерительн\w+ комплект)", item, re.I)) or any(
                re.fullmatch(r"компл(?:\.|ект\w{0,2})?", c.strip().lower()) for c in b.cells)
            verdict = okpd_dict.assess(item, code) if item and len(code) == 12 else None
            v = verdict["verdict"] if verdict else None
            sug = "; ".join(f"{c} «{n}»" for c, n, *_ in (verdict or {}).get("suggest", [])[:2])
            nm = (verdict or {}).get("name") or okpd_dict.name(code) or ""
            ver = okpd_dict.version()
            if v == "missing":
                out.append(_f(b, code, "Неверный или общий код ОКПД 2",
                              f"Кода {code} нет в классификаторе ({ver}): позицию нельзя однозначно отнести к виду продукции и проверить меры национального режима.",
                              "Указать действующий код ОКПД 2" + (f"; по наименованию «{item[:60]}» подходят: {sug}." if sug else "."),
                              "high", ["OK-034-2014"], False, "rule:okpd", 0.85))
                continue
            if v == "mismatch":
                out.append(_f(b, code, "Неверный или общий код ОКПД 2",
                              f"Код {code} в классификаторе означает «{nm[:90]}», а позиция — «{item[:70]}»." +
                              (" Кроме того, под одним кодом объединены разные товары комплекта." if kit else ""),
                              (f"Разбить комплект на позиции и каждой указать свой код; по наименованию ближе всего: {sug}." if kit else
                               f"Указать код по виду товара, ближе всего по наименованию: {sug}. Проверить по ОК 034-2014."),
                              "medium", ["OK-034-2014"], True, "rule:okpd", 0.75))
                continue
            if v == "generic":
                out.append(_f(b, code, "Неверный или общий код ОКПД 2",
                              f"Код {code} обобщающий («{nm[:80]}»), а для позиции «{item[:60]}» в классификаторе есть точный код: {sug}.",
                              "Заменить код «прочие» на точный код вида продукции" + (" и разбить комплект на позиции." if kit else "."),
                              "medium", ["OK-034-2014"], True, "rule:okpd", 0.75))
                continue
            generic_name = bool(re.search(r"прочи|не включенн", nm, re.I)) if nm else (
                code.endswith(".190") or code.endswith(".000") or code.endswith(".990") or code.endswith(".900"))
            if generic_name:
                if not kit:
                    if code in generic_seen and getattr(generic_seen[code], "_n", 1) >= 3:
                        generic_seen[code].explanation = re.sub(r"Встречается в \d+ позициях\. ", "", generic_seen[code].explanation)
                        generic_seen[code]._n = getattr(generic_seen[code], "_n", 1) + 1
                        generic_seen[code].explanation = f"Встречается в {generic_seen[code]._n} позициях. " + generic_seen[code].explanation
                        continue
                out.append(_f(b, code, "Неверный или общий код ОКПД 2",
                              f"Код {code} обобщающий («прочие»)." + (" Под ним объединены разные товары комплекта, у каждого из которых есть свой точный код." if kit else " Проверить по классификатору, нет ли точного кода для этого товара."),
                              "Разбить комплект на позиции и указать для каждой точный код ОКПД 2 по ОК 034-2014." if kit else "Подобрать точный код ОКПД 2 для товара по ОК 034-2014; код «прочие» оставить, только если точного нет.",
                              "medium" if kit else "low", ["OK-034-2014"], True, "rule:okpd", 0.65 if kit else 0.45))
                if not kit:
                    prev = generic_seen.get(code)
                    if prev is not None:
                        out[-1]._n = getattr(prev, "_n", 1) + 1
                    generic_seen[code] = out[-1] if prev is None or getattr(prev, "_n", 1) < 3 else prev
            if len(code) < 12 and b.kind == "row":
                out.append(_f(b, code, "Неверный или общий код ОКПД 2",
                              f"Код {code} указан не до уровня вида продукции (нужно 9 цифр, формат XX.XX.XX.XXX).",
                              "Указать полный код ОКПД 2.", "low", ["OK-034-2014"], True, "rule:okpd", 0.6))
            sect = int(code[:2])
            if is_goods and 41 <= sect <= 43:
                out.append(_f(b, code, "Неверный или общий код ОКПД 2",
                              f"Код {code} из раздела строительных работ, а предмет закупки поставка товара.",
                              "Проверить соответствие кода предмету закупки.", "medium", ["OK-034-2014"], True, "rule:okpd", 0.6))
            if is_works and sect < 33 and "работ" in b.text.lower():
                out.append(_f(b, code, "Неверный или общий код ОКПД 2",
                              f"Код {code} из разделов товаров, а строка описывает работы.",
                              "Проверить соответствие кода предмету закупки.", "medium", ["OK-034-2014"], True, "rule:okpd", 0.55))
    if not doc.fields.get("okpd2"):
        b = doc.blocks[0]
        out.append(_f(b, b.text[:120], "Неполнота или комплектность",
                      "В документе не найден код ОКПД 2: без него нельзя проверить применимость национального режима и корректность описания.",
                      "Указать код ОКПД 2 для каждой позиции.", "medium", ["OK-034-2014"], True, "rule:okpd", 0.5))
    return out


GENERIC_TRIAD_RE = re.compile(r"товар\w*\s*,\s*работ\w*\s*,?\s*(?:и\s+|или\s+|\(или\)\s+)?услуг\w*|работ\w*\s*,?\s*(?:и\s+|или\s+)?услуг\w*", re.I)
TERM_RE = re.compile(r"срок\w*\s+(?:поставки|выполнения|оказания|исполнения|действия)|не позднее|в течение \d+|период\w*\s+(?:поставки|выполнения|оказания)|"
                     r"(?:с|по|до)\s+\d{1,2}\.\d{1,2}\.(?:20)?\d{2}|(?:с|по|до)\s+\d{1,2}\s+[а-я]+\s+20\d\d|график\w*\s+(?:поставк|оказани|выполнени)|"
                     r"ежедневно|еженедельно|ежемесячно|круглосуточно", re.I)


SUBJECT_KIND_RE = {
    "goods": re.compile(r"поставк\w*\s+(?!(?:товар\w*\s*,\s*)?работ)|на\s+поставку|приобретени\w*\s+(?:товар|оборуд|\w+)|закупк\w*\s+(?:товар|оборуд)", re.I),
    "works": re.compile(r"выполнени\w*\s+(?:[\w-]+,?\s+){0,4}работ|работ\w*\s+по\s+(?:ремонт|строительств|монтаж|замен|капитальн|текущ)|(?:капитальн|текущ)\w*\s+ремонт", re.I),
    "services": re.compile(r"оказани\w*\s+(?:\w+\s+){0,3}услуг|(?<![а-яё])услуг[аи]?\s+(?:по|на)\s", re.I),
}
SUBJECT_ANCHOR_RE = re.compile(r"(?:предмет|объект)\w*\s+(?:закупки|договора)|наименовани\w*\s+(?:предмета|объекта)|техническое\s+задание|описание\s+объекта", re.I)


def subject_kinds(doc: ParsedDoc) -> set:
    """Вид предмета закупки: товары, работы или услуги по первому упоминанию после «предмет / объект закупки» или в начале документа.
    Общие формулы «товаров, работ, услуг» не считаются."""
    blocks = doc.blocks[:300]
    start = next((i for i, b in enumerate(blocks) if SUBJECT_ANCHOR_RE.search(b.text)), 0)
    head = GENERIC_TRIAD_RE.sub(" ", " ".join(b.text for b in blocks[start:start + 8]))
    hits = sorted((m.start(), k) for k, rx in SUBJECT_KIND_RE.items() for m in [rx.search(head)] if m)
    if not hits:
        head = GENERIC_TRIAD_RE.sub(" ", doc.fields.get("заголовок", ""))
        hits = sorted((m.start(), k) for k, rx in SUBJECT_KIND_RE.items() for m in [rx.search(head)] if m)
    return {hits[0][1]} if hits else set()


def check_completeness(doc: ParsedDoc) -> list[Finding]:
    out = []
    text = doc.text()
    first = doc.blocks[0]
    services_only = subject_kinds(doc) == {"services"}
    if services_only:
        pass
    elif not GUARANTEE_RE.search(text) and not SHELF_LIFE_RE.search(text):
        out.append(_f(first, first.text[:120], "Гарантийные условия",
                      "В ТЗ нет требований к гарантии: срок, порядок исчисления и срок устранения недостатков не определены.",
                      "Добавить раздел о гарантии: срок (не менее N месяцев), с какого документа он исчисляется, срок устранения недостатков.",
                      "high", ["GK-470", "GK-471"], False, "rule:complete", 0.7))
    else:
        g = [b for b in doc.blocks if GUARANTEE_RE.search(NOT_WARRANTY_RE.sub(" ", b.text))]
        term = lambda t: MONTHS_RE.search(t) or WARRANTY_TERM_RE.search(t)
        if g and not any(term(b.text) for b in g) and not any(term(o.text) for b in g for o in _neighbours(doc, b)):
            out.append(_f(g[0], g[0].text[:200], "Гарантийные условия",
                          "Гарантия упомянута, но срок гарантии не указан.", "Указать гарантийный срок в месяцах и начало его исчисления.",
                          "medium", ["GK-470", "GK-471"], False, "rule:complete", 0.65))
    if not re.search(r"адрес|место (?:поставки|выполнения|оказания)|г\.\s?[А-ЯЁ]", text):
        out.append(_f(first, first.text[:120], "Неполнота или комплектность",
                      "Не найдено место поставки или выполнения работ.", "Указать адрес поставки или выполнения работ.",
                      "high", ["223-FZ-4-10-4"], False, "rule:complete", 0.6))
    if not TERM_RE.search(text):
        out.append(_f(first, first.text[:120], "Неполнота или комплектность",
                      "Не найден срок поставки или выполнения работ.", "Указать срок поставки (выполнения) в календарных днях с даты заключения договора или конкретной датой.",
                      "high", ["223-FZ-4-10-4"], False, "rule:complete", 0.6))
    for b in doc.blocks:
        m = re.search(r"(?:должен быть|быть)\s+новым[^.;]{0,160}", b.text, re.I)
        if m and not re.search(r"не ранее\s+\d{4}|год\w*\s+(?:выпуска|изготовления)|изготовлен\w*\s+не ранее", text, re.I):
            out.append(_practice(_f(b, m.group(0), "Неполнота или комплектность",
                          "Требование «новый» не ограничено годом изготовления: можно поставить новый, но давно выпущенный товар со склада.",
                          "Добавить: «изготовленным не ранее YYYY года».", "low", ["223-FZ-4-10-1"], True, "rule:complete", 0.5), "complete.new_no_year"))
        for m in re.finditer(r"(пусконалад\w*|инструктаж\w*|монтаж\w*|обучени\w*)[^.;]{0,80}?в случаях,?\s+(?:указанных|предусмотренных)[^.;]{0,60}", b.text, re.I):
            stem = m.group(1)[:7].lower()
            if not any(o.id != b.id and stem in o.text.lower() for o in doc.blocks):
                out.append(_f(b, m.group(0), "Неполнота или комплектность",
                              f"Ссылка «в случаях, указанных…» никуда не ведёт: ни в одной позиции не сказано, где нужны {m.group(1).lower()}.",
                              f"Указать позиции, для которых требуются {m.group(1).lower()}, например «(для поз. 2)».", "medium", ["223-FZ-4-10-1"], True, "rule:complete", 0.6))
        m = re.search(r"[^.;]{0,80}разгрузк\w*[^.;]{0,80}", b.text, re.I)
        if m and re.search(r"достав|поставк", b.text, re.I) and not re.search(r"подъ[её]м|занос", text, re.I):
            out.append(_practice(_f(b, m.group(0).strip(), "Неполнота или комплектность",
                          "Условия доставки названы разгрузкой, но не сказано, кто поднимает и заносит товар в помещение: спор при приёмке тяжёлого оборудования.",
                          "Указать: «доставка, погрузочно-разгрузочные работы, подъём и занос Товара в помещение — силами и за счёт Поставщика».",
                          "low", ["223-FZ-4-10-4"], True, "rule:complete", 0.5), "complete.unloading"))
    for b in doc.blocks:
        if b.kind != "row" or len(b.cells) < 3:
            continue
        name = b.cells[1] if len(b.cells) > 1 else ""
        described = name and len(name) > 8 and any(
            o.id != b.id and name[:25] in o.text and re.search(r"шт|количеств|состав|комплект поставки", o.text, re.I) for o in doc.blocks)
        if (re.search(r"^(?:комплект(?!н)|набор)", name, re.I) or any(re.fullmatch(r"компл(?:\.|ект\w{0,2})?", c.strip().lower()) for c in b.cells)) \
                and not described and not re.search(r"в составе|состав:|:\s*\w+.*[,;]", b.text) \
                and not re.search(r"комплект\w*\s+[а-яёa-z][^,;]{1,40}?\s(?:\+|и)\s+[а-яёa-z]", name, re.I):
            out.append(_f(b, b.cells[1] if len(b.cells) > 1 and b.cells[1] in b.text else b.text[:150], "Неполнота или комплектность",
                          "Позиция «комплект» без перечня входящих предметов и их количества: непонятно, что именно поставить и как принять.",
                          "Раскрыть состав комплекта: перечень предметов, количество каждого и ключевые характеристики.",
                          "medium", ["GK-478", "223-FZ-4-10-1"], True, "rule:complete", 0.55))
    return out


def _neighbours(doc: ParsedDoc, b: Block, n: int = 2) -> list[Block]:
    i = next((k for k, o in enumerate(doc.blocks) if o.id == b.id), None)
    return [] if i is None else doc.blocks[max(0, i - n):i] + doc.blocks[i + 1:i + 1 + n]


def check_contradictions(doc: ParsedDoc) -> list[Finding]:
    out = []
    title = doc.fields.get("заголовок", "").lower()
    for a, bword in OPPOSITES:
        for x, y in ((a, bword), (bword, a)):
            if re.search(x, title) and not re.search(y.replace(r"\w* работ", r"\w*\s+(?:[\w-]+,?\s+){0,4}работ"), title):
                for b in doc.blocks:
                    m = re.search(y + r"\w*", b.text, re.I)
                    if m and b.kind == "row" and not re.search(x, b.text, re.I):
                        tw = re.search(x + r"\w*", title).group(0)
                        out.append(_f(b, _sentence_around(b.text, m.start(), m.end()), "Противоречие",
                                      f"В предмете закупки указано «{tw}», а в этой строке «{m.group(0)}»: части документа описывают разные объекты.",
                                      "Привести строку в соответствие с предметом закупки или исправить предмет.", "medium" if "поставк" in (x, y) else "high", ["223-FZ-3-6.1-1"], True, "rule:contra", 0.7))
    params = defaultdict(list)
    for b in doc.blocks:
        if b.kind == "row" and len(b.cells) >= 2:
            name = b.cells[-2] if len(b.cells) >= 3 and not re.search(r"\d", b.cells[-2]) else b.cells[0]
            if len(name) > 60:
                continue
            stem = re.match(r"[А-ЯЁа-яё]{5,}", name.strip())
            um = re.search(r",\s*([а-яёa-z/]{1,5})\.?\s*$", name.strip(), re.I) or re.search(r"\d\s*([а-яёa-z/]{1,5})\.?\s*$", b.cells[-1], re.I)
            nums = re.findall(r"\d+(?:[.,]\d+)?", b.cells[-1])
            if stem and nums and um and len(nums) <= 2:
                params[stem.group(0)[:7].lower()].append((b, nums[0], name, um.group(1).lower()))
    dims = defaultdict(list)
    for b in doc.blocks:
        for m in re.finditer(r"(?<![\d.,])(\d{3,4})\s*[хx×]\s*(\d{3,4})(?![\d])", b.text):
            dims[m.group(1)].append((b, m.group(2), m.group(0)))
    for first, items in dims.items():
        seconds = {x[1] for x in items}
        if len(seconds) < 2:
            continue
        base = items[0]
        for b, sec, frag in items[1:]:
            if sec != base[1] and b.id != base[0].id:
                out.append(_f(b, frag, "Противоречие",
                              f"Размер {frag} не совпадает с {base[2]} в {base[0].location()}: при одной ширине {first} мм указаны разные высоты.",
                              "Сверить размеры проёма или изделия во всех частях документа и оставить одно значение.",
                              "medium", ["223-FZ-3-6.1-1"], True, "rule:contra", 0.55))
                out.append(_f(base[0], base[2], "Противоречие",
                              f"Размер {base[2]} не совпадает с {frag} в {b.location()}.",
                              "Сверить размеры во всех частях документа и оставить одно значение.",
                              "medium", ["223-FZ-3-6.1-1"], True, "rule:contra", 0.55))
    for stem, rows in params.items():
        for b in doc.blocks:
            low = b.text.lower()
            for m in re.finditer(stem + r"[а-яё]*[^.;|\d]{0,40}?(\d+(?:[.,]\d+)?)", low):
                val = m.group(1)
                if re.match(r"\s*[-–]\s*\d", low[m.end():]) or NOTE_RE.search(b.text):
                    continue
                for rb, rv, name, unit in rows:
                    if not low[m.end():m.end() + 8].lstrip().startswith(unit):
                        continue
                    if rb.id != b.id and rv != val and b.kind != "row":
                        out.append(_f(rb, rb.text[:200], "Противоречие",
                                      f"Параметр «{name}» в таблице = {rv}, а в {b.location()} указано {val}.",
                                      "Сверить значения и оставить одно, согласованное с предметом закупки.", "high", ["223-FZ-3-6.1-1"], True, "rule:contra", 0.6))
    return out

QTY_WORDS = re.compile(r"сопротивлени|частот|[её]мкост|напряжени|\bток\w*|температур|давлени|мощност|влажност|момент|скорост|освещ[её]нност", re.I)
ACCURACY_RE = re.compile(r"погрешн|класс\w* точност|точност|неопредел[её]нност", re.I)
RANGE_RE = re.compile(r"\d[\d,]*\s*(?:[–-]|до)\s*\d")
ACCESSORY_RE = re.compile(r"\b(?:с|со)\s+(?:кабел\w+|стойк\w+|объектив\w+|штатив\w+|подставк\w+)|калибровочн\w+ доск\w+|подставк\w+[^;]*в комплекте", re.I)
KIT_RE = re.compile(r"(?:комплект\w*(?: поставки)?|в комплекте)\s*[:—–-]\s*([^;•]+)", re.I)
ONE_SIDED_RE = re.compile(r"диапазон\w*[^;,.]{0,25}?не менее\s*\d", re.I)
MIXED_RE = re.compile(r"\bдо\s*\d[^;]{0,40}\bи\s+не менее\s*\d|не менее\s*\d[^;]{0,40}\bи\s+до\s*\d", re.I)
COMPAT_RE = re.compile(r"совместим\w*\s+с\s+(?:регистратор|имеющ|оборудовани\w* Заказчика|систем\w* Заказчика|существующ)[^;,.]{0,50}", re.I)
MEMORY_RE = re.compile(r"памят\w*(?:\s+результат\w*)?|выход\w*\s+(?:данных|на ПК)", re.I)


PROFILES_PATH = os.environ.get("TZCHECK_PROFILES", os.path.join(os.path.dirname(__file__), "data", "profiles.json"))


def load_profiles(path: str = PROFILES_PATH) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for pr in data.get("profiles", []):
        out.append({"name": pr["name"], "trigger": re.compile(pr["trigger"], re.I),
                    "required": {k: re.compile(v, re.I) for k, v in pr["required"].items()}})
    return out


PROFILES = load_profiles()
QTY_SUFFIX_RE = re.compile(r"\s*[—–-]\s*(?:по\s+)?\d+\s*(?:шт|компл)\w*\.?")


def _profile_for(text: str):
    best = None
    for pr in PROFILES:
        m = pr["trigger"].search(text)
        if m and (best is None or m.start() < best[1]):
            best = (pr, m.start())
    return best[0] if best else None


# Пункты списка внутри ячейки. В DOCX это «•», после OCR — «·», «▪», «●», «-» или «1)», «а)», а сами пункты склеены в одну строку.
ITEM_SPLIT_RE = re.compile(r"\s*[•·▪●◦]\s*|\s*\n\s*|(?:(?<=^)|(?<=[.;:]\s))\s*(?:\d{1,2}\)|[а-еa-e]\)|[-–])\s+")


def _items(text: str) -> list[str]:
    return [x.strip() for x in ITEM_SPLIT_RE.split(text) if x and x.strip()]


def _segments(b: Block):
    for cell in b.cells or [b.text]:
        for bullet in _items(cell):
            bullet = bullet.strip()
            if not bullet:
                continue
            head = bullet.split(":", 1)[0] if ":" in bullet[:90] else ""
            for seg in re.split(r";\s*", bullet):
                seg = seg.strip(" .")
                if seg:
                    yield bullet, head, seg


def _attribute_form(pr: dict, text: str) -> bool:
    """«Источник питания: 220 В» — параметр другого изделия, а не позиция спецификации."""
    m = pr["trigger"].search(text[:60])
    return bool(m) and bool(re.match(r"[^:;]{0,20}:\s*[\d<>≥≤±~]", text[m.end():]))


def _profile_targets(doc: ParsedDoc, b: Block):
    name = next((c for c in b.cells if c and not re.fullmatch(r"[\d.,\s]+", c)), "")
    pr = _profile_for(name[:60]) if name and len(name) < 150 else None
    if pr is not None and pr["trigger"].search(name[:60]).start() > 3:
        pr = None
    if pr is not None and _attribute_form(pr, name):
        pr = None
    if pr is not None:
        related = [o for o in doc.blocks if name[:25] in o.text]
        scope = " ".join(o.text for o in related)
        if pr["trigger"].search(doc.fields.get("заголовок", "")):
            scope = doc.text()
        chars = [c for c in b.cells if c != name and len(c) > 40]
        quote = chars[0] if chars and len(chars[0]) <= 400 else name
        yield name, pr, scope, quote
        return
    for bullet in dict.fromkeys(x[0] for x in _segments(b)):
        m = None
        for cand in PROFILES:
            mm = cand["trigger"].search(bullet[:60])
            if mm and mm.start() <= 3:
                m = cand
                break
        if m is None or len(bullet) < 12 or re.match(r"комплект поставки|программн", bullet, re.I) or _attribute_form(m, bullet):
            continue
        yield bullet, m, bullet, bullet


def check_positions(doc: ParsedDoc) -> list[Finding]:
    out = []
    seen = set()

    def add(b, quote, cat, expl, fix, sev="medium", review=True, conf=0.55):
        key = (b.id, quote[:60], cat)
        if key in seen or quote not in b.text:
            return
        seen.add(key)
        norms = ["223-FZ-3-6.1-2"] if cat == "Некорректная характеристика" else ["223-FZ-4-10-1"]
        out.append(_f(b, quote, cat, expl, fix, sev, norms, review, "rule:position", conf))

    for b in doc.blocks:
        if b.kind != "row" and not re.search(r"компл|диапазон|измерени", b.text, re.I):
            continue
        for bullet, head, seg in _segments(b):
            words = re.findall(r"[а-яё]{3,}", seg.lower())
            if not re.search(r"\d", seg) and len(QTY_WORDS.findall(seg)) >= 2 and len(seg) < 90 and "," in seg \
                    and len(QTY_WORDS.findall(seg)) / max(len(words), 1) >= 0.6:
                add(b, seg, "Неполнота или комплектность",
                    "Измеряемые величины перечислены без диапазонов и точности: по такому описанию нельзя сравнить предложения и принять товар.",
                    "Для каждой величины указать диапазон измерения и пределы погрешности, например: «сопротивление — до 40 МОм».")
            m = KIT_RE.search(seg)
            if m and m.group(1).count(",") >= 1 and not re.search(r"шт|компл\w*\.|\d", m.group(1)):
                add(b, m.group(0).strip(), "Неполнота или комплектность",
                    "Состав комплекта перечислен без количества каждого предмета и без характеристик принадлежностей.",
                    "Указать комплект поставки построчно: предмет — количество (шт.), для принадлежностей ключевые параметры.")
            am = ACCESSORY_RE.search(seg)
            tail = QTY_SUFFIX_RE.sub("", seg[am.end():am.end() + 40]) if am else ""
            if am and not re.search(r"\d", tail.split(",")[0]) and not re.search(r"\d", seg[max(0, am.start() - 15):am.start()]):
                add(b, seg if len(seg) < 160 else am.group(0), "Неполнота или комплектность",
                    "Принадлежность названа без характеристик: поставщик может поставить любой вариант.",
                    "Указать параметры принадлежности (длина кабеля, высота стойки, фокусное расстояние, размер и т.п.).", "low")
            om = ONE_SIDED_RE.search(seg)
            if om and not re.match(r"[\d,]*\s*[–-]\s*\d", seg[om.end():]):
                add(b, om.group(0), "Некорректная характеристика",
                    "Диапазон задан одной границей «не менее»: непонятно, от какого значения должен начинаться диапазон измерения.",
                    "Задать диапазон двумя границами: «от … до …» с допустимым направлением отклонения.")
            if MIXED_RE.search(seg):
                add(b, seg, "Некорректная характеристика",
                    "В одной характеристике смешаны «до» и «не менее»: неясно, какая граница обязательна для каждого параметра.",
                    "Разделить параметры и для каждого указать одну обязательную границу.")
            cm = COMPAT_RE.search(seg)
            if cm and not re.search(r"\d|USB|RS-?\d|Ethernet|ГОСТ", cm.group(0)):
                add(b, cm.group(0), "Некорректная характеристика",
                    "Требование «совместимый с …» не проверяется при приёмке: не указан интерфейс или сигнал.",
                    "Указать измеримый параметр: тип и диапазон выходного сигнала или интерфейс.")
            mm = MEMORY_RE.search(seg)
            name_cell = b.kind == "row" and len(b.cells) >= 3 and any(c.strip(" .;") == seg for c in b.cells)
            if mm and not name_cell and SI_WORDS.search(b.text) and not re.search(r"оперативн|видеопамят|ОЗУ|\bГБ\b", seg, re.I) and not re.search(r"\d|USB|RS-?\d|Bluetooth|Wi-?Fi|Ethernet", bullet, re.I):
                add(b, seg, "Некорректная характеристика",
                    "Функция названа без параметра: не указан объём памяти или интерфейс передачи данных.",
                    "Указать объём памяти (не менее N измерений) и интерфейс передачи данных (например, USB).", "low")
        profiled = set()
        if b.kind == "row":
            for bullet, pr, scope, quote in _profile_targets(doc, b):
                missing = [k for k, rx in pr["required"].items() if not rx.search(scope)]
                if re.search(r"утвержд\w* типа|является средством измерени", scope, re.I):
                    missing = [k for k in missing if not re.search(r"погрешн|точност", k)]
                profiled.add(bullet)
                if not missing:
                    continue
                add(b, quote, "Неполнота или комплектность",
                    f"Для позиции «{pr['name']}» не указаны характеристики: {', '.join(missing)}. Без них нельзя однозначно сравнить предложения участников и принять товар.",
                    "Добавить: " + "; ".join(f"{k} — значение с допустимой границей (не менее / не более / от … до …)" for k in missing) + ".",
                    "medium", True, 0.55)
        if b.kind == "row" and SI_WORDS.search(b.text) and not re.search(r"утвержд\w* типа|является средством измерени", b.text, re.I):
            for bullet in dict.fromkeys(x[0] for x in _segments(b)):
                if bullet in profiled:
                    continue
                scope = b.text if bullet in b.cells else bullet
                if bullet in b.cells and len(bullet) < 120:
                    scope = " ".join(o.text for o in doc.blocks if bullet[:25] in o.text)
                    if re.search(r"утвержд\w* типа|является средством измерени", scope, re.I):
                        continue
                if SI_WORDS.search(bullet) and RANGE_RE.search(bullet) and not ACCURACY_RE.search(scope) and len(bullet) < 300:
                    add(b, bullet, "Неполнота или комплектность",
                        "Для средства измерения указан только диапазон, нет метрологических характеристик (пределы погрешности или класс точности).",
                        "Добавить пределы допускаемой погрешности (или класс точности) и цену деления для каждого прибора.", "medium", True, 0.5)
    return out


LANG_RE = re.compile(r"(?:документаци\w*|инструкци\w*|паспорт\w*|руководств\w*)[^.]{0,80}?на\s+(?:русском\s+и/или\s+английском|английском|русском\s+или\s+английском)", re.I)
READY_RE = re.compile(r"в\s+срок,?\s+обеспечивающ\w+[^.;]{0,120}", re.I)
SCALE_RE = re.compile(r"дискретност\w*[^.;•]{0,40}?\d[\d,.]*\s*(?:мг|г|кг)\b", re.I)
GENERIC_QUALITY_RE = re.compile(r"соответствовать\s+назначению\s+товара|применимым\s+требованиям\s+законодательства", re.I)
WARRANTY_START_RE = re.compile(r"(?:с\s+даты|с\s+момента)\s+при[её]мки\s+товара(?!\s*(?:по|согласно)\s+акт)|или\s+иного\s+документа,?\s+подтверждающего\s+при[её]мку", re.I)


def check_wording(doc: ParsedDoc) -> list[Finding]:
    out = []
    for b in doc.blocks:
        m = LANG_RE.search(b.text)
        if m:
            out.append(_f(b, _sentence_around(b.text, m.start(), m.end()), "Некорректная характеристика",
                          "Формулировка допускает документацию только на английском языке. Эксплуатационная документация по техрегламентам ЕАЭС "
                          "должна быть на русском, иначе персонал не сможет безопасно эксплуатировать товар, а приёмку нечем подтвердить.",
                          "Указать: «Товар сопровождается эксплуатационной документацией на русском языке».",
                          "medium", ["TR-TS-004-5"], False, "rule:wording", 0.75))
        m = READY_RE.search(b.text)
        if m:
            out.append(_f(b, m.group(0).strip(), "Некорректная характеристика",
                          "Срок «обеспечивающий готовность к приёмке» не определяет дату поставки: непонятно, когда поставщик обязан передать товар.",
                          "Указать срок поставки датой или числом дней: «не позднее 10 декабря 2026 г.».",
                          "medium", ["223-FZ-4-10-4"], False, "rule:wording", 0.7))
        if re.search(r"\bвес(?:ы|ов)\b", b.text + " " + b.section, re.I) and not re.search(r"цен\w+\s+делени", b.text, re.I):
            m = SCALE_RE.search(b.text)
            if m:
                out.append(_f(b, m.group(0).strip(), "Некорректная характеристика",
                              "Для весов указана «дискретность», которой нет среди нормируемых характеристик: по ГОСТ OIML R 76-1 задаются действительная цена деления (d) и поверочный интервал (e). "
                              "Поставщик может предложить весы с мелкой индикацией, но грубой поверочной ценой деления.",
                              "Заменить на «действительная цена деления (d) — не более …; поверочный интервал (e) — не более …; класс точности — …».",
                              "medium", ["GOST-OIML-R76-1"], False, "rule:wording", 0.7))
        m = GENERIC_QUALITY_RE.search(b.text)
        if m:
            out.append(_f(b, _sentence_around(b.text, m.start(), m.end()), "Некорректная характеристика",
                          "Требование «соответствовать назначению и применимым требованиям законодательства» не называет ни одного проверяемого требования: при приёмке на него нельзя сослаться.",
                          "Перечислить конкретные требования (техрегламенты, ГОСТ, документы о соответствии) или удалить общую фразу.",
                          "low", ["223-FZ-4-10-1"], True, "rule:wording", 0.55))
        if GUARANTEE_RE.search(b.text + " " + b.section):
            m = WARRANTY_START_RE.search(b.text)
            if m:
                out.append(_f(b, _sentence_around(b.text, m.start(), m.end()), "Гарантийные условия",
                              "Начало гарантийного срока привязано к неопределённому событию («дата приёмки» или «иной документ»): гарантия может начаться в разные даты, и это повод для спора.",
                              "Привязать начало гарантии к одному документу: «с даты подписания Заказчиком документа о приёмке Товара».",
                              "medium", ["GK-470", "GK-471"], False, "rule:wording", 0.7))
    return out



WORK_KINDS = {"замен": "замена", "ремонт": "ремонт", "монтаж": "монтаж", "демонтаж": "демонтаж", "поставк": "поставка",
              "обслуживан": "обслуживание", "модернизац": "модернизация", "реконструкц": "реконструкция",
              "строительств": "строительство", "уборк": "уборка", "охран": "охрана"}
PART_TITLE_RE = re.compile(r"^\s*(?:спецификаци\w+|ведомост\w+|смет\w+|перечень\s+работ|техническое\s+задание)\s+(?:на|по)\s", re.I)


def _work_kinds(text: str) -> set:
    return {k for k in WORK_KINDS if re.search(r"(?<![а-яё])" + k, text, re.I)}


def check_subject(doc: ParsedDoc) -> list[Finding]:
    out = []
    subject = _work_kinds(doc.fields.get("заголовок", "")[:160])
    if not subject:
        return out
    for b in doc.blocks:
        if len(b.text) > 200 or not PART_TITLE_RE.search(b.text):
            continue
        kinds = _work_kinds(b.text)
        if kinds and not (kinds & subject):
            have = ", ".join(WORK_KINDS[k] for k in sorted(kinds))
            need = ", ".join(WORK_KINDS[k] for k in sorted(subject))
            out.append(_f(b, b.text, "Предмет закупки или наименование",
                          f"В названии части документа вид работ «{have}», а предмет закупки «{need}»: название не совпадает с предметом.",
                          "Назвать часть документа по предмету закупки.", "medium", ["223-FZ-3-6.1-1"], True, "rule:subject", 0.6))
    return out


ALL_RULES = [check_subject, check_wording, check_brands, check_notes, check_days, check_gost, check_si, check_okpd, check_completeness, check_positions, check_contradictions]


def _apply_customer(doc: ParsedDoc, found: list[Finding]) -> list[Finding]:
    prof = customer.detect(doc.text())
    rules = (prof or {}).get("rules", {})
    out, skipped = [], defaultdict(int)
    for f in found:
        key = getattr(f, "_practice", None)
        if key is None:
            out.append(f)
        elif key in rules:
            f.evidence["практика заказчика"] = f"{prof['name']}: {rules[key]} (источник: {prof.get('source', 'профиль заказчика')})"
            out.append(f)
        else:
            skipped[key] += 1
    doc.fields["профиль заказчика"] = prof["name"] if prof else None
    if skipped:
        doc.fields["правила практики заказчика не применены"] = dict(skipped)
    return out


def run_rules(doc: ParsedDoc) -> list[Finding]:
    out = []
    for rule in ALL_RULES:
        try:
            out.extend(rule(doc))
        except Exception as e:
            doc.warnings.append(f"правило {rule.__name__} упало: {e}")
    return _apply_customer(doc, out)
