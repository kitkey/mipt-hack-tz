"""Split existing public DOCX bundles at their original top-level appendix boundaries."""
import os
from pathlib import Path
import docx

SOURCE = Path(os.environ.get("TZCHECK_EIS_DIR", Path(__file__).resolve().parents[2] / "data_raw" / "eis_more"))
OUT = Path(__file__).resolve().parent / "split"
SPECS = {"02_СПбГУ_ремонт_лифта.docx": [298, 316, 322],
         "03_ГУАП_метрологическая_лаборатория.docx": [217, 233, 242, 264, 273, 300]}

def split():
    for filename, boundaries in SPECS.items():
        folder = OUT / filename[:2]; folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob("*.docx"):
            old.unlink()
        original = SOURCE / filename
        count = len(docx.Document(original).element.body)
        edges = [0, *boundaries, count]
        for i, (start, stop) in enumerate(zip(edges, edges[1:])):
            document = docx.Document(original)
            body = document.element.body
            for position, child in reversed(list(enumerate(body))):
                if child.tag.endswith("}sectPr"):
                    continue
                if not start <= position < stop:
                    body.remove(child)
            numbers = [3, 4, 5] if filename.startswith("02") else [1, 2, 3, 2, 3, 1]
            name = "main.docx" if i == 0 else f"Приложение {numbers[i-1]}_часть{i}.docx"
            document.save(folder / name)
        print(filename, "=>", len(edges)-1, "частей")

if __name__ == "__main__":
    split()
