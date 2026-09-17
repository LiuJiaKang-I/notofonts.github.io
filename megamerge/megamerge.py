import json
import multiprocessing
import os
import tempfile
import time
from fontTools.ttLib import TTFont
from fontTools.merge import Merger, Options
from fontTools.ttLib.scaleUpem import scale_upem
from fontTools.ttLib.tables.otBase import USE_HARFBUZZ_REPACKER
from gftools.fix import rename_font

tiers = json.load(open('../fontrepos.json'))
state = json.load(open('../state.json'))
warnings = []

UPEM = 1000
_tmpdir = tempfile.mkdtemp(prefix="megamerge-upem-")


def normalize_upem(path):
    """Rescale a font to the common UPEM so fontTools' merger can combine it.

    fontTools requires head.unitsPerEm to be identical across all merged fonts
    and raises AssertionError otherwise. Returns a path to a normalized copy,
    or the original path when no rescaling is needed.
    """
    font = TTFont(path)
    if font['head'].unitsPerEm == UPEM:
        return path
    warnings.append(
        f"Rescaled {os.path.basename(path)} from "
        f"{font['head'].unitsPerEm} to {UPEM} upem before merging"
    )
    scale_upem(font, UPEM)
    out = os.path.join(_tmpdir, os.path.basename(path))
    font.save(out)
    return out


# Last-resort guard: the pure-fontTools packer can itself livelock on the
# largest groups, so no single strategy is safe without a time limit.
HB_REPACK_BUDGET = int(os.environ.get("MEGAMERGE_HB_BUDGET", "300"))

MERGE_OPTIONS = Options(drop_tables=["vmtx", "vhea", "MATH"])


def _save_worker(mergelist, path, newname, use_hb, queue):
    """Merge and save from scratch inside a child process.

    Re-doing the merge here (rather than passing a merged font in) is the
    point of this function: it guarantees a pristine writer tree, so the
    fontTools-only attempt never inherits state left behind by harfbuzz.
    """
    try:
        font = Merger(options=MERGE_OPTIONS).merge(mergelist)
        rename_font(font, newname)
        if not use_hb:
            font.cfg[USE_HARFBUZZ_REPACKER] = False
        font.save(path)
        queue.put(None)
    except BaseException as exc:  # pragma: no cover - reported to the parent
        queue.put(f"{type(exc).__name__}: {exc}")


def save_font(mergelist, path, newname):
    """Merge and save, working around the GSUB offset-overflow livelock.

    A merged GSUB overflows its uint16 offsets, and the two packers fail on
    different groups, so neither can be chosen statically:

        Sans Living (84 fonts)     harfbuzz ok      pure fontTools livelocks
        Sans Historical (61 fonts) harfbuzz fails   pure fontTools ok

    What makes the harfbuzz failure fatal is not the packer itself but the
    state it leaves behind. getAllDataUsingHarfbuzz() first calls
    _doneWriting(shareExtension=True), which dedups aggressively across
    Extension boundaries; when hb.repack then raises RepackerError,
    tryPackingHarfbuzz falls back to getAllData(remove_duplicate=False) --
    correctly, since _doneWriting must not run twice -- and the pure-python
    serializer is handed a graph laid out for a packer that just gave up.
    Measured on Sans Historical, GSUB: the conservative layout converges in
    95 overflow-resolution rounds, the harfbuzz layout was still spinning
    after 123.

    So on failure we discard everything and re-merge from scratch with the
    repacker disabled, which recompiles under the conservative dedup policy
    the pure-python serializer expects.
    """
    for use_hb, label in ((True, "harfbuzz"), (False, "fontTools-only")):
        queue = multiprocessing.Queue()
        proc = multiprocessing.Process(
            target=_save_worker,
            args=(mergelist, path, newname, use_hb, queue),
        )
        start = time.monotonic()
        proc.start()
        proc.join(HB_REPACK_BUDGET if use_hb else None)

        if proc.is_alive():
            proc.terminate()
            proc.join()
            warnings.append(
                f"{newname}: harfbuzz packing exceeded {HB_REPACK_BUDGET}s, "
                f"recompiled from scratch without it"
            )
            continue

        error = queue.get() if not queue.empty() else None
        if proc.exitcode == 0 and error is None:
            print(f"  saved {os.path.basename(path)} "
                  f"using {label} packing in {time.monotonic() - start:.1f}s")
            return
        if use_hb:
            warnings.append(
                f"{newname}: harfbuzz packing failed ({error or proc.exitcode}), "
                f"recompiled from scratch without it"
            )
            continue
        raise RuntimeError(f"Failed to save {path}: {error or proc.exitcode}")


def megamerge(newname, base_font, tier_predicate, banned, modulation):
    glyph_count = len(TTFont(base_font).getGlyphOrder())
    selected_repos = [k for k, v in tiers.items() if tier_predicate(v.get("tier", 4))]
    selected_repos = [k for k in selected_repos if k not in banned]
    selected_repos = sorted(selected_repos, key=lambda k: tiers[k]["tier"])
    mergelist = [base_font]
    for repo in selected_repos:
        if "families" not in state[repo]:
            print(f"Skipping odd repo {repo} (no families)")
            continue
        selected_families = [x for x in state[repo]["families"].keys() if modulation in x and "UI" not in x]
        if not selected_families:
            continue
        files = state[repo]["families"][selected_families[0]]["files"]
        files = [ x for x in files if "Regular.ttf" in x and "UI" not in x]
        target = None
        for file in files:
            if "/hinted/" in file:
                target = file
                break
        if target is None:
            for file in files:
                if "/unhinted/" in file:
                    target = file
                    break
        if target is None:
            print(f"Couldn't find a target for {repo}")
            continue
        target_font = TTFont("../"+target)
        glyph_count += len(target_font.getGlyphOrder())
        if glyph_count > 65535:
            warnings.append(f"Too many glyphs while building {newname}, stopped at {repo}")
            break
        mergelist.append("../"+target)
    print("Merging: ")
    for x in mergelist:
        print("  "+os.path.basename(x))
    mergelist = [normalize_upem(x) for x in mergelist]
    save_font(mergelist, newname.replace(" ","")+"-Regular.ttf", newname)


for modulation in ["Sans", "Serif"]:
    banned = ["duployan", "latin-greek-cyrillic", "sign-writing", "test"]
    if modulation == "sans":
        banned.append("devanagari")  # Already included
    megamerge(f"Noto {modulation} Living", 
            base_font=f"../fonts/Noto{modulation}/googlefonts/ttf/Noto{modulation}-Regular.ttf",
            tier_predicate= lambda x: x <= 3,
            banned=banned,
            modulation=modulation,
            )
    megamerge(f"Noto {modulation} Historical", 
            base_font=f"../fonts/Noto{modulation}/googlefonts/ttf/Noto{modulation}-Regular.ttf",
            tier_predicate= lambda x: x > 3,
            banned=banned,
            modulation=modulation
            )

if warnings:
    print("\n\nWARNINGS:")
    for w in warnings:
        print(w)
else:
    print("Completed successfully")