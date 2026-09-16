"""CI verification for the two megamerge production failures.

Bug 1 -- unitsPerEm mismatch (run #37, exit code 1 after 8 minutes)
    NotoSerifTodhri ships upem=1024 while every other merge candidate uses
    1000, so the "Noto Serif Living" merge died with
    "AssertionError: Expected all items to be equal: [1000, ..., 1024, ...]".

Bug 2 -- GSUB repacker livelock (runs #26-#39, killed at the 6 hour limit)
    The merged "Noto Sans Historical" GSUB overflows its 16-bit offsets.
    fontTools tries to recover by promoting lookups to Extension type, but it
    has no subtable splitter for lookup type 5, so compile() spins forever
    printing "Don't know how to split GSUB lookup type 5".

Each bug is verified with a control (the failure still reproduces) and a
treatment (the fix resolves it), so the test cannot silently become vacuous.
Nothing is written back to the repository.
"""
import ast
import json
import os
import sys
import time

from fontTools.ttLib import TTFont
from fontTools.merge import Merger, Options
from fontTools.ttLib.tables.otBase import USE_HARFBUZZ_REPACKER


def load_from_megamerge():
    """Load the fixes out of megamerge.py without executing the real job.

    megamerge.py runs the full merge at module level, so a plain import would
    kick off the multi-hour job. Execute only the prefix up to the first
    top-level statement, which covers imports, globals and function defs.
    Reading the real file means reverting a fix makes this verification fail.
    """
    source = open('megamerge.py').read()
    tree = ast.parse(source)
    cutoff = next(node.lineno for node in tree.body
                  if isinstance(node, (ast.For, ast.If)))
    prefix = "\n".join(source.splitlines()[:cutoff - 1])

    namespace = {'__name__': 'megamerge_fix'}
    exec(compile(prefix, 'megamerge.py', 'exec'), namespace)

    required = ('normalize_upem', 'UPEM', 'warnings', 'save_font')
    missing = [n for n in required if n not in namespace]
    if missing:
        raise SystemExit(f"megamerge.py is missing {missing}; fix not applied?")
    return namespace


MM = load_from_megamerge()
normalize_upem = MM['normalize_upem']
save_font = MM['save_font']
UPEM = MM['UPEM']
mm_warnings = MM['warnings']


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


BANNED = ["duployan", "latin-greek-cyrillic", "sign-writing", "test"]


def verify_upem():
    """Bug 1: the Serif Living merge must go from AssertionError to success."""
    print("=" * 62)
    print("BUG 1: unitsPerEm mismatch (Noto Serif Living)")
    print("=" * 62)

    mergelist = build_mergelist("Serif", lambda x: x <= 3, BANNED)
    print(f"merge list: {len(mergelist)} fonts")

    mismatched = [(os.path.basename(p), TTFont(p)['head'].unitsPerEm)
                  for p in mergelist
                  if TTFont(p)['head'].unitsPerEm != UPEM]
    print(f"fonts with non-{UPEM} upem: {mismatched}")
    if not mismatched:
        print("FAIL: no upem mismatch present, this test would be vacuous")
        return False

    print("\n[control] merging WITHOUT normalization, expecting AssertionError")
    try:
        merge(mergelist)
    except AssertionError as exc:
        print(f"          reproduced: {str(exc)[:80]}...")
    else:
        print("FAIL: merge unexpectedly succeeded without the fix")
        return False

    print("\n[treatment] merging WITH normalization, expecting success")
    start = time.time()
    try:
        merged = merge([normalize_upem(p) for p in mergelist])
    except Exception as exc:
        print(f"FAIL: still broken: {type(exc).__name__}: {exc}")
        return False
    print(f"          merged OK in {time.time() - start:.1f}s")

    out = "/tmp/verify-SerifLiving.ttf"
    merged.save(out)
    font = TTFont(out)
    cmap = font.getBestCmap()
    todhri = [cp for cp in cmap if 0x105C0 <= cp <= 0x105FF]
    print(f"          upem={font['head'].unitsPerEm} "
          f"glyphs={len(font.getGlyphOrder())} cmap={len(cmap)} "
          f"todhri_codepoints={len(todhri)}")

    if font['head'].unitsPerEm != UPEM:
        print(f"FAIL: merged upem is {font['head'].unitsPerEm}")
        return False
    if not todhri:
        print("FAIL: Todhri codepoints missing from merged font")
        return False

    print("\nPASS: upem normalization fixes the Serif Living merge")
    return True


def verify_livelock():
    """Bug 2: the Sans Historical save must complete instead of spinning."""
    print()
    print("=" * 62)
    print("BUG 2: GSUB repacker livelock (Noto Sans Historical)")
    print("=" * 62)

    mergelist = build_mergelist("Sans", lambda x: x > 3, BANNED)
    print(f"merge list: {len(mergelist)} fonts")
    merged = merge([normalize_upem(p) for p in mergelist])
    print(f"merged in memory: {len(merged.getGlyphOrder())} glyphs")

    # Control: prove the livelock is real by giving harfbuzz packing a short
    # budget in an isolated process. A healthy table packs well within this;
    # the livelocked one never finishes.
    print("\n[control] saving with harfbuzz packing, 60s budget")
    import multiprocessing
    queue = multiprocessing.Queue()
    proc = multiprocessing.Process(
        target=MM['_save_worker'],
        args=(merged, "/tmp/verify-control.ttf", True, queue))
    proc.start()
    proc.join(60)
    if proc.is_alive():
        proc.terminate()
        proc.join()
        print("          still running after 60s -> livelock confirmed")
    else:
        print("FAIL: harfbuzz packing finished; livelock no longer reproduces")
        return False

    # Treatment: the real save_font guard must produce a font.
    print("\n[treatment] saving via save_font() guard")
    out = "/tmp/verify-SansHistorical.ttf"
    start = time.time()
    try:
        save_font(merged, out, "Noto Sans Historical")
    except Exception as exc:
        print(f"FAIL: save_font raised {type(exc).__name__}: {exc}")
        return False
    elapsed = time.time() - start
    print(f"          completed in {elapsed:.1f}s")

    font = TTFont(out)
    cmap = font.getBestCmap()
    print(f"          glyphs={len(font.getGlyphOrder())} cmap={len(cmap)} "
          f"GSUB={'GSUB' in font} GPOS={'GPOS' in font}")

    if 'GSUB' not in font:
        print("FAIL: merged font lost its GSUB table")
        return False

    # Historical scripts must actually shape, not fall back to .notdef.
    try:
        import uharfbuzz as hb
        data = open(out, 'rb').read()
        hbfont = hb.Font(hb.Face(data))
        for name, cp in (("Egyptian Hieroglyphs", 0x13000),
                         ("Cuneiform", 0x12000),
                         ("Gothic", 0x10330)):
            buf = hb.Buffer()
            buf.add_str(chr(cp))
            buf.guess_segment_properties()
            hb.shape(hbfont, buf)
            gid = buf.glyph_infos[0].codepoint
            print(f"          {name} U+{cp:05X} -> gid {gid}"
                  f"{'' if gid else '  NOTDEF!'}")
            if not gid:
                print(f"FAIL: {name} shaped to .notdef")
                return False
    except ImportError:
        print("          (uharfbuzz unavailable, skipped shaping check)")

    print(f"\nPASS: save_font guard avoids the livelock "
          f"({elapsed:.0f}s vs >6h in production)")
    return True


def main():
    results = {
        "bug1_upem": verify_upem(),
        "bug2_livelock": verify_livelock(),
    }

    print()
    print("=" * 62)
    for name, passed in results.items():
        print(f"  {name}: {'PASS' if passed else 'FAIL'}")
    print(f"  warnings recorded: {mm_warnings}")
    print("=" * 62)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
