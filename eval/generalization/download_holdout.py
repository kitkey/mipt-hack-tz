"""Скачивает отложенный набор eis_holdout (30 ТЗ по 223-ФЗ) по ссылкам из data_raw/eis_holdout/manifest.csv.

Сами документы заказчиков в репозиторий не входят: в них контакты сотрудников. Файл ТЗ из архива извлекается
по имени, sha256 сверяется с тем, на котором делался замер (eval/generalization/REPORT.md).
Запуск: python eval/generalization/download_holdout.py [--out data_raw/eis_holdout]
"""
import argparse
import csv
import hashlib
import io
import time
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data_raw" / "eis_holdout" / "manifest.csv"
# файл: (имя внутри архива или None, sha256 файла ТЗ)
FILES = {
    '01_РоссетиЮг_строительство.docx': ('Задание на торги.docx', '08f4db77c16a1cef46aae0bdb9ab24b780846948ab4a44bde7434f81e2636799'),
    '02_ТПлюс_капремонт.docx': ('Прилож.1 ТЗ КР МКП-220_2027.docx', 'c5ee3222b483c9982430c30347bd508799740f19063480288200a39617a6a46d'),
    '03_МиграционныйЦентр_разработка_ПО.pdf': (None, '7b2fd225ffb02f01b9c4078361cc777febaa261908cf74eb85c9ddb7d28468a5'),
    '04_АрхангельскийАвиаотряд_разработка_сайта.docx': (None, '959682a0ba00233ebcdf5cb75a9ab66f0691adcde42103e44805d7a786d9da7d'),
    '05_ЕйскПорт_лицензии_ПО.docx': (None, 'd9d76ebaf3307b391aeaf9919f1f2a77ffce1c1739376a34966a986ec78543aa'),
    '06_ПетербургскийМетрополитен_лицензии_ПО.docx': (None, 'b9e24096426fe0dcc2fdc7f57b89af793201abcccf972fc5b988b1c85ecec66e'),
    '07_ФИПС_консультации_по_ПО.docx': (None, '94f944663243a8dc9ee28048d7c9c7f2563e4a6edcfa757d583aa7aad18ccc1c'),
    '08_ОбщепитСалават_продукты_питания.doc': (None, 'e9fe4386fd0b49ccbf5fa2b53a841eb61021772ccd40438e0770b9cf2f0213d3'),
    '09_СанаторийМалаховка_продукты_питания.docx': (None, '6369f9b567fe37549d8b9e8d80b814f9c2ddbc1da58967045bfdc0949b38851e'),
    '10_РСОМалиновка_дизтопливо.docx': (None, 'eae04fac3bace6548524cf296bba9675efb94962021c72821e27ea05a3da35aa'),
    '11_СургутскоеПОПАТ_дизтопливо.docx': (None, 'ebdf49af4b40ed4c34073d166c89883682d2c5e6de03f78892a2173008923739'),
    '12_ГорэнергосистемыКачканар_спецодежда.docx': (None, 'aae2cceb5aca650accdb5a31005a0651a5e9b6436147d7b4121e3ad8b2a0ca5d'),
    '13_Гурьевский_спецодежда.docx': (None, '196513a8b59ad5ca015b3395a10861782b3fdcc0623b381309f93232eee04061'),
    '14_ТамбовскаяПКБ_мебель.docx': (None, '3d005fde024e840b65907f38fa3952b6739d1dc3a39b7d6801473dcffa7c1072'),
    '15_ЯрославскаяКБСМП_медрасходники.docx': (None, 'e2f87b5cffb1e860b8b081ba4267c6737fa0a56558873afbdfad0e8a2d7f09cb'),
    '16_ЦГиЭ_СПб_транспортные_услуги.doc': (None, '7d0ae02f5b31d52f8412fe8a03c48130656c006af42ad8e2ea3f1a006bc06ed5'),
    '17_РосатомВИЭ_автотранспорт.pdf': (None, '5e805713536e5de7734dfe8cd1f54f0e11f993a05c7de06b80998c38a8c546f4'),
    '18_Магаданэнерго_охрана.docx': ('91602-ПРО ДЭК-2027-МЭ-МЭС_ТТ.docx', '1c4990f3d39dc8fc72b02b4231c2b4419873c7e1f6149a4999bef9db8f490499'),
    '19_РоссетиСевКавказ_охрана_скан.pdf': ('ТЗ БВС 2026-2027.pdf', '9ff6dc3110eadf2ae0bc8d4757ad531614026399f3ed44423242dc82b5eedefa'),
    '20_Дальпресс_уборка.docx': (None, 'd20f67ec35bbb5d765175a4373856b8b8e598147f5a7a7be1edc27fdfb38705b'),
    '21_РоссетиУрал_уборка.docx': ('ТЗ услуги по уборке и ТО зданий ПЭ 2027.docx', 'b559783fabb243d40ad04ef9a397044b39ea9c26d0eb9cc03fa9110e1f4baa32'),
    '22_АэропортыЯмала_обучение.docx': (None, 'f1d55372aa639b7c15ed5e9c543d6508606407ebd6236d9fca145a9c17c753cc'),
    '23_Ставрополькрайводоканал_обучение.docx': ('Часть IV. Техническое задание.docx', 'f27807a62fb8a1b1511935e44dd0da8aa7a9ffff07c7882f25ccef6b1974aa61'),
    '24_АэропортУфа_страхование_КАСКО.docx': (None, '859523b1be0224d2088b33f519b904875ca1100968ab45ae167cb1d54834f300'),
    '25_ВТЕЮгоВосток_страхование_ДМС.pdf': (None, '2603f1c5bd4ade6de08b4129e2b29ae17ba5387c6713d838440eb16f61b2db9f'),
    '26_РоссетиСибирь_полиграфия.docx': ('ТЗ Поставка полиграфической продукции по ТБ.docx', '7202f0a58d68505f19915e3794cad4185ef78553be1bc3a3c8bf8534e7a36b04'),
    '27_УВЦ_аудит.docx': (None, 'b20280f5b9abc435bc3bf4963c9bf2aa907e7f35f93ff55794b3cdbf176d3648'),
    '28_ТеплоэнергоКраснодар_компьютерная_техника.doc': (None, 'fa0c10d976217ab305e5c71644a1fe4a15ed124a054a6162789f942f9b2db5fa'),
    '29_Мосгаз_оборудование_СВТ.pdf': (None, 'abea779cb98c34c8163046a53ecc73c0ef59a8716952bc27ab566c09c2dbe046'),
    '30_Атомспецтранс_электротехника.docx': (None, '747eb46fc38d0522e46d20f796885977cb8a68598b8b2d7ffd8e41b4fb9e08bf')
}


def _name(info: zipfile.ZipInfo) -> str:
    """Имена в zip без флага UTF-8 обычно в cp866."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp866")
    except UnicodeError:
        return info.filename


def fetch(url: str) -> bytes:
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=60, headers={"User-Agent": "tzcheck-eval"})
            r.raise_for_status()
            return r.content
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(MANIFEST.parent))
    ap.add_argument("--only", nargs="*", help="номера файлов, например 18 19")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ok = bad = 0
    with open(MANIFEST, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    if args.only:
        rows = [r for r in rows if r["файл"][:2] in args.only]
    for row in rows:
        name, url = row["файл"], row["ссылка на файл"]
        member, sha = FILES.get(name, (None, None))
        target = out / name
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == sha:
            ok += 1
            continue
        try:
            data = fetch(url)
            if data[:2] == b"PK" and member:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    data = z.read(next(i for i in z.infolist() if _name(i).endswith(member)))
        except Exception as e:
            print(f"{name}: не скачан ({type(e).__name__})")
            bad += 1
            continue
        target.write_bytes(data)
        same = hashlib.sha256(data).hexdigest() == sha
        print(f"{name}: {len(data)} байт" + ("" if same else ", sha256 не совпал: файл на площадке мог измениться"))
        ok += same
        bad += not same
    print(f"Готово: совпало {ok}, не совпало или не скачано {bad}")


if __name__ == "__main__":
    main()
