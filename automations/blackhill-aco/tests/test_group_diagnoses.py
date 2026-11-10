"""Offline checks for merging same-visit rows with different CPT/ICD10."""
import sys, types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
pw = types.ModuleType("playwright"); sa = types.ModuleType("playwright.sync_api")
sa.Error = Exception; sa.sync_playwright = None
sys.modules.setdefault("playwright", pw); sys.modules.setdefault("playwright.sync_api", sa)

import patients as p


def rec(row, cpt, icd, mod=""):
    return {"row": row, "name": "A B", "dob": "01/01/1950", "dos": "07/27/2026",
            "provider": "J S", "pos_raw": "32", "cpt": cpt, "modifier": mod,
            "icd10": p.prioritise_codes(p.split_icd10(icd, limit=None)), "icd10_raw": icd}


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        sys.exit(1)


g = p.group_records([rec(2, "99309", "E11.9, B35.1", "25"),
                     rec(3, "11721", "B35.1, M79.674, M79.675", "Q8")])
check("different CPT + ICD10 -> one visit", len(g) == 1)
check("two lines kept", g[0]["cpts"] == ["99309", "11721"] and g[0]["modifiers"] == ["25", "Q8"])
check("no duplicate diagnosis", g[0]["record"]["icd10"] == ["B35.1", "E11.9", "M79.674", "M79.675"])  # 11721 row -> foot order
check("box limit = 12 for two rows", g[0]["dx_limit"] == 12)

g = p.group_records([rec(2, "99309", "A1,B1,C1,D1,E1"), rec(3, "11721", "F1,G1,H1,I1,J1")])
check("every code of every row is kept", g[0]["record"]["icd10"] == "A1 B1 C1 D1 E1 F1 G1 H1 I1 J1".split())
check("limit is 12", g[0]["dx_limit"] == 12)

g = p.group_records([rec(2, "99309", "A1,B1,C1,D1,E1,F1")])
check("single row: all 6 codes kept, limit 12", g[0]["dx_limit"] == 12 and len(g[0]["record"]["icd10"]) == 6)
check("split_icd10 default keeps up to 12", len(p.split_icd10(",".join(f"A{i}" for i in range(15)))) == 12)

g = p.group_records([rec(2, "99309", "E11.9"), rec(3, "99309", "I73.9")])
check("same CPT, different ICD10 -> one visit, two lines", len(g) == 1 and g[0]["cpts"] == ["99309", "99309"])

check("pointer is 1234 with 8 diagnoses", p.diagnosis_pointer(8) == "1234")
check("pointer is 1234 with 12 diagnoses", p.diagnosis_pointer(12) == "1234")
check("pointer is 123 with 3 diagnoses", p.diagnosis_pointer(3) == "123")

# --- diagnosis priority: first sheet code, then HCC, then Z, then the rest ---
hcc = next(c for c in sorted(p.hcc_codes()) if not c.startswith("Z"))
non_hcc = next(c for c in ("R600", "R51", "M545") if not p.is_hcc(c))
z_non_hcc = next(c for c in ("Z0000", "Z23", "Z7689", "Z000") if not p.is_hcc(c))
z_hcc = "Z21"

check("test fixtures behave", p.is_hcc(hcc) and not p.is_hcc(non_hcc)
      and p.is_z_code(z_non_hcc) and not p.is_hcc(z_non_hcc) and p.is_hcc(z_hcc))

order = p.prioritise_codes([non_hcc, z_non_hcc, "X1", hcc, z_hcc, "Y1"])
check("first sheet code stays A(1) even if not HCC", order[0] == non_hcc)
check("then HCC (incl. HCC Z code) in sheet order", order[1:3] == [hcc, z_hcc])
check("then non-HCC Z codes", order[3] == z_non_hcc)
check("then the rest in sheet order", order[4:] == ["X1", "Y1"])
check("nothing lost or added", sorted(order) == sorted([non_hcc, z_non_hcc, "X1", hcc, z_hcc, "Y1"]))
check("idempotent", p.prioritise_codes(order) == order)
check("first code that is HCC and Z stays first", p.prioritise_codes([z_hcc, "X1", hcc])[0] == z_hcc)
check("empty list", p.prioritise_codes([]) == [])

g = p.group_records([rec(2, "99309", f"{non_hcc}, X1, {z_non_hcc}"), rec(3, "11721", f"{hcc}, Y1")])
check("merged rows: row 1's first code stays first, then HCC, Z, rest",
      g[0]["record"]["icd10"] == [non_hcc, hcc, z_non_hcc, "X1", "Y1"])


# Line Charges: a sheet that already has a "Charge" column is reused, and the
# column is always a written (output) field.
check("line_charge is an output field", "line_charge" in p.OUTPUT_FIELDS)
fields = p.resolve_against_headers(p.resolve_column_map({}), ["Patient Name", "Charge"])
check("existing 'Charge' header picked up for Line Charges", fields["line_charge"] == "Charge")
check("blank line_charge override falls back to default",
      p.resolve_column_map({"line_charge": ""})["line_charge"] == "Line Charges")


# Foot-care CPTs: B35.1 first, then I73.9 (else E11.9), then HCC / Z / rest.
def foot(cpt, icd):
    return p.group_records([rec(2, cpt, icd)])[0]


for cpt in ("11721", "11720", "11056", "11055", "11057"):
    check(f"{cpt} is a foot-care CPT", p.is_foot_visit([cpt]))
check("99309 is not", not p.is_foot_visit(["99309"]))

g = foot("11721", "E11.9, Z79.4, I73.9, B35.1, M79.674")
check("B35.1 first, I73.9 second", g["record"]["icd10"][:2] == ["B35.1", "I73.9"])
check("nothing lost", sorted(g["record"]["icd10"]) == sorted(["E11.9", "Z79.4", "I73.9", "B35.1", "M79.674"]))
check("B35.1 present -> nothing missing", g["missing_icd"] == "")

g = foot("11055", "M79.674, E11.9, B35.1")
check("no I73.9 -> E11.9 second", g["record"]["icd10"][:2] == ["B35.1", "E11.9"])

g = foot("11056", "M79.674, B35.1")
check("neither I73.9 nor E11.9 -> B35.1 then sheet order", g["record"]["icd10"] == ["B35.1", "M79.674"])

g = foot("11721", "E11.9, M79.674, I73.9")
check("B35.1 missing is reported", g["missing_icd"] == "B35.1")
check("B35.1 missing -> I73.9 takes the top", g["record"]["icd10"][0] == "I73.9")

g = foot("11721", "E11.9, M79.674")
check("B35.1 + I73.9 missing -> E11.9 on top", g["record"]["icd10"][0] == "E11.9" and g["missing_icd"] == "B35.1")

g = foot("99309", "E11.9, M79.674")
check("non-foot CPT: unchanged order, never flagged",
      g["record"]["icd10"][0] == "E11.9" and g["missing_icd"] == "")

g = p.group_records([rec(2, "99309", "E11.9"), rec(3, "11721", "B35.1, I73.9")])[0]
check("foot CPT on 2nd row still orders the whole visit", g["record"]["icd10"][:3] == ["B35.1", "I73.9", "E11.9"])

g = foot("11721", "b35.1, i73.9")
check("matched ignoring case", g["missing_icd"] == "")
