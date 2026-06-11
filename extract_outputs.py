"""Walk the executed notebook and print every code cell's text output (truncating long ones)."""
import json, pathlib, re

NB = pathlib.Path(__file__).parent / "Crypto-AML-Analysis.executed.ipynb"
nb = json.loads(NB.read_text(encoding="utf-8"))

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

def clean(text: str) -> str:
    return ANSI_RE.sub("", text)

for i, cell in enumerate(nb["cells"]):
    if cell.get("cell_type") != "code":
        continue
    outputs = cell.get("outputs", [])
    if not outputs:
        continue
    src = "".join(cell.get("source", "")) if isinstance(cell.get("source"), list) else cell.get("source", "")
    first_line = src.strip().split("\n", 1)[0][:80]
    print(f"\n{'='*70}\nCELL #{i}  cid={cell.get('id','')}  | {first_line}\n{'='*70}")
    for out in outputs:
        ot = out.get("output_type")
        if ot == "stream":
            txt = clean("".join(out.get("text", [])))
            print(txt)
        elif ot in ("execute_result", "display_data"):
            data = out.get("data", {})
            if "text/plain" in data:
                txt = "".join(data["text/plain"]) if isinstance(data["text/plain"], list) else data["text/plain"]
                print(clean(txt))
            if "image/png" in data:
                print("[image/png embedded -> plot]")
        elif ot == "error":
            print("ERROR:", out.get("ename"), out.get("evalue"))
