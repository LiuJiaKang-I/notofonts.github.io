"""CI verification for the unitsPerEm normalization fix.

Builds the exact "Noto Serif Living" merge list that megamerge.py builds --
the group that failed in production with:

    AssertionError: Expected all items to be equal: [1000, ..., 1024, ...]

then asserts two things:

  1. WITHOUT normalization the merge still raises AssertionError
     (proves the bug is real and this test is not vacuous)
  2. WITH normalization the merge succeeds and produces a sane font

Exits non-zero if either expectation is not met. Writes nothing to the repo.
"""
import json
import os
import sys
import time

from fontTools.ttLib import TTFont
from fontTools.merge import Merger, Options


def load_fix_from_megamerge():
    """Load normalize_upem/UPEM out of megamerge.py without running it.

    megamerge.py performs the full merge at module level, so a plain import
    would kick off the real (multi-hour) job. Instead execute only the module
    prefix up to the first top-level statement, which covers the imports,
    globals and function definitions we need. This still reads the real file,
    so reverting the fix makes this verification fail.
    """
    import ast

    source = open('megamerge.py').read()
    tree = ast.parse(source)
    cutoff = next(node.lineno for node in tree.body
                  if isinstance(node, (ast.For, ast.If)))
    prefix = "\n".join(source.splitlines()[:cutoff - 1])

    namespace = {'__name__': 'megamerge_fix'}
    exec(compile(prefix, 'megamerge.py', 'exec'), namespace)

    missing = [n for n in ('normalize_upem', 'UPEM', 'warnings')
               if n not in namespace]
    if missing:
        raise SystemExit(f"megamerge.py is missing {missing}; fix not applied?")
    return namespace['normalize_upem'], namespace['UPEM'], namespace['warnings']


normalize_upem, UPEM, mm_warnings = load_fix_from_megamerge()


def build_mergelist(modulation, tier_predicate, banned):
    """Replicate megamerge.py's font selection logic."""
    tiers = json.load(open('../fontrepos.json'))
    state = json.load(open('../state.json'))

    base = f"../fonts/Noto{modulation}/googlefonts/ttf/Noto{modulation}-Regular.ttf"
    repos = [k for k, v in tiers.items() if tier_predicate(v.get("tier", 4))]
    repos = [k for k in repos if k not in banned]
    repos = sorted(repos, key=lambda k: tiers[k]["tier"])

    mergelist = [base]
    glyph_count = len(TTFont(base).getGlyphOrder())
    for repo in repos:
        if "families" not in state[repo]:
            continue
        families = [x for x in state[repo]["families"]
                    if modulation in x and "UI" not in x]
        if not families:
            continue
        files = [x for x in state[repo]["families"][families[0]]["files"]
                 if "Regular.ttf" in x and "UI" not in x]
        target = next((f for f in files if "/hinted/" in f), None)
        if target is None:
            target = next((f for f in files if "/unhinted/" in f), None)
        if target is None:
            continue
        glyph_count += len(TTFont("../" + target).getGlyphOrder())
        if glyph_count > 65535:
            break
        mergelist.append("../" + target)
    return mergelist


def merge(paths):
    return Merger(options=Options(
        drop_tables=["vmtx", "vhea", "MATH"])).merge(paths)


def main():
    banned = ["duployan", "latin-greek-cyrillic", "sign-writing", "test"]
    mergelist = build_mergelist("Serif", lambda x: x <= 3, banned)

    print(f"Serif Living merge list: {len(mergelist)} fonts")
    mismatched = [(os.path.basename(p), TTFont(p)['head'].unitsPerEm)
                  for p in mergelist
                  if TTFont(p)['head'].unitsPerEm != UPEM]
    print(f"Fonts with non-{UPEM} upem: {mismatched}")

    if not mismatched:
        print("FAIL: no upem mismatch present, this test would be vacuous")
        return 1

    # --- 1. control: the bug must still reproduce without the fix ---
    print("\n[1/2] merging WITHOUT normalization (expecting AssertionError)")
    try:
        merge(mergelist)
    except AssertionError as exc:
        print(f"      reproduced as expected: {str(exc)[:90]}...")
    else:
        print("FAIL: merge unexpectedly succeeded without the fix")
        return 1

    # --- 2. the fix must make the same merge succeed ---
    print("\n[2/2] merging WITH normalization (expecting success)")
    start = time.time()
    try:
        normalized = [normalize_upem(p) for p in mergelist]
        merged = merge(normalized)
    except Exception as exc:
        print(f"FAIL: merge still broken: {type(exc).__name__}: {exc}")
        return 1
    print(f"      merged OK in {time.time() - start:.1f}s")
    print(f"      warnings recorded: {mm_warnings}")

    # --- sanity-check the resulting font ---
    out = "/tmp/SerifLiving-verify.ttf"
    merged.save(out)
    font = TTFont(out)
    upem = font['head'].unitsPerEm
    glyphs = len(font.getGlyphOrder())
    cmap = font.getBestCmap()
    todhri = [cp for cp in cmap if 0x105C0 <= cp <= 0x105FF]

    print(f"\n      upem={upem} glyphs={glyphs} cmap={len(cmap)}")
    print(f"      Todhri codepoints retained: {len(todhri)}")
    print(f"      dropped tables absent: "
          f"{all(t not in font for t in ('vmtx', 'vhea', 'MATH'))}")

    if upem != UPEM:
        print(f"FAIL: merged upem is {upem}, expected {UPEM}")
        return 1
    if not todhri:
        print("FAIL: Todhri codepoints missing from merged font")
        return 1
    if not mm_warnings:
        print("FAIL: rescaling happened but was not recorded in warnings")
        return 1

    print("\nPASS: upem normalization fixes the Serif Living merge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
