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


def verify_dedup_mechanism():
    """Bug 2, root cause: the dedup policy is the variable, not the packer.

    Compile the same merged GSUB twice with the *same* pure-python
    serializer, changing only what _doneWriting() was told:

      shareExtension=False  the conservative policy that serializer expects
      shareExtension=True   what getAllDataUsingHarfbuzz() leaves behind

    If the first converges and the second does not, the hang cannot be
    blamed on the packer -- it is the graph it was handed.
    """
    import threading
    from fontTools.ttLib.tables.otBase import (
        OTTableWriter, OTLOffsetOverflowError)
    from fontTools.ttLib.tables import otTables

    print()
    print("=" * 62)
    print("BUG 2 root cause: dedup policy decides convergence")
    print("=" * 62)

    mergelist = [normalize_upem(p)
                 for p in build_mergelist("Sans", lambda x: x > 3, BANNED)]
    font = merge(mergelist)
    table = font["GSUB"]
    print(f"Sans Historical GSUB: "
          f"{len(table.table.LookupList.Lookup)} lookups")

    # The conservative arm needs ~70s locally; CI runners measured ~1.7x
    # slower on this same workload, so leave generous headroom. The
    # aggressive arm never converges, so a larger budget costs nothing but
    # makes a timeout here unambiguous.
    budget = int(os.environ.get("MEGAMERGE_CONTROL_BUDGET", "420"))

    def attempt(share_extension, budget=budget):
        rounds = [0]
        result = [None]
        original = otTables.fixLookupOverFlows

        def counted(ttf, record, _orig=original):
            rounds[0] += 1
            return _orig(ttf, record)

        otTables.fixLookupOverFlows = counted

        def run():
            last = None
            try:
                while True:
                    writer = OTTableWriter(tableTag="GSUB")
                    table.table.compile(writer, font)
                    try:
                        writer._doneWriting({}, shareExtension=share_extension)
                        result[0] = ("converged",
                                     len(writer.getAllData(
                                         remove_duplicate=False)))
                        return
                    except OTLOffsetOverflowError as exc:
                        ok = table.tryResolveOverflow(font, exc, last)
                        last = exc.value
                        if not ok:
                            result[0] = ("gave up", rounds[0])
                            return
            except BaseException as exc:
                result[0] = (type(exc).__name__, str(exc)[:40])

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(budget)
        otTables.fixLookupOverFlows = original
        return result[0] or ("still spinning", f">{budget}s"), rounds[0]

    conservative, rounds_conservative = attempt(False)
    aggressive, rounds_aggressive = attempt(True)

    print(f"\n  shareExtension=False (conservative, what pure-FT expects)")
    print(f"      -> {conservative[0]}, {conservative[1]} "
          f"[{rounds_conservative} overflow rounds]")
    print(f"  shareExtension=True  (what harfbuzz leaves behind)")
    print(f"      -> {aggressive[0]}, {aggressive[1]} "
          f"[{rounds_aggressive} overflow rounds]")

    if conservative[0] != "converged":
        print(f"\nFAIL: conservative dedup should converge but reported "
              f"'{conservative[0]}'.")
        if conservative[0] == "still spinning":
            print(f"      It converged in ~95 rounds when this was written; "
                  f"it reached {rounds_conservative} here.")
            print(f"      If the machine is simply slower, raise "
                  f"MEGAMERGE_CONTROL_BUDGET (currently {budget}s).")
        return False
    if aggressive[0] == "converged":
        print("\nFAIL: aggressive dedup converged; "
              "the root cause no longer reproduces")
        return False

    print("\nPASS: same packer, same font -- only the dedup policy differs,")
    print("      so recompiling from scratch (not reusing hb's graph) is the fix")
    return True


def verify_all_groups():
    """Bug 2, the fix: every group must produce a font, none may hang."""
    print()
    print("=" * 62)
    print("BUG 2 fix: all four merge groups via save_font()")
    print("=" * 62)

    groups = (
        ("Sans", "Living", lambda x: x <= 3),
        ("Sans", "Historical", lambda x: x > 3),
        ("Serif", "Living", lambda x: x <= 3),
        ("Serif", "Historical", lambda x: x > 3),
    )
    checks = {
        "Sans Historical": (("Egyptian Hieroglyphs", 0x13000),
                            ("Cuneiform", 0x12000),
                            ("Gothic", 0x10330)),
        "Serif Living": (("Todhri", 0x105C1),),
    }

    for modulation, label, predicate in groups:
        name = f"{modulation} {label}"
        mergelist = [normalize_upem(p)
                     for p in build_mergelist(modulation, predicate, BANNED)]
        out = f"/tmp/verify-{modulation}{label}.ttf"
        print(f"\n  {name} ({len(mergelist)} fonts)")
        start = time.time()
        try:
            save_font(mergelist, out, f"Noto {modulation} {label}")
        except Exception as exc:
            print(f"  FAIL: {type(exc).__name__}: {exc}")
            return False
        elapsed = time.time() - start

        font = TTFont(out)
        cmap = font.getBestCmap()
        print(f"      total {elapsed:.1f}s | glyphs={len(font.getGlyphOrder())} "
              f"cmap={len(cmap)} GSUB={'GSUB' in font} GPOS={'GPOS' in font}")

        if 'GSUB' not in font:
            print(f"  FAIL: {name} lost its GSUB table")
            return False

        # The merged font must be re-readable, not just written.
        import io
        buffer = io.BytesIO()
        font.save(buffer)
        buffer.seek(0)
        if len(TTFont(buffer).getGlyphOrder()) != len(font.getGlyphOrder()):
            print(f"  FAIL: {name} does not survive a recompile round-trip")
            return False

        # Scripts unique to this group must shape, not hit .notdef.
        try:
            import uharfbuzz as hb
            hbfont = hb.Font(hb.Face(open(out, 'rb').read()))
            for script, codepoint in checks.get(name, ()):
                buf = hb.Buffer()
                buf.add_str(chr(codepoint))
                buf.guess_segment_properties()
                hb.shape(hbfont, buf)
                gid = buf.glyph_infos[0].codepoint
                print(f"      {script} U+{codepoint:05X} -> gid {gid}"
                      f"{'' if gid else '  NOTDEF!'}")
                if not gid:
                    print(f"  FAIL: {script} shaped to .notdef")
                    return False
        except ImportError:
            print("      (uharfbuzz unavailable, skipped shaping check)")

    print("\nPASS: all four groups produced a usable font")
    return True


def main():
    results = {
        "bug1_upem": verify_upem(),
        "bug2_root_cause": verify_dedup_mechanism(),
        "bug2_all_groups": verify_all_groups(),
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
